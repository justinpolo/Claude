"""Rule engine: decides whether a listing should trigger an alert for a watch."""
from __future__ import annotations

import re
import time
from typing import Optional

from .models import Listing, Watch


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower())


def _contains_term(haystack: str, term: str) -> bool:
    term = _norm(term).strip()
    if not term:
        return True
    # word-boundary match so "air" doesn't hit "chair"; works for multi-word terms too
    # "9" must not hit "9.5" / "19", but "uk 9" still hits "uk 9 / eu 44"
    pat = r"(?<![a-z0-9])(?<![0-9]\.)" + re.escape(term) + r"(?![a-z0-9])(?!\.[0-9])"
    return re.search(pat, haystack) is not None


def _size_matches(listing_size: str, wanted: list) -> bool:
    ls = _norm(listing_size)
    if not ls:
        return False
    # split "UK 9 / EU 43" or "M / 38 / 10" into tokens and compare whole tokens / phrases
    for w in wanted:
        w = _norm(w).strip()
        if not w:
            continue
        if ls == w or _contains_term(ls, w):
            return True
    return False


def check(listing: Listing, watch: Watch, now: Optional[float] = None,
          default_max_age: Optional[float] = None) -> Optional[str]:
    """Return None if the listing passes every rule, else a short reason it was rejected.

    default_max_age (minutes) applies when the watch sets no max_age_minutes: it stops old
    bumped/promoted items that newly appear in the "newest" feed from triggering alerts."""
    if listing.platform not in watch.platforms:
        return "platform"

    price = listing.price
    if watch.use_total_price and listing.total_price is not None:
        price = listing.total_price
    if watch.min_price is not None and price < watch.min_price:
        return f"price {price} < min {watch.min_price}"
    if watch.max_price is not None and price > watch.max_price:
        return f"price {price} > max {watch.max_price}"

    allowed = watch.allowed_conditions()
    if allowed is not None:
        if listing.condition is None:
            return f"condition unknown ({listing.condition_raw!r})"
        if listing.condition not in allowed:
            return f"condition {listing.condition}"

    text = _norm(f"{listing.title} {listing.description} {listing.brand}")
    for term in watch.must_include:
        if not _contains_term(text, term):
            return f"missing '{term}'"
    for term in watch.exclude:
        if _contains_term(text, term):
            return f"excluded '{term}'"

    if watch.brands:
        b = _norm(f"{listing.brand} {listing.title}")
        if not any(_norm(x).strip() in b for x in watch.brands):
            return f"brand {listing.brand!r}"

    if watch.sizes and not _size_matches(listing.size, watch.sizes):
        return f"size {listing.size!r}"

    max_age = watch.max_age_minutes if watch.max_age_minutes is not None else default_max_age
    if listing.created_ts:
        age_min = ((now or time.time()) - listing.created_ts) / 60
        if max_age is not None and age_min > max_age:
            return f"too old ({age_min:.0f} min)"
        if listing.promoted and age_min > 15:
            return "promoted (old item bumped into the feed)"
    elif listing.promoted:
        return "promoted, age unknown"

    return None


def matches(listing: Listing, watch: Watch, now: Optional[float] = None,
            default_max_age: Optional[float] = None) -> bool:
    return check(listing, watch, now, default_max_age) is None
