"""Core data types shared across the app."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Optional

# Unified condition scale. Higher = better. Each platform maps onto this.
CONDITION_RANK = {
    "new_with_tags": 6,   # Vinted: New with tags
    "new": 5,             # Vinted: New without tags | Depop: Brand new
    "like_new": 4,        # Depop: Like new
    "very_good": 3,       # Vinted: Very good | Depop: Excellent
    "good": 2,            # Vinted: Good | Depop: Good
    "fair": 1,            # Vinted: Satisfactory | Depop: Fair
}
CONDITION_ALIASES = {
    "nwt": "new_with_tags", "new with tags": "new_with_tags", "new_with_tags": "new_with_tags",
    "new": "new", "nwot": "new", "new without tags": "new", "brand new": "new", "brand_new": "new",
    "like new": "like_new", "like_new": "like_new", "used_like_new": "like_new",
    "very good": "very_good", "very_good": "very_good", "excellent": "very_good",
    "used_excellent": "very_good",
    "good": "good", "used_good": "good",
    "fair": "fair", "satisfactory": "fair", "used_fair": "fair",
}
PLATFORMS = ("vinted", "depop")


def normalize_condition(raw) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        raw = raw.get("id") or raw.get("code") or raw.get("name") or raw.get("title")
    s = str(raw).strip().lower().replace("-", " ")
    if s in CONDITION_ALIASES:
        return CONDITION_ALIASES[s]
    s2 = s.replace(" ", "_")
    return CONDITION_ALIASES.get(s2)


@dataclass
class Listing:
    platform: str
    id: str
    title: str
    url: str
    price: float
    currency: str = "GBP"
    total_price: Optional[float] = None      # incl. buyer fees where the platform reports it
    brand: str = ""
    size: str = ""
    condition: Optional[str] = None          # normalized key from CONDITION_RANK
    condition_raw: str = ""
    image_url: str = ""
    description: str = ""
    seller: str = ""
    created_ts: Optional[float] = None       # unix seconds, when known
    promoted: bool = False                   # paid bump / ad slot in the results

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.id}"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Watch:
    """A saved search + the rules a listing must pass to trigger an alert."""
    id: str
    query: str
    platforms: list = field(default_factory=lambda: list(PLATFORMS))
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    conditions: list = field(default_factory=list)   # explicit allow-list (normalized keys)
    min_condition: Optional[str] = None              # or a floor on the scale
    sizes: list = field(default_factory=list)        # any of these (case-insensitive)
    brands: list = field(default_factory=list)       # any of these (substring, case-insensitive)
    must_include: list = field(default_factory=list) # every term must appear in title/desc
    any_of: list = field(default_factory=list)       # at least ONE of these must appear
    exclude: list = field(default_factory=list)      # none of these may appear
    max_age_minutes: Optional[float] = None          # skip listings older than this (if known)
    use_total_price: bool = False                    # compare total (incl. fees) instead of price
    enabled: bool = True
    vinted: dict = field(default_factory=dict)       # raw extra API params, e.g. catalog_ids
    depop: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Watch":
        d = dict(d)
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"watch '{d.get('id', '?')}': unknown field(s) {sorted(unknown)}")
        if "id" not in d or not str(d["id"]).strip():
            raise ValueError("every watch needs an 'id'")
        if not str(d.get("query", "")).strip():
            raise ValueError(f"watch '{d['id']}' needs a 'query'")
        d["id"] = str(d["id"]).strip()
        for k in ("platforms", "conditions", "sizes", "brands", "must_include", "any_of",
                  "exclude"):
            v = d.get(k)
            if v is None:
                d.pop(k, None)
                continue
            if isinstance(v, str):
                v = v.split(",")
            if not isinstance(v, list):
                v = [v]
            # drop blanks: `sizes: [""]` must mean "any size", not "match nothing"
            d[k] = [str(x).strip() for x in v if x is not None and str(x).strip()]
        if "platforms" in d:
            d["platforms"] = [p.lower() for p in d["platforms"]]
            if d["platforms"] == ["all"]:
                d["platforms"] = list(PLATFORMS)
            bad = [p for p in d["platforms"] if p not in PLATFORMS]
            if bad:
                raise ValueError(f"watch '{d['id']}': unknown platform(s) {bad}")
        if "conditions" in d:
            norm = []
            for c in d["conditions"]:
                n = normalize_condition(c)
                if not n:
                    raise ValueError(f"watch '{d['id']}': unknown condition '{c}'")
                norm.append(n)
            d["conditions"] = norm
        if d.get("min_condition"):
            n = normalize_condition(d["min_condition"])
            if not n:
                raise ValueError(f"watch '{d['id']}': unknown min_condition '{d['min_condition']}'")
            d["min_condition"] = n
        return cls(**d)

    def to_dict(self) -> dict:
        out = {}
        for k, v in asdict(self).items():
            default = Watch.__dataclass_fields__[k].default
            if v in (None, [], {}) and k != "query":
                continue
            if k == "enabled" and v is True:
                continue
            if k == "use_total_price" and v is False:
                continue
            if k == "platforms" and sorted(v) == sorted(PLATFORMS):
                continue
            out[k] = v
        return out

    def allowed_conditions(self) -> Optional[set]:
        """Set of normalized conditions that pass, or None for 'any'."""
        allowed = set(self.conditions) if self.conditions else None
        if self.min_condition:
            floor = CONDITION_RANK[self.min_condition]
            by_floor = {c for c, r in CONDITION_RANK.items() if r >= floor}
            allowed = by_floor if allowed is None else allowed & by_floor
        return allowed

    def fingerprint(self, platform: str) -> str:
        """Changes whenever the search that hits the platform changes -> triggers a re-baseline."""
        blob = json.dumps({
            "p": platform, "q": self.query.lower().strip(), "min": self.min_price,
            "max": self.max_price, "c": sorted(self.allowed_conditions() or []),
            "x": self.vinted if platform == "vinted" else self.depop,
        }, sort_keys=True)
        return hashlib.sha1(blob.encode()).hexdigest()[:16]
