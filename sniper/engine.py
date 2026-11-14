"""The polling engine.

One scheduler loop per platform. Each loop walks the active watches round-robin,
spacing requests so the platform sees a steady, modest request rate no matter
how many watches you have:

    gap = max(min_gap, interval / n_watches)   (+/- jitter)

So with 3 watches and interval 30s, Vinted gets a request every ~10s and every
watch is re-checked every ~30s. New listings are diffed against SQLite, run
through the rule engine, and broadcast to every notifier.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import rules
from .config import load_watches
from .models import Listing, Watch
from .notifiers import Notifier, broadcast, broadcast_text
from .sources import BlockedError, Source, SourceError
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class PlatformHealth:
    ok_polls: int = 0
    fail_streak: int = 0
    last_ok: float = 0.0
    last_error: str = ""
    backoff_until: float = 0.0
    warned: bool = False
    latencies: list = field(default_factory=list)

    def avg_latency(self) -> float:
        return sum(self.latencies) / len(self.latencies) if self.latencies else 0.0


class Engine:
    def __init__(self, cfg: dict, sources: dict[str, Source], notifiers: list[Notifier],
                 store: Store, watches_path: str = "watches.yaml",
                 watches: Optional[list[Watch]] = None,
                 sleep: Callable = asyncio.sleep, clock: Callable = time.time):
        self.cfg = cfg
        self.sources = sources
        self.notifiers = notifiers
        self.store = store
        self.watches_path = watches_path
        self._static_watches = watches
        self.watches: list[Watch] = watches or []
        self._watches_mtime = None
        self.sleep = sleep
        self.clock = clock
        pc = cfg.get("poll", {})
        self.interval = float(pc.get("interval_seconds", 30))
        self.min_gap = float(pc.get("min_gap_seconds", 2.5))
        self.jitter = float(pc.get("jitter", 0.25))
        self.max_backoff = float(pc.get("max_backoff_seconds", 900))
        self.alert_on_start = bool(cfg.get("alert_on_start", False))
        dma = cfg.get("max_listing_age_minutes", 120)
        self.default_max_age = float(dma) if dma not in (None, 0, "") else None
        self.health = {p: PlatformHealth() for p in sources}
        self.started = clock()
        self.alerts_sent = 0
        self._stop = asyncio.Event()

    # ---------- watches ----------
    def reload_watches(self, force: bool = False) -> bool:
        if self._static_watches is not None:
            return False
        p = Path(self.watches_path)
        try:
            mtime = p.stat().st_mtime if p.exists() else None
        except OSError:
            return False
        if not force and mtime == self._watches_mtime:
            return False
        try:
            self.watches = load_watches(p)
            self._watches_mtime = mtime
            log.info("loaded %d watch(es) from %s", len(self.watches), p)
            return True
        except Exception as e:
            log.error("watches.yaml has an error, keeping previous watches: %s", e)
            self._watches_mtime = mtime
            return False

    def active_watches(self, platform: str) -> list[Watch]:
        return [w for w in self.watches if w.enabled and platform in w.platforms]

    # ---------- core ----------
    async def poll_watch(self, platform: str, watch: Watch) -> list[Listing]:
        """Poll one watch on one platform. Returns the listings that were alerted."""
        src = self.sources[platform]
        t0 = time.perf_counter()
        listings = await src.search(watch)
        h = self.health[platform]
        h.latencies = (h.latencies + [time.perf_counter() - t0])[-20:]

        scope = watch.fingerprint(platform)
        new_keys = set(self.store.filter_unseen(scope, [l.key for l in listings]))
        first_time = not self.store.has_baseline(scope)
        if first_time:
            self.store.set_baseline(scope)
            if not self.alert_on_start:
                log.info("[%s/%s] baseline: %d current listings marked as seen",
                         watch.id, platform, len(listings))
                return []

        alerted = []
        now = self.clock()
        for l in listings:
            if l.key not in new_keys:
                continue
            reason = rules.check(l, watch, now, self.default_max_age)
            if reason:
                log.debug("[%s/%s] skip %s: %s", watch.id, platform, l.title[:40], reason)
                continue
            if not self.store.claim_alert(l.key, watch.id, l):
                continue  # another watch already alerted this exact item
            log.info("[%s/%s] MATCH %s %.2f %s -> %s", watch.id, platform, l.title[:60],
                     l.price, l.currency, l.url)
            await broadcast(self.notifiers, l, watch)
            self.alerts_sent += 1
            alerted.append(l)
        return alerted

    def _gap(self, n: int) -> float:
        base = max(self.min_gap, self.interval / max(n, 1))
        return base * random.uniform(1 - self.jitter, 1 + self.jitter)

    async def _record_failure(self, platform: str, err: Exception):
        h = self.health[platform]
        h.fail_streak += 1
        h.last_error = str(err)
        blocked = isinstance(err, BlockedError)
        base = 30 if blocked else 5
        delay = min(self.max_backoff, base * (2 ** min(h.fail_streak - 1, 8)))
        if blocked and err.retry_after:
            delay = max(delay, err.retry_after)
        h.backoff_until = self.clock() + delay
        log.warning("%s: %s (failure #%d) - backing off %.0fs", platform, err, h.fail_streak, delay)
        if h.fail_streak >= 5 and not h.warned:
            h.warned = True
            await broadcast_text(self.notifiers,
                                 f"⚠️ {platform.title()} checks are failing: {err}. "
                                 f"Retrying automatically; you'll get a message when it recovers.")

    async def _record_ok(self, platform: str):
        h = self.health[platform]
        if h.warned:
            await broadcast_text(self.notifiers, f"✅ {platform.title()} checks are working again.")
        h.ok_polls += 1
        h.fail_streak = 0
        h.warned = False
        h.last_ok = self.clock()
        h.backoff_until = 0

    async def platform_loop(self, platform: str):
        idx = 0
        while not self._stop.is_set():
            self.reload_watches()
            ws = self.active_watches(platform)
            if not ws:
                await self._wait(5)
                continue
            h = self.health[platform]
            if h.backoff_until > self.clock():
                await self._wait(min(h.backoff_until - self.clock(), 30))
                continue
            w = ws[idx % len(ws)]
            idx += 1
            try:
                await self.poll_watch(platform, w)
                await self._record_ok(platform)
            except SourceError as e:
                await self._record_failure(platform, e)
            except Exception as e:  # bug guard: log and keep going
                log.exception("%s: unexpected error polling %s", platform, w.id)
                await self._record_failure(platform, SourceError(repr(e)))
            await self._wait(self._gap(len(ws)))

    async def _wait(self, seconds: float):
        if seconds <= 0:
            return
        if self.sleep is not asyncio.sleep:
            await self.sleep(seconds)
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def heartbeat(self):
        """Log a one-line 'still alive' summary so a quiet window doesn't look frozen."""
        every = float(self.cfg.get("heartbeat_minutes", 5)) * 60
        if every <= 0:
            return
        while not self._stop.is_set():
            await self._wait(every)
            if self._stop.is_set():
                break
            parts = []
            for p, h in self.health.items():
                if h.fail_streak:
                    parts.append(f"{p}: FAILING x{h.fail_streak} ({h.last_error[:60]})")
                else:
                    ago = int(self.clock() - h.last_ok) if h.last_ok else -1
                    parts.append(f"{p}: {h.ok_polls} checks OK, last {ago}s ago")
            log.info("💓 alive · %s · %d alert(s) so far", " | ".join(parts) or "no sites",
                     self.alerts_sent)

    async def housekeeping(self):
        while not self._stop.is_set():
            await self._wait(3600)
            try:
                self.store.prune(30)
            except Exception:
                log.exception("prune failed")

    def stop(self):
        self._stop.set()

    async def run(self, extra_tasks: tuple = ()):
        self.reload_watches(force=True)
        if not self.watches:
            log.warning("no watches yet - add some to %s (or via Telegram /add)", self.watches_path)
        names = ", ".join(self.sources) or "none"
        await broadcast_text(self.notifiers,
                             f"🟢 Resale Sniper started - {len(self.watches)} watch(es) on {names}.")
        tasks = [asyncio.create_task(self.platform_loop(p), name=f"loop-{p}") for p in self.sources]
        tasks.append(asyncio.create_task(self.housekeeping(), name="housekeeping"))
        tasks.append(asyncio.create_task(self.heartbeat(), name="heartbeat"))
        tasks += [asyncio.create_task(t) for t in extra_tasks]
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def status_text(self) -> str:
        up = int(self.clock() - self.started)
        lines = [f"⏱ Up {up // 3600}h {up % 3600 // 60}m · {self.alerts_sent} alert(s) this session",
                 f"👀 {sum(w.enabled for w in self.watches)} active / {len(self.watches)} watch(es)"]
        for p, h in self.health.items():
            if h.fail_streak:
                state = f"❌ failing x{h.fail_streak}: {h.last_error[:80]}"
            elif h.ok_polls:
                ago = int(self.clock() - h.last_ok)
                state = f"✅ {h.ok_polls} checks, last {ago}s ago, ~{h.avg_latency():.1f}s/request"
            else:
                state = "⏳ starting"
            n = len(self.active_watches(p))
            cycle = max(self.interval, n * self.min_gap) if n else 0
            lines.append(f"{p.title()}: {state}" + (f" · each watch every ~{cycle:.0f}s" if n else ""))
        return "\n".join(lines)
