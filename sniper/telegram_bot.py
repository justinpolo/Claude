"""Manage watches from your phone via the same Telegram bot that sends alerts.

Only messages from the configured chat_id are obeyed.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shlex
from typing import Optional

import httpx

from .config import load_watches, save_watches
from .models import PLATFORMS, Watch

log = logging.getLogger(__name__)

HELP = """<b>Resale Sniper commands</b>
/add <i>search words</i> [options] - new watch
   max=50  min=10  cond=very_good+  size=M,L
   brand=nike  not=kids,fake  on=vinted|depop|all  id=myname
   e.g. <code>/add nike dunk low max=60 size=9,9.5 cond=good+</code>
/list - show watches
/pause <i>id</i> · /resume <i>id</i> · /remove <i>id</i>
/status - health + speed
/recent - last alerts
/help - this message

Conditions (worst→best): fair, good, very_good, like_new, new, new_with_tags.
Add <b>+</b> to mean "this or better"."""

KEYMAP = {
    "max": "max_price", "under": "max_price", "min": "min_price", "over": "min_price",
    "size": "sizes", "sizes": "sizes", "brand": "brands", "brands": "brands",
    "not": "exclude", "exclude": "exclude", "must": "must_include", "need": "must_include",
    "on": "platforms", "platform": "platforms", "id": "id", "name": "id",
    "cond": "condition", "condition": "condition", "age": "max_age_minutes",
}


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "watch"


def parse_add(args: str, existing_ids: set) -> Watch:
    try:
        tokens = shlex.split(args)
    except ValueError:
        tokens = args.split()
    words, opts = [], {}
    for t in tokens:
        m = re.match(r"^([a-zA-Z_]+)[=:](.+)$", t)
        if m and m.group(1).lower() in KEYMAP:
            opts[KEYMAP[m.group(1).lower()]] = m.group(2)
        elif re.match(r"^[£$€]\d+(\.\d+)?$", t):
            opts["max_price"] = t[1:]
        else:
            words.append(t)
    query = " ".join(words).strip()
    if not query:
        raise ValueError("give me something to search for, e.g. /add carhartt jacket max=40")
    d: dict = {"query": query}
    for k in ("max_price", "min_price", "max_age_minutes"):
        if k in opts:
            d[k] = float(re.sub(r"[^\d.]", "", opts[k]))
    for k in ("sizes", "brands", "exclude", "must_include"):
        if k in opts:
            d[k] = [x.strip() for x in opts[k].split(",") if x.strip()]
    if "platforms" in opts:
        p = opts["platforms"].lower()
        d["platforms"] = list(PLATFORMS) if p in ("all", "both") else [x for x in p.split(",")]
    if "condition" in opts:
        c = opts["condition"].strip()
        if c.endswith("+"):
            d["min_condition"] = c[:-1]
        else:
            d["conditions"] = [x for x in c.split(",") if x]
    base = slugify(opts.get("id", query))
    wid, n = base, 2
    while wid in existing_ids:
        wid, n = f"{base}-{n}", n + 1
    d["id"] = wid
    return Watch.from_dict(d)


def describe(w: Watch) -> str:
    bits = [f"<b>{w.id}</b>{'' if w.enabled else ' (paused)'}: “{w.query}”"]
    rules = []
    if w.min_price is not None or w.max_price is not None:
        lo = f"{w.min_price:g}" if w.min_price is not None else "0"
        hi = f"{w.max_price:g}" if w.max_price is not None else "∞"
        rules.append(f"price {lo}–{hi}")
    if w.min_condition:
        rules.append(f"cond ≥ {w.min_condition}")
    if w.conditions:
        rules.append("cond " + "/".join(w.conditions))
    if w.sizes:
        rules.append("size " + ",".join(w.sizes))
    if w.brands:
        rules.append("brand " + ",".join(w.brands))
    if w.exclude:
        rules.append("not " + ",".join(w.exclude))
    if w.must_include:
        rules.append("needs " + ",".join(w.must_include))
    rules.append("on " + "+".join(w.platforms))
    return bits[0] + "\n   " + " · ".join(rules)


class TelegramCommands:
    def __init__(self, token: str, chat_id, watches_path: str, engine=None, store=None,
                 client: Optional[httpx.AsyncClient] = None, api_base="https://api.telegram.org"):
        self.api = f"{api_base}/bot{token}"
        self.chat_id = str(chat_id)
        self.watches_path = watches_path
        self.engine = engine
        self.store = store
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(40))
        self.offset = None

    async def reply(self, text: str):
        await self.client.post(f"{self.api}/sendMessage", json={
            "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True})

    async def handle(self, text: str) -> str:
        text = text.strip()
        cmd, _, args = text.partition(" ")
        cmd = cmd.split("@", 1)[0].lower()
        watches = load_watches(self.watches_path)
        ids = {w.id for w in watches}

        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/add":
            w = parse_add(args, ids)
            watches.append(w)
            save_watches(watches, self.watches_path)
            if self.engine:
                self.engine.reload_watches(force=True)
            return ("✅ Watching now (existing listings are skipped, you'll only hear about new "
                    "ones):\n" + describe(w))
        if cmd == "/list":
            if not watches:
                return "No watches yet. Try: <code>/add stone island jacket max=120</code>"
            return "\n\n".join(describe(w) for w in watches)
        if cmd in ("/remove", "/delete", "/pause", "/resume"):
            target = args.strip()
            match = [w for w in watches if w.id == target]
            if not match:
                return f"No watch with id <b>{target or '?'}</b>. Use /list to see ids."
            if cmd in ("/remove", "/delete"):
                watches = [w for w in watches if w.id != target]
                msg = f"🗑 Removed <b>{target}</b>"
            else:
                match[0].enabled = cmd == "/resume"
                msg = f"{'▶️ Resumed' if match[0].enabled else '⏸ Paused'} <b>{target}</b>"
            save_watches(watches, self.watches_path)
            if self.engine:
                self.engine.reload_watches(force=True)
            return msg
        if cmd == "/status":
            return self.engine.status_text() if self.engine else "engine not running"
        if cmd == "/recent":
            if not self.store:
                return "no store"
            rows = self.store.recent_alerts(10)
            if not rows:
                return "No alerts yet."
            return "\n".join(f"• [{r[0]}] {r[1][:50]} — {r[2]:g} {r[3]}\n  {r[4]}" for r in rows)
        return "Unknown command. /help"

    async def poll_forever(self):
        backoff = 1
        while True:
            try:
                params = {"timeout": 30, "allowed_updates": '["message"]'}
                if self.offset is not None:
                    params["offset"] = self.offset
                r = await self.client.get(f"{self.api}/getUpdates", params=params)
                data = r.json()
                if not data.get("ok"):
                    raise RuntimeError(data.get("description", r.status_code))
                for upd in data.get("result", []):
                    self.offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    if str((msg.get("chat") or {}).get("id")) != self.chat_id:
                        continue  # ignore strangers
                    text = msg.get("text") or ""
                    if not text.startswith("/"):
                        continue
                    try:
                        out = await self.handle(text)
                    except ValueError as e:
                        out = f"⚠️ {e}"
                    except Exception as e:
                        log.exception("telegram command failed")
                        out = f"⚠️ error: {e}"
                    await self.reply(out)
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("telegram command polling: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)


async def discover_chat_id(token: str, client: Optional[httpx.AsyncClient] = None,
                           api_base="https://api.telegram.org") -> Optional[tuple[str, str]]:
    """Return (chat_id, name) of the most recent person who messaged the bot."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=20)
    try:
        r = await client.get(f"{api_base}/bot{token}/getUpdates")
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "bad token?"))
        for upd in reversed(data.get("result", [])):
            chat = (upd.get("message") or {}).get("chat")
            if chat:
                return str(chat["id"]), chat.get("first_name") or chat.get("title") or ""
        return None
    finally:
        if own:
            await client.aclose()
