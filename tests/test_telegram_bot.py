import json

import httpx
import pytest

from sniper.config import load_watches, save_watches
from sniper.models import Watch
from sniper.telegram_bot import TelegramCommands, parse_add


def test_parse_add_full():
    w = parse_add('nike dunk low max=60 min=20 size=9,9.5 cond=good+ not=kids,gs on=vinted', set())
    assert (w.query, w.max_price, w.min_price) == ("nike dunk low", 60, 20)
    assert w.sizes == ["9", "9.5"] and w.min_condition == "good" and w.exclude == ["kids", "gs"]
    assert w.platforms == ["vinted"] and w.id == "nike-dunk-low"


def test_parse_add_shorthand_and_unique_ids():
    w = parse_add("stone island £120 cond=new,nwt", {"stone-island"})
    assert w.max_price == 120 and w.conditions == ["new", "new_with_tags"] and w.id == "stone-island-2"
    w = parse_add('"north face" nuptse id=puffer', set())
    assert w.query == "north face nuptse" and w.id == "puffer"
    with pytest.raises(ValueError):
        parse_add("max=50", set())
    with pytest.raises(ValueError):
        parse_add("shoes cond=mint+", set())


async def test_command_flow(tmp_path):
    wp = str(tmp_path / "w.yaml")
    bot = TelegramCommands("T", 7, wp, client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"ok": True}))))
    out = await bot.handle("/add carhartt jacket max=40 size=L")
    assert "Watching now" in out
    assert [w.id for w in load_watches(wp)] == ["carhartt-jacket"]
    assert "price 0–40" in await bot.handle("/list")
    assert "Paused" in await bot.handle("/pause carhartt-jacket")
    assert load_watches(wp)[0].enabled is False
    assert "Resumed" in await bot.handle("/resume carhartt-jacket")
    assert "No watch" in await bot.handle("/remove nope")
    assert "Removed" in await bot.handle("/remove carhartt-jacket")
    assert load_watches(wp) == []


async def test_poll_ignores_strangers(tmp_path):
    wp = str(tmp_path / "w.yaml")
    sent = []
    calls = {"n": 0}

    def h(req):
        if req.url.path.endswith("getUpdates"):
            calls["n"] += 1
            if calls["n"] > 1:
                raise httpx.ConnectError("stop")
            return httpx.Response(200, json={"ok": True, "result": [
                {"update_id": 1, "message": {"chat": {"id": 999}, "text": "/add evil max=1"}},
                {"update_id": 2, "message": {"chat": {"id": 7}, "text": "/add good thing"}},
            ]})
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})

    bot = TelegramCommands("T", 7, wp, client=httpx.AsyncClient(transport=httpx.MockTransport(h)))
    import asyncio
    task = asyncio.create_task(bot.poll_forever())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if sent:
            break
    task.cancel()
    assert [w.query for w in load_watches(wp)] == ["good thing"]
    assert bot.offset == 3 and len(sent) == 1 and sent[0]["chat_id"] == "7"


def test_yaml_roundtrip(tmp_path):
    p = tmp_path / "w.yaml"
    ws = [Watch.from_dict({"id": "a", "query": "x", "max_price": 5, "sizes": ["M"],
                           "vinted": {"catalog_ids": [1]}})]
    save_watches(ws, p)
    assert load_watches(p) == ws


def test_parse_add_any_of():
    w = parse_add("coach brand=coach any=bag,purse,tote max=200", set())
    assert w.any_of == ["bag", "purse", "tote"] and w.brands == ["coach"]
