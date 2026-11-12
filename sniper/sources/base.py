"""Shared HTTP plumbing for marketplace sources."""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from ..models import Listing, Watch

log = logging.getLogger(__name__)

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class SourceError(Exception):
    """Temporary failure (network, 5xx, bad JSON). Retry later."""


class BlockedError(SourceError):
    """The site refused us (403/429). Back off hard."""

    def __init__(self, msg: str, retry_after: Optional[float] = None):
        super().__init__(msg)
        self.retry_after = retry_after


class Source:
    name = "base"

    def __init__(self, cfg: dict, client: Optional[httpx.AsyncClient] = None):
        self.cfg = cfg or {}
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.cfg.get("timeout", 20)),
            follow_redirects=True,
            headers={
                "User-Agent": self.cfg.get("user_agent", DEFAULT_UA),
                "Accept-Language": self.cfg.get("accept_language", "en-GB,en;q=0.9"),
            },
            http2=False,
        )

    async def search(self, watch: Watch) -> list[Listing]:
        raise NotImplementedError

    async def close(self):
        if self._own_client:
            await self.client.aclose()

    @staticmethod
    def _raise_for(resp: httpx.Response, what: str):
        if resp.status_code in (403, 429):
            ra = resp.headers.get("retry-after")
            try:
                ra = float(ra) if ra else None
            except ValueError:
                ra = None
            raise BlockedError(f"{what}: HTTP {resp.status_code}", retry_after=ra)
        if resp.status_code >= 400:
            raise SourceError(f"{what}: HTTP {resp.status_code}")

    @staticmethod
    def _json(resp: httpx.Response, what: str):
        ctype = resp.headers.get("content-type", "")
        try:
            return resp.json()
        except ValueError:
            snippet = resp.text[:120].replace("\n", " ")
            # an HTML page instead of JSON almost always means a bot wall / captcha
            if "html" in ctype or snippet.lstrip().startswith("<"):
                raise BlockedError(f"{what}: got an HTML page instead of JSON (bot check?)")
            raise SourceError(f"{what}: invalid JSON: {snippet!r}")


def to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for k in ("amount", "priceAmount", "price_amount", "value", "total_price"):
            if k in v:
                return to_float(v[k])
        return None
    s = str(v).replace(",", ".").strip()
    s = "".join(ch for ch in s if ch.isdigit() or ch == ".")
    try:
        return float(s) if s else None
    except ValueError:
        return None
