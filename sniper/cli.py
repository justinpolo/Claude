"""Command line entry point:  python -m sniper <command>"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

import yaml

from . import __version__, rules
from .config import load_config, load_watches, save_watches
from .engine import Engine
from .models import Listing, Watch
from .notifiers import (ConsoleNotifier, broadcast, broadcast_text, build_notifiers, money,
                        summary_lines)
from .sources import BlockedError, SourceError, build_sources
from .store import Store

log = logging.getLogger("sniper")


def _setup_logging(level: str, logfile: str | None = None):
    handlers = [logging.StreamHandler(sys.stdout)]
    if logfile:
        from logging.handlers import RotatingFileHandler
        handlers.append(RotatingFileHandler(logfile, maxBytes=2_000_000, backupCount=3,
                                            encoding="utf-8"))
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
                        handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _utf8_console():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ---------------- commands ----------------
async def cmd_run(args, cfg):
    store = Store(cfg["database"])
    sources = build_sources(cfg)
    notifiers = build_notifiers(cfg)
    engine = Engine(cfg, sources, notifiers, store, watches_path=args.watches)
    extra = []
    tg = cfg["notifications"]["telegram"]
    if tg.get("enabled") and tg.get("bot_token") and tg.get("chat_id") and tg.get("commands", True):
        from .telegram_bot import TelegramCommands
        extra.append(TelegramCommands(tg["bot_token"], tg["chat_id"], args.watches,
                                      engine=engine, store=store,
                                      api_base=tg.get("api_base", "https://api.telegram.org")).poll_forever())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, engine.stop)
        except (NotImplementedError, RuntimeError):
            pass  # Windows: Ctrl+C raises KeyboardInterrupt instead
    log.info("Resale Sniper %s running. Channels: %s. Ctrl+C to stop.", __version__,
             ", ".join(n.name for n in notifiers))
    try:
        await engine.run(extra_tasks=tuple(extra))
    finally:
        for s in sources.values():
            await s.close()
        for n in notifiers:
            await n.close()
        store.close()


def _print_listing(l: Listing, verdict: str | None):
    mark = "✅" if verdict is None else "  "
    print(f"{mark} [{l.platform}] {l.title[:70]}")
    print(f"     {' | '.join(summary_lines(l))}")
    if verdict:
        print(f"     ✗ {verdict}")
    print(f"     {l.url}")


async def cmd_search(args, cfg):
    """Ad-hoc search: shows what the bot sees right now and which rules pass."""
    w = Watch.from_dict({"id": "adhoc", "query": args.query,
                         "platforms": args.platform or ["vinted", "depop"],
                         **({"max_price": args.max} if args.max is not None else {}),
                         **({"min_price": args.min} if args.min is not None else {}),
                         **({"min_condition": args.cond} if args.cond else {}),
                         **({"sizes": args.size} if args.size else {})})
    sources = build_sources(cfg)
    try:
        for p in w.platforms:
            if p not in sources:
                continue
            t0 = time.perf_counter()
            try:
                ls = await sources[p].search(w)
            except SourceError as e:
                print(f"❌ {p}: {e}")
                continue
            print(f"\n=== {p.upper()}: {len(ls)} newest listings in {time.perf_counter() - t0:.1f}s ===")
            for l in ls[: args.limit]:
                _print_listing(l, rules.check(l, w))
    finally:
        for s in sources.values():
            await s.close()


async def cmd_doctor(args, cfg):
    ok = True
    print(f"Resale Sniper {__version__} · Python {sys.version.split()[0]} · {sys.platform}")
    try:
        ws = load_watches(args.watches)
        print(f"✅ watches file: {len(ws)} watch(es) in {args.watches}")
    except Exception as e:
        ok = False
        print(f"❌ watches file: {e}")
    probe = Watch.from_dict({"id": "doctor", "query": "nike"})
    sources = build_sources(cfg)
    for name, src in sources.items():
        t0 = time.perf_counter()
        try:
            ls = await src.search(probe)
            dt = time.perf_counter() - t0
            parsed = sum(1 for l in ls if l.price and l.url)
            print(f"✅ {name}: {len(ls)} listings ({parsed} fully parsed) in {dt:.1f}s")
            if ls:
                print(f"   newest: {ls[0].title[:60]} - {money(ls[0].price, ls[0].currency)} - {ls[0].url}")
            if not ls or parsed < len(ls) / 2:
                ok = False
                print(f"   ⚠️ parsing looks off - run with --log-level DEBUG and report the output")
        except BlockedError as e:
            ok = False
            print(f"❌ {name}: BLOCKED - {e}\n   Open the site in a normal browser on this PC "
                  f"(solve any check), wait 10-15 min, and lower poll speed in config.yaml.")
        except SourceError as e:
            ok = False
            print(f"❌ {name}: {e}")
        finally:
            await src.close()
    notifiers = [n for n in build_notifiers(cfg) if n.name != "console"]
    print("✅ alert channels: " + (", ".join(n.name for n in notifiers) if notifiers else
                                  "console only (set up Telegram/ntfy in config.yaml)"))
    for n in notifiers:
        await n.close()
    return 0 if ok else 1


def _sample_listing() -> tuple[Listing, Watch]:
    l = Listing(platform="vinted", id="test", title="TEST ALERT - Nike Dunk Low Panda",
                url="https://www.vinted.co.uk/catalog?search_text=nike%20dunk%20low&order=newest_first",
                price=45.0, currency="GBP", total_price=48.95, brand="Nike", size="UK 9",
                condition="very_good", condition_raw="Very good", seller="tester",
                image_url="https://upload.wikimedia.org/wikipedia/commons/thumb/a/a6/Logo_NIKE.svg/320px-Logo_NIKE.svg.png",
                created_ts=time.time() - 12)
    return l, Watch.from_dict({"id": "test-alert", "query": "nike dunk"})


async def cmd_test_notify(args, cfg):
    notifiers = build_notifiers(cfg)
    l, w = _sample_listing()
    res = await broadcast(notifiers, l, w)
    for n in notifiers:
        await n.close()
    for name, err in res.items():
        print(f"{'✅' if err is None else '❌'} {name}{'' if err is None else ': ' + err}")
    return 0 if all(e is None for e in res.values()) else 1


async def cmd_telegram_setup(args, cfg):
    from .telegram_bot import discover_chat_id
    token = args.token or cfg["notifications"]["telegram"].get("bot_token")
    if not token:
        print("Pass your bot token: python -m sniper telegram-setup --token 123:ABC...")
        return 1
    print("Open your bot in Telegram and send it any message (e.g. 'hi'). Waiting up to 2 min...")
    found = None
    for _ in range(40):
        try:
            found = await discover_chat_id(token)
        except Exception as e:
            print(f"❌ {e}")
            return 1
        if found:
            break
        await asyncio.sleep(3)
    if not found:
        print("No message received. Make sure you messaged the right bot.")
        return 1
    chat_id, name = found
    print(f"✅ Found chat with {name}: chat_id = {chat_id}")
    path = Path(args.config)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    data = data or {}
    tg = data.setdefault("notifications", {}).setdefault("telegram", {})
    tg.update({"enabled": True, "bot_token": token, "chat_id": chat_id})
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"✅ Saved to {path}. Sending a test alert...")
    cfg = load_config(path)
    return await cmd_test_notify(args, cfg)


async def cmd_add(args, cfg):
    from .telegram_bot import describe, parse_add
    ws = load_watches(args.watches)
    w = parse_add(" ".join(args.spec), {x.id for x in ws})
    ws.append(w)
    save_watches(ws, args.watches)
    print("Added: " + describe(w).replace("<b>", "").replace("</b>", ""))


async def cmd_list(args, cfg):
    from .telegram_bot import describe
    ws = load_watches(args.watches)
    if not ws:
        print("No watches. Add one:  python -m sniper add nike dunk low max=60 size=9")
    for w in ws:
        print(describe(w).replace("<b>", "").replace("</b>", ""))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="sniper", description="Instant alerts for new Depop/Vinted listings")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--watches", default="watches.yaml")
    ap.add_argument("--log-level", default=None)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="start watching (the main thing)")
    s = sub.add_parser("search", help="one-off search to preview results + rule verdicts")
    s.add_argument("query")
    s.add_argument("--platform", action="append", choices=["vinted", "depop"])
    s.add_argument("--max", type=float)
    s.add_argument("--min", type=float)
    s.add_argument("--cond")
    s.add_argument("--size", action="append")
    s.add_argument("--limit", type=int, default=10)
    sub.add_parser("doctor", help="check connectivity, parsing and config")
    sub.add_parser("test-notify", help="send a sample alert to every channel")
    t = sub.add_parser("telegram-setup", help="link your Telegram bot (finds your chat id)")
    t.add_argument("--token")
    a = sub.add_parser("add", help='add a watch, e.g.: add carhartt jacket max=40 size=L')
    a.add_argument("spec", nargs="+")
    sub.add_parser("list", help="list watches")
    return ap


def main(argv=None) -> int:
    _utf8_console()
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    _setup_logging(args.log_level or cfg.get("log_level", "INFO"),
                   cfg.get("log_file") if args.cmd == "run" else None)
    fn = {"run": cmd_run, "search": cmd_search, "doctor": cmd_doctor,
          "test-notify": cmd_test_notify, "telegram-setup": cmd_telegram_setup,
          "add": cmd_add, "list": cmd_list}[args.cmd]
    if sys.platform == "win32" and sys.version_info < (3, 8):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        rc = asyncio.run(fn(args, cfg))
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except ValueError as e:
        print(f"⚠️ {e}")
        return 2
    return rc or 0
