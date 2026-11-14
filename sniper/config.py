"""Config (config.yaml) + watch list (watches.yaml) loading/saving."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

from .models import Watch

DEFAULTS = {
    "poll": {
        "interval_seconds": 20,     # target time between checks of the SAME watch
        "min_gap_seconds": 3.0,     # never hit one platform faster than this
        "jitter": 0.25,             # +/- randomness so we don't look like a metronome
        "max_backoff_seconds": 900,
    },
    "platforms": {
        "vinted": {"enabled": True, "domain": "www.vinted.co.uk", "per_page": 30},
        # Depop currently refuses automated clients (HTTP 403), so it is off by default.
        "depop": {"enabled": False, "country": "gb", "currency": "GBP", "per_page": 24},
    },
    "notifications": {
        "console": True,
        "telegram": {"enabled": False, "bot_token": "", "chat_id": "", "commands": True},
        "discord": {"enabled": False, "webhook_url": "", "mention": ""},
        "ntfy": {"enabled": False, "topic": "", "server": "https://ntfy.sh", "priority": 5},
        "desktop": {"enabled": False, "sound": True},
    },
    "alert_on_start": False,
    "max_listing_age_minutes": 120, # ignore items older than this (bumps/relists); 0 = off        # True = also alert for listings already live at startup
    "database": "sniper.db",
    "log_level": "INFO",
    "log_file": "sniper.log",
    "heartbeat_minutes": 5,         # print a "still alive" line this often (0 = off)
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike = "config.yaml") -> dict:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    cfg = _merge(DEFAULTS, data or {})
    # secrets may come from env vars instead of the file
    tg = cfg["notifications"]["telegram"]
    tg["bot_token"] = os.environ.get("SNIPER_TELEGRAM_TOKEN", tg.get("bot_token", ""))
    tg["chat_id"] = os.environ.get("SNIPER_TELEGRAM_CHAT_ID", str(tg.get("chat_id") or ""))
    dc = cfg["notifications"]["discord"]
    dc["webhook_url"] = os.environ.get("SNIPER_DISCORD_WEBHOOK", dc.get("webhook_url", ""))
    return cfg


def load_watches(path: str | os.PathLike = "watches.yaml") -> list[Watch]:
    p = Path(path)
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    raw = data.get("watches", data) if isinstance(data, dict) else data
    watches, ids = [], set()
    for d in raw or []:
        w = Watch.from_dict(d)
        if w.id in ids:
            raise ValueError(f"duplicate watch id '{w.id}'")
        ids.add(w.id)
        watches.append(w)
    return watches


def save_watches(watches: list[Watch], path: str | os.PathLike = "watches.yaml"):
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    header = ("# Your saved searches. Edit freely - the bot reloads this file automatically.\n"
              "# See watches.example.yaml for every available rule.\n")
    body = yaml.safe_dump({"watches": [w.to_dict() for w in watches]},
                          sort_keys=False, allow_unicode=True)
    tmp.write_text(header + body, encoding="utf-8")
    os.replace(tmp, p)
