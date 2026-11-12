"""Alert delivery. Every notifier gets the same Listing + Watch and formats it for its channel.

Recommended: Telegram (free, instant phone push, photo + one-tap "Buy" button that opens
the Depop/Vinted app). ntfy is the zero-account alternative. Discord + Windows toast also
supported. Enable as many as you like; they are sent in parallel.
"""
from __future__ import annotations

import asyncio
import html
import logging
import sys
import time
from typing import Optional

import httpx

from .models import Listing, Watch

log = logging.getLogger(__name__)

CURRENCY_SYMBOL = {"GBP": "£", "EUR": "€", "USD": "$", "AUD": "A$", "CAD": "C$", "PLN": "zł"}
PLATFORM_EMOJI = {"vinted": "🟢", "depop": "🔴"}


def money(amount: Optional[float], currency: str) -> str:
    if amount is None:
        return "?"
    sym = CURRENCY_SYMBOL.get((currency or "").upper())
    txt = f"{amount:,.2f}" if round(amount % 1, 2) else f"{amount:,.0f}"
    return f"{sym}{txt}" if sym else f"{txt} {currency}"


def age_text(ts: Optional[float], now: Optional[float] = None) -> str:
    if not ts:
        return ""
    s = max(0, int((now or time.time()) - ts))
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60} min ago"
    return f"{s // 3600} h ago"


def summary_lines(l: Listing) -> list[str]:
    price = money(l.price, l.currency)
    if l.total_price and abs(l.total_price - l.price) > 0.009:
        price += f" ({money(l.total_price, l.currency)} incl. fees)"
    bits = [price]
    if l.size:
        bits.append(f"Size {l.size}")
    if l.condition_raw:
        bits.append(l.condition_raw.replace("_", " ").title() if l.platform == "depop" else l.condition_raw)
    second = []
    if l.brand:
        second.append(l.brand)
    if l.seller:
        second.append(f"@{l.seller}")
    ago = age_text(l.created_ts)
    if ago:
        second.append(f"listed {ago}")
    return [" · ".join(bits)] + ([" · ".join(second)] if second else [])


class Notifier:
    name = "base"

    async def send(self, listing: Listing, watch: Watch) -> None:
        raise NotImplementedError

    async def send_text(self, text: str) -> None:
        """Plain status message (startup, blocked warnings). Optional."""

    async def close(self):
        pass


class ConsoleNotifier(Notifier):
    name = "console"

    async def send(self, l: Listing, w: Watch):
        line = " | ".join(summary_lines(l))
        print(f"\n🔔 [{w.id}] {PLATFORM_EMOJI.get(l.platform, '')} {l.platform.upper()}: {l.title}\n"
              f"   {line}\n   {l.url}\n", flush=True)

    async def send_text(self, text: str):
        print(f"ℹ️  {text}", flush=True)


class _HttpNotifier(Notifier):
    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._own = client is None
        self.client = client or httpx.AsyncClient(timeout=15)

    async def close(self):
        if self._own:
            await self.client.aclose()


class TelegramNotifier(_HttpNotifier):
    name = "telegram"

    def __init__(self, bot_token: str, chat_id, client=None, api_base="https://api.telegram.org"):
        super().__init__(client)
        self.token = bot_token
        self.chat_id = str(chat_id)
        self.api = f"{api_base}/bot{bot_token}"

    def _caption(self, l: Listing, w: Watch) -> str:
        e = html.escape
        lines = [f"{PLATFORM_EMOJI.get(l.platform, '')} <b>{e(l.title or 'New listing')}</b>"]
        lines += [e(x) for x in summary_lines(l)]
        lines.append(f"🔎 <i>{e(w.id)}</i>")
        return "\n".join(lines)[:1000]

    def _markup(self, l: Listing) -> dict:
        label = f"🛒 Buy on {l.platform.title()} — {money(l.price, l.currency)}"
        return {"inline_keyboard": [[{"text": label, "url": l.url}]]}

    async def _post(self, method: str, payload: dict) -> httpx.Response:
        """POST with one retry on Telegram's flood limit (429 + retry_after)."""
        r = await self.client.post(f"{self.api}/{method}", json=payload)
        if r.status_code == 429:
            try:
                wait = float(r.json().get("parameters", {}).get("retry_after", 1))
            except Exception:
                wait = 1.0
            await asyncio.sleep(min(wait, 30))
            r = await self.client.post(f"{self.api}/{method}", json=payload)
        return r

    async def send(self, l: Listing, w: Watch):
        cap, mk = self._caption(l, w), self._markup(l)
        if l.image_url:
            r = await self._post("sendPhoto", {
                "chat_id": self.chat_id, "photo": l.image_url, "caption": cap,
                "parse_mode": "HTML", "reply_markup": mk})
            if r.status_code == 200:
                return
            log.debug("telegram sendPhoto failed (%s), falling back to text", r.status_code)
        r = await self._post("sendMessage", {
            "chat_id": self.chat_id, "text": cap, "parse_mode": "HTML",
            "reply_markup": mk, "link_preview_options": {"url": l.url, "prefer_large_media": True}})
        if r.status_code != 200:
            raise RuntimeError(f"telegram: HTTP {r.status_code} {r.text[:200]}")

    async def send_text(self, text: str):
        await self.client.post(f"{self.api}/sendMessage", json={
            "chat_id": self.chat_id, "text": text, "disable_web_page_preview": True})


class DiscordNotifier(_HttpNotifier):
    name = "discord"
    COLORS = {"vinted": 0x09B1BA, "depop": 0xFF2300}

    def __init__(self, webhook_url: str, mention: str = "", client=None):
        super().__init__(client)
        self.url = webhook_url
        self.mention = mention  # e.g. "@here" or "<@123456789>"

    async def send(self, l: Listing, w: Watch):
        lines = summary_lines(l)
        embed = {
            "title": (l.title or "New listing")[:250],
            "url": l.url,
            "description": "\n".join(lines) + f"\n\n**[🛒 Buy on {l.platform.title()}]({l.url})**",
            "color": self.COLORS.get(l.platform, 0x5865F2),
            "footer": {"text": f"{l.platform.title()} · watch: {w.id}"},
        }
        if l.image_url:
            embed["image"] = {"url": l.image_url}
        if l.created_ts:
            embed["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(l.created_ts))
        payload = {"content": f"{self.mention} {l.url}".strip() if self.mention else None,
                   "embeds": [embed]}
        r = await self.client.post(self.url, json=payload)
        if r.status_code == 429:
            await asyncio.sleep(float(r.json().get("retry_after", 1)))
            r = await self.client.post(self.url, json=payload)
        if r.status_code >= 300:
            raise RuntimeError(f"discord: HTTP {r.status_code} {r.text[:200]}")

    async def send_text(self, text: str):
        await self.client.post(self.url, json={"content": text})


class NtfyNotifier(_HttpNotifier):
    """https://ntfy.sh - install the ntfy app, subscribe to your (secret) topic. No account."""
    name = "ntfy"

    def __init__(self, topic: str, server: str = "https://ntfy.sh", token: str = "",
                 priority: int = 5, client=None):
        super().__init__(client)
        self.topic, self.server, self.token, self.priority = topic, server.rstrip("/"), token, priority

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def send(self, l: Listing, w: Watch):
        body = {
            "topic": self.topic,
            "title": f"{l.platform.title()}: {l.title}"[:200],
            "message": "\n".join(summary_lines(l)) + f"\n🔎 {w.id}",
            "click": l.url,            # tapping the notification opens the listing
            "priority": self.priority,
            "tags": ["shopping_cart"],
            "actions": [{"action": "view", "label": f"Buy {money(l.price, l.currency)}",
                         "url": l.url, "clear": True}],
        }
        if l.image_url:
            body["attach"] = l.image_url
        r = await self.client.post(self.server, json=body, headers=self._headers())
        if r.status_code >= 300:
            raise RuntimeError(f"ntfy: HTTP {r.status_code} {r.text[:200]}")

    async def send_text(self, text: str):
        await self.client.post(self.server, json={"topic": self.topic, "message": text,
                                                  "priority": 3}, headers=self._headers())


class DesktopNotifier(Notifier):
    """Windows 10/11 toast; clicking it opens the listing in your browser.
    Needs `pip install win11toast` (installed automatically by install.bat)."""
    name = "desktop"

    def __init__(self, sound: bool = True):
        self.sound = sound
        try:
            from win11toast import toast_async  # type: ignore
            self._toast = toast_async
        except Exception:
            self._toast = None
            if sys.platform == "win32":
                log.warning("desktop notifications need: pip install win11toast")

    async def send(self, l: Listing, w: Watch):
        if not self._toast:
            return
        kw = dict(on_click=l.url, button={"activationType": "protocol", "arguments": l.url,
                                          "content": "🛒 Buy now"})
        if l.image_url:
            kw["image"] = {"src": l.image_url, "placement": "hero"}
        if not self.sound:
            kw["audio"] = {"silent": "true"}
        # don't block the engine while the toast is on screen
        asyncio.create_task(self._toast(f"{l.platform.title()}: {l.title}"[:120],
                                        "\n".join(summary_lines(l)), **kw))

    async def send_text(self, text: str):
        if self._toast:
            asyncio.create_task(self._toast("Resale Sniper", text))


def build_notifiers(cfg: dict) -> list[Notifier]:
    n = cfg.get("notifications", {}) or {}
    out: list[Notifier] = []
    if n.get("console", True):
        out.append(ConsoleNotifier())
    tg = n.get("telegram") or {}
    if tg.get("enabled") and tg.get("bot_token") and tg.get("chat_id"):
        out.append(TelegramNotifier(tg["bot_token"], tg["chat_id"],
                                    api_base=tg.get("api_base", "https://api.telegram.org")))
    dc = n.get("discord") or {}
    if dc.get("enabled") and dc.get("webhook_url"):
        out.append(DiscordNotifier(dc["webhook_url"], dc.get("mention", "")))
    nt = n.get("ntfy") or {}
    if nt.get("enabled") and nt.get("topic"):
        out.append(NtfyNotifier(nt["topic"], nt.get("server", "https://ntfy.sh"),
                                nt.get("token", ""), int(nt.get("priority", 5))))
    dk = n.get("desktop") or {}
    if dk.get("enabled"):
        out.append(DesktopNotifier(dk.get("sound", True)))
    return out


async def broadcast(notifiers: list[Notifier], listing: Listing, watch: Watch) -> dict:
    """Send to every channel concurrently. Returns {name: error or None}."""
    async def one(n):
        try:
            await n.send(listing, watch)
            return n.name, None
        except Exception as e:
            log.error("notify via %s failed: %s", n.name, e)
            return n.name, str(e)
    return dict(await asyncio.gather(*(one(n) for n in notifiers)))


async def broadcast_text(notifiers: list[Notifier], text: str):
    async def one(n):
        try:
            await n.send_text(text)
        except Exception as e:
            log.debug("status via %s failed: %s", n.name, e)
    await asyncio.gather(*(one(n) for n in notifiers))
