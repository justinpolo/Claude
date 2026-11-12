import json
import time

import httpx

from sniper.models import Listing, Watch
from sniper.notifiers import (DiscordNotifier, NtfyNotifier, TelegramNotifier, age_text,
                              broadcast, money)


def sample():
    return (Listing(platform="depop", id="1", title="Carhartt <Detroit> & Jacket",
                    url="https://www.depop.com/products/x-carhartt/", price=85.0, currency="GBP",
                    brand="Carhartt", size="L", condition="very_good", condition_raw="used_excellent",
                    seller="vintagefinds", image_url="https://img/1.jpg", created_ts=time.time() - 20),
            Watch.from_dict({"id": "carhartt", "query": "carhartt detroit"}))


def recorder():
    reqs = []

    def h(req):
        reqs.append(req)
        return httpx.Response(200, json={"ok": True})
    return reqs, httpx.AsyncClient(transport=httpx.MockTransport(h))


async def test_telegram_photo_with_buy_button():
    reqs, c = recorder()
    l, w = sample()
    await TelegramNotifier("TOKEN", 42, client=c).send(l, w)
    [r] = reqs
    assert r.url.path == "/botTOKEN/sendPhoto"
    body = json.loads(r.content)
    assert body["chat_id"] == "42" and body["photo"] == "https://img/1.jpg"
    assert "&lt;Detroit&gt; &amp;" in body["caption"]           # HTML-escaped
    assert "£85" in body["caption"] and "Used Excellent" in body["caption"]
    btn = body["reply_markup"]["inline_keyboard"][0][0]
    assert btn["url"] == l.url and btn["text"].startswith("🛒 Buy on Depop")


async def test_telegram_falls_back_to_text_when_photo_fails():
    reqs = []

    def h(req):
        reqs.append(req)
        return httpx.Response(400 if req.url.path.endswith("sendPhoto") else 200, json={})
    l, w = sample()
    await TelegramNotifier("T", 1, client=httpx.AsyncClient(transport=httpx.MockTransport(h))).send(l, w)
    assert [r.url.path.rsplit("/", 1)[1] for r in reqs] == ["sendPhoto", "sendMessage"]


async def test_ntfy_payload_click_opens_listing():
    reqs, c = recorder()
    l, w = sample()
    await NtfyNotifier("my-secret-topic", client=c).send(l, w)
    body = json.loads(reqs[0].content)
    assert body["topic"] == "my-secret-topic" and body["click"] == l.url
    assert body["attach"] == l.image_url and body["actions"][0]["url"] == l.url
    assert body["priority"] == 5


async def test_discord_embed():
    reqs, c = recorder()
    l, w = sample()
    await DiscordNotifier("https://discord.com/api/webhooks/1/abc", mention="@here", client=c).send(l, w)
    body = json.loads(reqs[0].content)
    e = body["embeds"][0]
    assert e["url"] == l.url and e["image"]["url"] == l.image_url and "Buy on Depop" in e["description"]
    assert body["content"].startswith("@here")


async def test_broadcast_isolates_failures_and_retries():
    class Bad:
        name = "bad"
        calls = 0
        async def send(self, l, w):
            Bad.calls += 1
            raise RuntimeError("down")

    class Flaky:
        name = "flaky"
        calls = 0
        async def send(self, l, w):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise RuntimeError("blip")
    reqs, c = recorder()
    l, w = sample()
    res = await broadcast([Bad(), Flaky(), NtfyNotifier("t", client=c)], l, w, retry_delay=0)
    assert res == {"bad": "down", "flaky": None, "ntfy": None}
    assert len(reqs) == 1 and Bad.calls == 3 and Flaky.calls == 2   # good channel not re-sent


def test_formatting():
    assert money(45, "GBP") == "£45" and money(12.5, "EUR") == "€12.50" and money(3, "SEK") == "3 SEK"
    now = 1000
    assert age_text(now - 12, now) == "12s ago" and age_text(now - 600, now) == "10 min ago"


async def test_telegram_retries_flood_limit(monkeypatch):
    import sniper.notifiers as nm
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        if n["c"] == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 2}})
        return httpx.Response(200, json={"ok": True})
    slept = []

    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr(nm.asyncio, "sleep", fake_sleep)
    l, w = sample()
    await TelegramNotifier("T", 1, client=httpx.AsyncClient(transport=httpx.MockTransport(h))).send(l, w)
    assert n["c"] == 2 and slept == [2.0]
