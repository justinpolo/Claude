"""Bring an existing config.yaml up to the current recommended settings.

    python tools/migrate_config.py [config.yaml]

- Backs the file up first (config.yaml.bak-YYYYmmdd-HHMMSS).
- Keeps everything personal: Telegram/Discord/ntfy tokens, chat id, Vinted domain, etc.
- Updates only the speed/freshness settings and turns Depop off (Depop blocks automated access).
- Prints what changed. Never prints secrets.
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import yaml

SET = {
    ("poll", "interval_seconds"): 6,
    ("poll", "min_gap_seconds"): 3,
    ("poll", "jitter"): 0.2,
    ("poll", "max_backoff_seconds"): 900,
    ("fresh_only",): True,
    ("fresh_grace_minutes",): 0,
    ("max_listing_age_minutes",): 3,
    ("heartbeat_minutes",): 5,
    ("platforms", "depop", "enabled"): False,
}


def main(path: str = "config.yaml") -> int:
    p = Path(path)
    if not p.exists():
        example = p.with_name("config.example.yaml")
        if example.exists():
            shutil.copy(example, p)
            print(f"no {p.name} found - created it from {example.name}")
        else:
            print(f"{p} not found")
            return 1
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    backup = p.with_name(f"{p.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy(p, backup)
    changes = []
    for keys, value in SET.items():
        node = data
        for k in keys[:-1]:
            if not isinstance(node.get(k), dict):
                node[k] = {}
            node = node[k]
        old = node.get(keys[-1], "<unset>")
        if old != value:
            node[keys[-1]] = value
            changes.append(f"  {'.'.join(keys)}: {old} -> {value}")
    p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"backup saved: {backup.name}")
    print("changed:\n" + "\n".join(changes) if changes else "already up to date - nothing changed")
    tg = (data.get("notifications") or {}).get("telegram") or {}
    print(f"telegram: {'configured' if tg.get('bot_token') and tg.get('chat_id') else 'NOT configured'} "
          f"(enabled={tg.get('enabled', False)})")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
