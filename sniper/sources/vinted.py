"""Vinted catalog search.

Vinted's web app (2025/26) loads search results from
    GET https://api.<domain>/svc-catalogue/items
            ?search_text=..&order=newest_first&attribute_ids[status]=6,1,2 ...
using the anonymous `access_token_web` cookie (+ `anon_id`, CSRF token) it gets on the first
visit to the homepage. Older deployments used  GET https://www.<domain>/api/v2/catalog/items
with `status_ids[]` params. We try the modern endpoint first and fall back automatically.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

import httpx

from ..models import Listing, Watch, normalize_condition
from .base import BlockedError, Source, SourceError, to_float

log = logging.getLogger(__name__)

# Vinted condition ids (attribute_ids[status] / status_ids[])
VINTED_STATUS_IDS = {"new_with_tags": 6, "new": 1, "very_good": 2, "good": 3, "fair": 4}
# Vinted has no "like new" tier; closest server-side match is "new without tags".
VINTED_STATUS_IDS_EXTRA = {"like_new": 1}
# watch.vinted extra keys -> modern attribute name
ATTR_ALIASES = {"catalog_ids": "catalog", "brand_ids": "brand", "size_ids": "size",
                "status_ids": "status", "color_ids": "color", "material_ids": "material"}

LOCALES = {"co.uk": "en-GB", "com": "en-US", "fr": "fr-FR", "de": "de-DE", "it": "it-IT",
           "es": "es-ES", "nl": "nl-NL", "be": "fr-BE", "pl": "pl-PL", "pt": "pt-PT",
           "ie": "en-IE", "at": "de-AT", "lt": "lt-LT", "cz": "cs-CZ", "se": "sv-SE"}
CSRF_RE = re.compile(r'CSRF_TOKEN\\?"\s*:\s*\\?"([A-Za-z0-9_\-:.=+/]{8,})')


class VintedSource(Source):
    name = "vinted"

    def __init__(self, cfg: dict, client: Optional[httpx.AsyncClient] = None):
        super().__init__(cfg, client)
        self.domain = self.cfg.get("domain", "www.vinted.co.uk")
        host = self.domain if self.domain.startswith("www.") else "www." + self.domain
        self.base = (self.cfg.get("base_url") or f"https://{host}").rstrip("/")
        api_host = "api." + host[4:]
        self.api_base = (self.cfg.get("api_base_url") or self.cfg.get("base_url")
                         or f"https://{api_host}").rstrip("/")
        tld = host.split("vinted.", 1)[-1]
        self.locale = self.cfg.get("locale") or LOCALES.get(tld, "en-GB")
        self.per_page = int(self.cfg.get("per_page", 30))
        self.currency = self.cfg.get("currency")
        self.session_ttl = float(self.cfg.get("session_ttl_seconds", 1800))
        mode = self.cfg.get("api", "auto")  # auto | modern | legacy
        self.modes = {"auto": ["modern", "legacy"], "modern": ["modern"],
                      "legacy": ["legacy"]}.get(mode, ["modern", "legacy"])
        self._good_mode: Optional[str] = None
        self._session_at = 0.0
        self.cookies: dict[str, str] = {}
        self.csrf: Optional[str] = None

    # ---------- session ----------
    async def _ensure_session(self, force: bool = False):
        if not force and self._session_at and time.time() - self._session_at < self.session_ttl:
            return
        self.client.cookies.clear()
        try:
            r = await self.client.get(self.base + "/", headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": f"{self.locale},en;q=0.5",
                "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1"})
        except httpx.HTTPError as e:
            raise SourceError(f"vinted session: {e!r}") from e
        self._raise_for(r, "vinted session")
        self.cookies = {c.name: c.value for c in self.client.cookies.jar}
        # we send cookies explicitly (the API lives on a different host than the homepage)
        self.client.cookies.clear()
        m = CSRF_RE.search(r.text or "")
        self.csrf = m.group(1) if m else None
        if "access_token_web" not in self.cookies:
            log.warning("vinted: homepage did not set access_token_web (bot check / blocked IP?)")
        self._session_at = time.time()

    def _api_headers(self, watch: Watch) -> dict:
        h = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": f"{self.locale},en;q=0.5",
            "Cache-Control": "no-cache", "Pragma": "no-cache",
            "Origin": self.base, "Referer": self.base + "/",
            "Locale": self.locale, "Platform": "web", "X-Next-App": "marketplace-web",
            "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-site",
        }
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if self.csrf:
            h["X-Csrf-Token"] = self.csrf
        if self.cookies.get("anon_id"):
            h["X-Anon-Id"] = self.cookies["anon_id"]
        return h

    # ---------- params ----------
    def _status_ids(self, watch: Watch) -> list[int]:
        allowed = watch.allowed_conditions()
        if not allowed:
            return []
        return sorted({VINTED_STATUS_IDS.get(c) or VINTED_STATUS_IDS_EXTRA.get(c)
                       for c in allowed} - {None})

    def _common(self, watch: Watch) -> list[tuple[str, str]]:
        p = [("search_text", watch.query), ("order", "newest_first"),
             ("per_page", str(self.per_page)), ("page", "1")]
        if watch.min_price is not None:
            p.append(("price_from", f"{watch.min_price:g}"))
        if watch.max_price is not None and not watch.use_total_price:
            p.append(("price_to", f"{watch.max_price:g}"))
        if self.currency:
            p.append(("currency", self.currency))
        return p

    def build_params(self, watch: Watch, mode: str = "legacy") -> list[tuple[str, str]]:
        p = self._common(watch)
        ids = self._status_ids(watch)
        extra = dict(watch.vinted or {})
        if mode == "modern":
            attrs: dict[str, list] = {}
            if ids:
                attrs["status"] = ids
            for k, v in extra.items():
                name = ATTR_ALIASES.get(k.rstrip("[]"), None)
                vals = v if isinstance(v, list) else [v]
                if name:
                    attrs.setdefault(name, []).extend(vals)
                else:
                    p.append((k, ",".join(map(str, vals))))
            for name, vals in attrs.items():
                p.append((f"attribute_ids[{name}]", ",".join(map(str, vals))))
        else:
            for i in ids:
                p.append(("status_ids[]", str(i)))
            for k, v in extra.items():
                key = k if k.endswith("[]") else f"{k}[]" if isinstance(v, list) else k
                for item in (v if isinstance(v, list) else [v]):
                    p.append((key, str(item)))
        return p

    def _url(self, mode: str) -> str:
        return (self.api_base + "/svc-catalogue/items") if mode == "modern" \
            else (self.base + "/api/v2/catalog/items")

    # ---------- search ----------
    async def _fetch(self, watch: Watch, mode: str) -> httpx.Response:
        url, params = self._url(mode), self.build_params(watch, mode)
        try:
            r = await self.client.get(url, params=params, headers=self._api_headers(watch))
            if r.status_code == 401:
                await self._ensure_session(force=True)
                r = await self.client.get(url, params=params, headers=self._api_headers(watch))
        except httpx.HTTPError as e:
            raise SourceError(f"vinted search: {e!r}") from e
        return r

    async def search(self, watch: Watch) -> list[Listing]:
        await self._ensure_session()
        modes = [self._good_mode] if self._good_mode else self.modes
        last: Optional[Exception] = None
        for mode in modes:
            r = await self._fetch(watch, mode)
            if r.status_code in (404, 405, 410) and not self._good_mode and len(modes) > 1:
                last = SourceError(f"vinted {mode} endpoint: HTTP {r.status_code}")
                continue
            if r.status_code == 403:
                self._session_at = 0  # start a fresh session next time
            try:
                self._raise_for(r, f"vinted search ({mode})")
                data = self._json(r, f"vinted search ({mode})")
            except BlockedError as e:
                if not self._good_mode and mode != modes[-1]:
                    last = e
                    continue
                raise
            items = data.get("items") if isinstance(data, dict) else None
            if items is None:
                last = SourceError(f"vinted {mode}: unexpected payload keys "
                                   f"{list(data)[:8] if isinstance(data, dict) else type(data)}")
                continue
            if self._good_mode != mode:
                log.info("vinted: using %s endpoint %s", mode, self._url(mode))
            self._good_mode = mode
            return self._parse_all(items)
        self._good_mode = None
        raise last or SourceError("vinted: no endpoint worked")

    def _parse_all(self, items) -> list[Listing]:
        out = []
        for it in items:
            try:
                out.append(self.parse_item(it))
            except Exception as e:  # never let one weird item kill the poll
                log.debug("vinted: skip unparsable item %s: %r",
                          it.get("id") if isinstance(it, dict) else it, e)
        return out

    def parse_item(self, it: dict) -> Listing:
        price_raw = it.get("price")
        price = to_float(price_raw)
        currency = (price_raw.get("currency_code") if isinstance(price_raw, dict) else None) \
            or it.get("currency") or "GBP"
        total = to_float(it.get("total_item_price"))
        photo = it.get("photo") or ((it.get("photos") or [None])[0]) or {}
        img = photo.get("full_size_url") or photo.get("url") or ""
        ts = (photo.get("high_resolution") or {}).get("timestamp")
        url = it.get("url") or (self.base + it["path"] if it.get("path") else
                                f"{self.base}/items/{it['id']}")
        if url.startswith("/"):
            url = self.base + url
        cond_raw = it.get("status") or ""
        if isinstance(cond_raw, dict):
            cond_raw = cond_raw.get("title") or cond_raw.get("name") or ""
        user = it.get("user") or {}
        brand = it.get("brand_title") or ((it.get("brand") or {}).get("title")
                                          if isinstance(it.get("brand"), dict) else "") or ""
        return Listing(
            platform="vinted",
            id=str(it["id"]),
            title=it.get("title") or "",
            url=url,
            price=price if price is not None else 0.0,
            currency=currency,
            total_price=total,
            brand=brand,
            size=it.get("size_title") or "",
            condition=normalize_condition(cond_raw),
            condition_raw=str(cond_raw),
            image_url=img,
            description=it.get("description") or "",
            seller=user.get("login") or "",
            created_ts=float(ts) if ts else None,
            promoted=bool(it.get("promoted") or it.get("is_promoted")),
        )
