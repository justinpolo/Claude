import asyncio
import json

import httpx
import pytest

from sniper.config import DEFAULTS, save_watches
from sniper.engine import Engine
from sniper.models import Listing, Watch
from sniper.notifiers import Notifier
from sniper.sources import BlockedError, Source, SourceError
from sniper.store import Store


class FakeSource(Source):
    def __init__(self, name, pages):
        self.name = name
        self.pages = pages  # list of list[Listing] or Exception, one per call
        self.calls = 0

    async def search(self, watch):
        p = self.pages[min(self.calls, len(self.pages) - 1)]
        self.calls += 1
        if isinstance(p, Exception):
            raise p
        return list(p)

    async def close(self):
        pass


class Capture(Notifier):
    name = "capture"

    def __init__(self):
        self.alerts, self.texts = [], []

    async def send(self, l, w):
        self.alerts.append((w.id, l.key))

    async def send_text(self, t):
        self.texts.append(t)


def lst(i, price=30, title="Nike Dunk Low", platform="vinted", cond="very_good", size="UK 9"):
    return Listing(platform=platform, id=str(i), title=title, url=f"https://x/{i}", price=price,
                   condition=cond, condition_raw=cond, size=size)


def W(**kw):
    return Watch.from_dict({"id": "dunks", "query": "nike dunk", "max_price": 50, **kw})


def make(tmp_path, sources, watches, **cfg):
    c = json.loads(json.dumps(DEFAULTS))
    c.update(cfg)
    cap = Capture()
    eng = Engine(c, sources, [cap], Store(str(tmp_path / "t.db")), watches=watches)
    return eng, cap


async def test_baseline_then_only_new_matching_items(tmp_path):
    src = FakeSource("vinted", [
        [lst(1), lst(2)],                                   # startup: already live -> silent
        [lst(3), lst(1), lst(2)],                           # new matching item
        [lst(5, price=80), lst(4, title="kids dunk"), lst(3), lst(1)],  # too expensive / excluded
        [lst(6, cond="fair"), lst(7), lst(3)],              # bad condition / match
    ])
    w = W(exclude=["kids"], min_condition="good")
    eng, cap = make(tmp_path, {"vinted": src}, [w])
    for _ in range(4):
        await eng.poll_watch("vinted", w)
    assert cap.alerts == [("dunks", "vinted:3"), ("dunks", "vinted:7")]


async def test_alert_on_start(tmp_path):
    src = FakeSource("vinted", [[lst(1), lst(2, price=99)]])
    eng, cap = make(tmp_path, {"vinted": src}, [W()], alert_on_start=True)
    await eng.poll_watch("vinted", W())
    assert cap.alerts == [("dunks", "vinted:1")]


async def test_no_duplicate_alert_across_watches_and_restarts(tmp_path):
    src = FakeSource("vinted", [[], [lst(9)]])
    a, b = W(id="a"), W(id="b", max_price=40)
    eng, cap = make(tmp_path, {"vinted": src}, [a, b])
    await eng.poll_watch("vinted", a)
    await eng.poll_watch("vinted", b)     # both baselined (src returns [] then [9])
    src.pages = [[lst(9)]]
    await eng.poll_watch("vinted", a)
    await eng.poll_watch("vinted", b)
    assert cap.alerts == [("a", "vinted:9")]
    # restart: new engine, same DB -> nothing re-alerted
    eng2 = Engine(eng.cfg, {"vinted": src}, [cap], Store(str(tmp_path / "t.db")), watches=[a])
    await eng2.poll_watch("vinted", a)
    assert len(cap.alerts) == 1


async def test_editing_search_rebaselines(tmp_path):
    src = FakeSource("vinted", [[lst(1)], [lst(1), lst(2)]])
    eng, cap = make(tmp_path, {"vinted": src}, [W()])
    await eng.poll_watch("vinted", W())
    w2 = W(query="nike dunk low")             # different search -> new baseline, no flood
    await eng.poll_watch("vinted", w2)
    assert cap.alerts == []


async def test_backoff_and_recovery_messages(tmp_path):
    src = FakeSource("vinted", [BlockedError("HTTP 403")] * 5 + [[lst(1)]])
    eng, cap = make(tmp_path, {"vinted": src}, [W()])
    t = [1000.0]
    eng.clock = lambda: t[0]
    delays = []
    for _ in range(5):
        try:
            await eng.poll_watch("vinted", W())
        except SourceError as e:
            await eng._record_failure("vinted", e)
            delays.append(eng.health["vinted"].backoff_until - t[0])
    assert delays == [30, 60, 120, 240, 480]
    assert any("failing" in x for x in cap.texts)
    await eng.poll_watch("vinted", W())
    await eng._record_ok("vinted")
    assert any("working again" in x for x in cap.texts)
    assert eng.health["vinted"].fail_streak == 0


async def test_full_loop_round_robin_and_hot_reload(tmp_path):
    """Run the real scheduler loop with a fake clock; edit watches.yaml mid-run."""
    wpath = tmp_path / "watches.yaml"
    save_watches([W(id="one", platforms=["vinted"])], wpath)
    feed = {"n": 0}

    class Growing(FakeSource):
        async def search(self, watch):
            self.calls += 1
            feed["n"] += 1
            return [lst(100 + i) for i in range(feed["n"])][::-1]  # one new item per call

    src = Growing("vinted", [])
    c = json.loads(json.dumps(DEFAULTS))
    cap = Capture()
    slept = []

    async def fake_sleep(s):
        slept.append(s)
        if len(slept) == 3:
            save_watches([W(id="one", platforms=["vinted"]), W(id="two", platforms=["vinted"], max_price=10)], wpath)
            import os, time as _t
            os.utime(wpath, (_t.time() + 5, _t.time() + 5))
        if len(slept) >= 8:
            eng.stop()
            raise asyncio.CancelledError
        await asyncio.sleep(0)

    eng = Engine(c, {"vinted": src}, [cap], Store(str(tmp_path / "t.db")),
                 watches_path=str(wpath), sleep=fake_sleep)
    eng.reload_watches(force=True)
    with pytest.raises(asyncio.CancelledError):
        await eng.platform_loop("vinted")
    ids = [a[0] for a in cap.alerts]
    assert "one" in ids and "two" not in ids     # 'two' has max_price=10 -> never matches
    assert len(eng.watches) == 2                  # hot reload picked up the edit
    # gap = max(min_gap 3s, interval 6s / n_watches) +/-20%: 6s for 1 watch, 3s for 2
    assert 4.8 <= slept[0] <= 7.2 and 2.4 <= slept[-1] <= 3.6


async def test_broken_watches_file_keeps_old(tmp_path):
    wpath = tmp_path / "watches.yaml"
    save_watches([W()], wpath)
    eng, _ = make(tmp_path, {}, None)
    eng._static_watches = None
    eng.watches_path = str(wpath)
    eng.reload_watches(force=True)
    wpath.write_text("watches:\n  - id: x\n    query: y\n    colour: red\n")
    eng.reload_watches(force=True)
    assert [w.id for w in eng.watches] == ["dunks"]


async def test_heartbeat_logs_health(tmp_path, caplog):
    import logging
    eng, _ = make(tmp_path, {"vinted": FakeSource("vinted", [[]])}, [W()])
    eng.health["vinted"].ok_polls = 7
    eng.health["vinted"].last_ok = eng.clock()
    n = {"c": 0}

    async def fake_sleep(s):
        n["c"] += 1
        if n["c"] > 1:
            eng.stop()
    eng.sleep = fake_sleep
    with caplog.at_level(logging.INFO, logger="sniper.engine"):
        await eng.heartbeat()
    assert "alive" in caplog.text and "vinted: 7 checks OK" in caplog.text


async def test_old_items_resurfacing_in_feed_are_not_alerted(tmp_path):
    import time as _t
    now = _t.time()

    def V(i, age_min=None):
        x = lst(i)
        x.created_ts = now - age_min * 60 if age_min is not None else None
        return x
    src = FakeSource("vinted", [
        [V(500, 3), V(480, 50), V(470, 90)],          # baseline -> watermark 500
        [V(510, 1), V(300, 400), V(500, 3)],          # 510 new upload; 300 = old item bumped in
        [V(495), V(520), V(510, 1)],                  # 495 (no age, id below wm) old; 520 new
        [V(505, 4), V(520)],                          # 505 below wm but listed 4 min ago: draft published late
    ])
    eng, cap = make(tmp_path, {"vinted": src}, [W()])
    for _ in range(4):
        await eng.poll_watch("vinted", W())
    # strict default: only ids above the newest seen; 505 (4 min old) also exceeds the 3 min cap
    assert [k for _, k in cap.alerts] == ["vinted:510", "vinted:520"]
    med = eng.health["vinted"].detect_median()
    assert med is not None and 50 <= med <= 70          # 510 was seen ~1 min after upload
    assert "new uploads spotted ~" in eng.status_text()

    # optional grace window re-admits a late-published draft
    src.calls = 0
    (tmp_path / "g").mkdir()
    eng2, cap2 = make(tmp_path / "g", {"vinted": src}, [W()], fresh_grace_minutes=10,
                      max_listing_age_minutes=30)
    for _ in range(4):
        await eng2.poll_watch("vinted", W())
    assert [k for _, k in cap2.alerts] == ["vinted:510", "vinted:520", "vinted:505"]


async def test_adaptive_slowdown_and_recovery(tmp_path):
    eng, cap = make(tmp_path, {"vinted": FakeSource("vinted", [[]])}, [W()])
    base = eng._gap(1, "vinted")
    assert 4.8 <= base <= 7.2
    await eng._record_failure("vinted", BlockedError("HTTP 429"))
    await eng._record_failure("vinted", BlockedError("HTTP 429"))
    assert eng.health["vinted"].slow_factor == 4.0
    assert 19 <= eng._gap(1, "vinted") <= 29
    assert "slowed 4.0x" in eng.status_text()
    for _ in range(60):
        await eng._record_ok("vinted")
    assert abs(eng.health["vinted"].slow_factor - 4 / 1.5) < 1e-9
    await eng._record_failure("vinted", SourceError("timeout"))   # plain errors don't slow us
    assert abs(eng.health["vinted"].slow_factor - 4 / 1.5) < 1e-9


async def test_fresh_only_off_restores_old_behaviour(tmp_path):
    src = FakeSource("vinted", [[lst(500)], [lst(300), lst(500)]])
    eng, cap = make(tmp_path, {"vinted": src}, [W()], fresh_only=False)
    for _ in range(2):
        await eng.poll_watch("vinted", W())
    assert [k for _, k in cap.alerts] == ["vinted:300"]


async def test_first_item_on_search_that_started_empty_is_alerted(tmp_path):
    import time as _t
    x = lst(9001)
    x.created_ts = _t.time() - 5
    src = FakeSource("vinted", [[], [x]])     # rare item: nothing listed at baseline
    eng, cap = make(tmp_path, {"vinted": src}, [W()])
    await eng.poll_watch("vinted", W())
    await eng.poll_watch("vinted", W())
    assert cap.alerts == [("dunks", "vinted:9001")]
