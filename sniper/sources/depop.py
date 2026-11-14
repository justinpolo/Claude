"""Depop product search.

Depop's web app calls  GET https://webapi.depop.com/api/v3/search/products/
(older clients: /api/v2/search/products/). We try the configured endpoint
list in order and stick with the first one that answers with JSON.
Param names differ slightly between versions, so we send both spellings;
unknown params are ignored by the API, and every rule is re-checked locally.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

import httpx

from ..models import Listing, Watch, normalize_condition
from .base import BlockedError, Source, SourceError, to_float

log = logging.getLogger(__name__)

# v3 answers 410 "Deprecated endpoint" (confirmed Nov 2026); v2 and the search page answer
# 403 "Forbidden - Depop" to any non-browser client. Kept so the source starts working again
# automatically if Depop reopens access.
DEFAULT_ENDPOINTS = [
    "https://webapi.depop.com/api/v2/search/products/",
    "https://www.depop.com/search/",   # HTML page; products read from its embedded JSON
]
JSON_SCRIPT_RE = re.compile(
    r'<script[^>]*type="application/(?:ld\+)?json"[^>]*>(.*?)</script>', re.S | re.I)


def describe_response(r: httpx.Response) -> str:
    """One-line diagnosis of a response: status, who answered, and what kind of page it was."""
    h = r.headers
    bits = [f"HTTP {r.status_code}", h.get("content-type", "?").split(";")[0]]
    server = h.get("server", "")
    if server:
        bits.append(f"server={server}")
    if h.get("cf-mitigated"):
        bits.append(f"cf-mitigated={h['cf-mitigated']}")
    if h.get("cf-ray"):
        bits.append("via Cloudflare")
    if h.get("x-amzn-errortype") or h.get("x-amz-cf-id"):
        bits.append("via AWS/CloudFront")
    text = r.text[:3000] if r.content else ""
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    if m:
        bits.append(f"title={m.group(1).strip()[:60]!r}")
    elif text:
        bits.append(f"body={text[:100].strip()!r}")
    low = text.lower()
    if "just a moment" in low or "challenge-platform" in low or "cf-chl" in low:
        bits.append("=> Cloudflare bot challenge")
    elif "captcha" in low or "datadome" in low or "perimeterx" in low:
        bits.append("=> captcha/bot wall")
    return " · ".join(bits)


def find_product_lists(obj, depth: int = 0) -> list:
    """Walk arbitrary JSON and return the longest list of product-looking dicts."""
    best: list = []
    if depth > 14:
        return best
    if isinstance(obj, list):
        prods = [x for x in obj if isinstance(x, dict) and x.get("slug")
                 and ("price" in x or "pricing" in x)]
        if prods and len(prods) >= len(obj) / 2:
            best = prods
        for x in obj:
            sub = find_product_lists(x, depth + 1)
            if len(sub) > len(best):
                best = sub
    elif isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, (dict, list)):
                sub = find_product_lists(v, depth + 1)
                if len(sub) > len(best):
                    best = sub
    return best
DEPOP_CONDITIONS = {
    "new_with_tags": "brand_new",
    "new": "brand_new",
    "like_new": "used_like_new",
    "very_good": "used_excellent",
    "good": "used_good",
    "fair": "used_fair",
}


def _iso_to_ts(s) -> Optional[float]:
    if not s:
        return None
    if isinstance(s, (int, float)):
        return float(s) / (1000 if s > 1e12 else 1)
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class DepopSource(Source):
    name = "depop"

    def __init__(self, cfg: dict, client: Optional[httpx.AsyncClient] = None):
        super().__init__(cfg, client)
        self.country = self.cfg.get("country", "gb")
        self.currency = self.cfg.get("currency", "GBP")
        self.per_page = int(self.cfg.get("per_page", 24))
        eps = self.cfg.get("endpoints") or DEFAULT_ENDPOINTS
        self.endpoints = [eps] if isinstance(eps, str) else list(eps)
        self._good_endpoint: Optional[str] = None
        self.attempts: list[str] = []   # per-endpoint outcome of the last search (for `doctor`)

    def build_params(self, watch: Watch) -> list[tuple[str, str]]:
        p = [
            ("what", watch.query),
            ("sort", "newlyListed"),
            ("itemsPerPage", str(self.per_page)),
            ("items_per_page", str(self.per_page)),
            ("country", self.country),
            ("currency", self.currency),
        ]
        if watch.min_price is not None:
            p += [("priceMin", f"{watch.min_price:g}"), ("price_min", f"{watch.min_price:g}")]
        if watch.max_price is not None:
            p += [("priceMax", f"{watch.max_price:g}"), ("price_max", f"{watch.max_price:g}")]
        allowed = watch.allowed_conditions()
        if allowed:
            conds = ",".join(sorted({DEPOP_CONDITIONS[c] for c in allowed}))
            p.append(("conditions", conds))
        for k, v in (watch.depop or {}).items():
            p.append((k, ",".join(map(str, v)) if isinstance(v, list) else str(v)))
        return p

    def _note(self, ep: str, r: Optional[httpx.Response] = None, err: str = ""):
        if r is None:
            self.attempts.append(f"{ep} -> {err}")
            return
        self.attempts.append(f"{ep} -> {describe_response(r)}")

    async def search(self, watch: Watch) -> list[Listing]:
        self.attempts = []
        params = self.build_params(watch)
        headers = {"Accept": "application/json",
                   "Origin": "https://www.depop.com",
                   "Referer": "https://www.depop.com/"}
        endpoints = [self._good_endpoint] if self._good_endpoint else self.endpoints
        last_err: Optional[Exception] = None
        for ep in endpoints:
            if not ep.startswith("https://webapi.") and "/api/" not in ep:
                try:
                    return await self._search_page(ep, watch)
                except SourceError as e:
                    last_err = e
                    continue
            try:
                r = await self.client.get(ep, params=params, headers=headers)
            except httpx.HTTPError as e:
                last_err = SourceError(f"depop search: {e!r}")
                self._note(ep, err=repr(e))
                continue
            self._note(ep, r)
            if r.status_code in (404, 410) and not self._good_endpoint:
                last_err = SourceError(f"depop: endpoint {ep} gone ({r.status_code})")
                continue
            try:
                self._raise_for(r, "depop search")
                data = self._json(r, "depop search")
            except BlockedError as e:
                last_err = e
                self._good_endpoint = None   # re-probe every endpoint on the next attempt
                continue
            products = data.get("products")
            if products is None:
                products = data.get("objects") or data.get("items")
            if products is None:
                last_err = SourceError(f"depop: unexpected payload keys {list(data)[:8]}")
                continue
            self._good_endpoint = ep
            out = []
            for prod in products:
                try:
                    out.append(self.parse_product(prod))
                except Exception as e:
                    log.debug("depop: skip unparsable product %s: %r", prod.get("id"), e)
            return out
        self._good_endpoint = None
        raise last_err or SourceError("depop: no endpoint worked")

    async def _search_page(self, url: str, watch: Watch) -> list[Listing]:
        params = [("q", watch.query), ("sort", "newlyListed")]
        if watch.min_price is not None:
            params.append(("priceMin", f"{watch.min_price:g}"))
        if watch.max_price is not None:
            params.append(("priceMax", f"{watch.max_price:g}"))
        try:
            r = await self.client.get(url, params=params, headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
        except httpx.HTTPError as e:
            self._note(url, err=repr(e))
            raise SourceError(f"depop page: {e!r}") from e
        self._note(url, r)
        self._raise_for(r, "depop page")
        products = self.products_from_html(r.text)
        if products is None:
            raise SourceError("depop page: no product data found in HTML")
        self._good_endpoint = url
        out = []
        for prod in products:
            try:
                out.append(self.parse_product(prod))
            except Exception as e:
                log.debug("depop: skip unparsable product %s: %r", prod.get("id"), e)
        return out

    @staticmethod
    def products_from_html(html_text: str) -> Optional[list]:
        import json as _json
        found = None
        for blob in JSON_SCRIPT_RE.findall(html_text or ""):
            try:
                data = _json.loads(blob)
            except ValueError:
                continue
            prods = find_product_lists(data)
            if prods and (found is None or len(prods) > len(found)):
                found = prods
        if found is None and "__NEXT_DATA__" not in (html_text or "") and \
                "<script" in (html_text or ""):
            return None
        return found

    @staticmethod
    def _price(prod: dict) -> tuple[Optional[float], str]:
        pr = prod.get("price") or prod.get("pricing") or {}
        if not isinstance(pr, dict):
            return to_float(pr), prod.get("currency", "GBP")
        cur = pr.get("currencyName") or pr.get("currency_name") or pr.get("currency") or "GBP"
        # prefer a live discount, then the normal price
        for k in ("discountedPriceAmount", "discounted_price_amount", "priceAmount",
                  "price_amount", "amount"):
            v = to_float(pr.get(k))
            if v:
                return v, cur
        for k in ("discounted_price", "original_price"):  # v3-style nested objects
            sub = pr.get(k)
            if isinstance(sub, dict):
                v = to_float(sub.get("price_breakdown", {}).get("price") if
                             isinstance(sub.get("price_breakdown"), dict) else None) \
                    or to_float(sub.get("total_price")) or to_float(sub.get("amount"))
                if v:
                    return v, sub.get("currency_name", cur)
        return None, cur

    @staticmethod
    def _image(prod: dict) -> str:
        prev = prod.get("preview")
        if isinstance(prev, dict) and prev:
            for size in ("640", "480", "320", "210", "150"):
                if prev.get(size):
                    return prev[size]
            return next(iter(prev.values()))
        pics = prod.get("pictures") or prod.get("pictures_data") or []
        if pics:
            pic = pics[0]
            if isinstance(pic, list):
                pic = pic[-1] if pic else {}
            if isinstance(pic, dict):
                if pic.get("url"):
                    return pic["url"]
                fmts = pic.get("formats") or {}
                for k in ("P0", "P1", "P2", "P4"):
                    if isinstance(fmts.get(k), dict) and fmts[k].get("url"):
                        return fmts[k]["url"]
            elif isinstance(pic, str):
                return pic
        return ""

    @staticmethod
    def _title_from_slug(slug: str, seller: str) -> str:
        s = slug or ""
        if seller and s.lower().startswith(seller.lower() + "-"):
            s = s[len(seller) + 1:]
        s = re.sub(r"-(?=[0-9a-f]*[0-9])[0-9a-f]{3,6}$", "", s)  # trailing hash like -1f3a (needs a digit)
        return s.replace("-", " ").strip().capitalize()

    def parse_product(self, prod: dict) -> Listing:
        slug = prod.get("slug") or ""
        seller = ""
        for k in ("seller", "user", "owner"):
            if isinstance(prod.get(k), dict):
                seller = prod[k].get("username") or ""
                break
        price, cur = self._price(prod)
        sizes = prod.get("sizes") or prod.get("size") or []
        if isinstance(sizes, list):
            size = " / ".join(
                str(s.get("name") or s.get("title") or "") if isinstance(s, dict) else str(s)
                for s in sizes)
        else:
            size = str(sizes)
        brand = prod.get("brandName") or prod.get("brand_name") or prod.get("brand") or ""
        if isinstance(brand, dict):
            brand = brand.get("name", "")
        desc = prod.get("description") or ""
        title = prod.get("title") or (desc.split("\n", 1)[0][:90] if desc else "") \
            or self._title_from_slug(slug, seller)
        cond_raw = prod.get("condition") or prod.get("item_condition") or ""
        if isinstance(cond_raw, dict):
            cond_raw = cond_raw.get("id") or cond_raw.get("name") or ""
        url = prod.get("url") or f"https://www.depop.com/products/{slug or prod['id']}/"
        if url.startswith("/"):
            url = "https://www.depop.com" + url
        return Listing(
            platform="depop",
            id=str(prod.get("id") or slug),
            title=title,
            url=url,
            price=price if price is not None else 0.0,
            currency=cur,
            brand=str(brand),
            size=size,
            condition=normalize_condition(cond_raw),
            condition_raw=str(cond_raw),
            image_url=self._image(prod),
            description=desc,
            seller=seller,
            created_ts=_iso_to_ts(prod.get("dateCreated") or prod.get("date_created")
                                  or prod.get("created_at")),
        )
