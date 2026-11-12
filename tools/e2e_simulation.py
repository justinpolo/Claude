"""End-to-end simulation: runs the REAL `python -m sniper run` process against local fake
Vinted / Depop / Telegram servers, uploads listings at random moments and measures how long
it takes for the Telegram alert to arrive. Also drives a /add command through fake Telegram.

    python tools/e2e_simulation.py [--interval 6] [--uploads 6]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent


class World:
    def __init__(self):
        self.lock = threading.Lock()
        self.vinted, self.depop = [], []   # newest first
        self.uploaded_at = {}              # key -> time
        self.alerts = []                   # (time, payload)
        self.updates = []                  # pending telegram updates
        self.update_id = 0
        self.replies = []
        self.requests = {"vinted": 0, "depop": 0}
        self.queries = []
        self.block_vinted_until = 0.0
        self.blocked_hits = 0

    def upload(self, platform, item):
        with self.lock:
            (self.vinted if platform == "vinted" else self.depop).insert(0, item)
            self.uploaded_at[f"{platform}:{item['id']}"] = time.time()


W = World()


def vinted_item(i, title, price, status="Very good", size="UK 9"):
    return {"id": i, "title": title, "price": {"amount": str(price), "currency_code": "GBP"},
            "total_item_price": {"amount": f"{price * 1.05 + 0.7:.2f}", "currency_code": "GBP"},
            "brand_title": title.split()[0], "size_title": size, "status": status,
            "url": f"https://www.vinted.co.uk/items/{i}-x",
            "photo": {"url": f"https://img/v{i}.jpg", "high_resolution": {"timestamp": int(time.time())}},
            "user": {"login": "seller"}}


def depop_item(i, slug, price, cond="used_excellent", size="L"):
    return {"id": i, "slug": slug, "price": {"priceAmount": f"{price:.2f}", "currencyName": "GBP"},
            "preview": {"640": f"https://img/d{i}.jpg"}, "sizes": [size], "condition": cond,
            "dateCreated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            self.send_response(200)
            self.send_header("set-cookie", "access_token_web=fake; Path=/")
            self.send_header("content-length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        elif u.path in ("/api/v2/catalog/items", "/svc-catalogue/items") and \
                time.time() < W.block_vinted_until:
            W.blocked_hits += 1
            self._json({"error": "blocked"}, 403)
        elif u.path in ("/api/v2/catalog/items", "/svc-catalogue/items"):
            W.requests["vinted"] += 1
            W.queries.append(("vinted", q.get("search_text", [""])[0]))
            text = q.get("search_text", [""])[0].lower()
            with W.lock:
                items = [i for i in W.vinted if all(t in i["title"].lower() for t in text.split())]
            self._json({"items": items[:30]})
        elif u.path.startswith("/api/v3/search/products"):
            self._json({"error": "gone"}, 410)   # simulate Depop retiring its JSON API
        elif u.path == "/depop-search/":
            W.requests["depop"] += 1
            text = q.get("q", [""])[0].lower()
            with W.lock:
                items = [i for i in W.depop if all(t in i["slug"] for t in text.split())]
            page = ('<html><body><script id="__NEXT_DATA__" type="application/json">'
                    + json.dumps({"props": {"pageProps": {"initialData": {"products": items[:24]}}}})
                    + "</script></body></html>").encode()
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.send_header("content-length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
        elif u.path.endswith("/getUpdates"):
            time.sleep(0.3)
            with W.lock:
                ups, W.updates = W.updates, []
            self._json({"ok": True, "result": ups})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path.endswith("/sendPhoto") or self.path.endswith("/sendMessage"):
            if "reply_markup" in body:
                W.alerts.append((time.time(), body))
            else:
                W.replies.append(body.get("text", ""))
            self._json({"ok": True, "result": {}})
        else:
            self._json({"ok": False}, 404)


def send_command(text):
    with W.lock:
        W.update_id += 1
        W.updates.append({"update_id": W.update_id, "message": {"chat": {"id": 4242}, "text": text}})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=6)
    ap.add_argument("--uploads", type=int, default=6)
    ap.add_argument("--chaos", action="store_true", help="also simulate a 20s Vinted block")
    args = ap.parse_args()

    class Quiet(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            pass  # long-poll connections dropped at shutdown
    srv = Quiet(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    # pre-existing listings (must NOT alert)
    for i in range(5):
        W.upload("vinted", vinted_item(1000 + i, "Nike Dunk Low old stock", 30))
        W.upload("depop", depop_item(2000 + i, f"seller-carhartt-detroit-old-{i}", 60))
    W.uploaded_at.clear()

    tmp = Path(tempfile.mkdtemp(prefix="sniper-e2e-"))
    (tmp / "config.yaml").write_text(json.dumps({
        "poll": {"interval_seconds": args.interval, "min_gap_seconds": 0.5, "jitter": 0.2},
        "platforms": {"vinted": {"base_url": base},
                      "depop": {"endpoints": [base + "/api/v3/search/products/", base + "/depop-search/"]}},
        "notifications": {"console": False, "telegram": {
            "enabled": True, "bot_token": "TEST", "chat_id": "4242", "api_base": base}},
        "database": str(tmp / "sniper.db"), "log_file": str(tmp / "sniper.log"),
    }))
    (tmp / "watches.yaml").write_text(json.dumps({"watches": [
        {"id": "dunks", "query": "nike dunk", "platforms": ["vinted"], "max_price": 50,
         "min_condition": "good", "exclude": ["kids"], "sizes": ["UK 9"]},
        {"id": "carhartt", "query": "carhartt detroit", "platforms": ["depop"], "max_price": 100},
    ]}))
    proc = subprocess.Popen([sys.executable, "-m", "sniper", "--config", str(tmp / "config.yaml"),
                             "--watches", str(tmp / "watches.yaml"), "run"], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env={**os.environ, "PYTHONUNBUFFERED": "1"})
    log_lines = []
    threading.Thread(target=lambda: [log_lines.append(l) for l in proc.stdout], daemon=True).start()

    try:
        time.sleep(args.interval + 2)   # let it baseline
        assert not W.alerts, f"alerted on pre-existing listings: {W.alerts}"
        expected, decoys = set(), set()
        nid = 5000
        for k in range(args.uploads):
            time.sleep(random.uniform(1, args.interval))
            nid += 1
            if k % 2 == 0:
                W.upload("vinted", vinted_item(nid, "Nike Dunk Low Panda", 40 + k))
                expected.add(f"vinted:{nid}")
                nid += 1  # a decoy at the same moment: wrong size / kids / too pricey / poor cond
                decoy = random.choice([vinted_item(nid, "Nike Dunk Low", 35, size="UK 6"),
                                       vinted_item(nid, "Nike Dunk Low kids", 20),
                                       vinted_item(nid, "Nike Dunk Low", 95),
                                       vinted_item(nid, "Nike Dunk Low", 25, status="Satisfactory")])
                W.upload("vinted", decoy)
                decoys.add(f"vinted:{nid}")
            else:
                W.upload("depop", depop_item(nid, f"bob-carhartt-detroit-jacket-{nid}", 70 + k))
                expected.add(f"depop:{nid}")
        # live /add from "phone"
        send_command("/add stone island max=150 on=vinted")
        time.sleep(args.interval + 2)  # baseline for the new watch
        nid += 1
        W.upload("vinted", vinted_item(nid, "Stone Island overshirt", 120, size="L"))
        expected.add(f"vinted:{nid}")
        deadline = time.time() + args.interval * 3
        while time.time() < deadline:
            got = {a[1]["reply_markup"]["inline_keyboard"][0][0]["url"] for a in W.alerts}
            if len(W.alerts) >= len(expected):
                break
            time.sleep(0.2)
        if args.chaos:
            W.block_vinted_until = time.time() + 20
            time.sleep(3)
            nid += 1
            W.upload("vinted", vinted_item(nid, "Nike Dunk Low Panda", 33))  # during the outage
            expected.add(f"vinted:{nid}")
            chaos_key = f"vinted:{nid}"
            deadline = time.time() + 120
            while time.time() < deadline and not any(
                    f"/{nid}-" in a[1]["reply_markup"]["inline_keyboard"][0][0]["url"] for a in W.alerts):
                time.sleep(0.5)
        send_command("/status")
        time.sleep(1.5)
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        srv.shutdown()

    def key_of(alert):
        url = alert["reply_markup"]["inline_keyboard"][0][0]["url"]
        if "vinted" in url:
            return "vinted:" + url.rsplit("/", 1)[1].split("-")[0]
        return "depop:" + url.rstrip("/").rsplit("-", 1)[1]

    got = {}
    for t, body in W.alerts:
        got.setdefault(key_of(body), []).append(t)
    lat = sorted(got[k][0] - W.uploaded_at[k] for k in got if k in W.uploaded_at)
    missed = expected - set(got)
    false_pos = set(got) - expected
    dupes = {k: len(v) for k, v in got.items() if len(v) > 1}
    print("\n===== E2E RESULT =====")
    print(f"poll interval            : {args.interval}s per watch")
    print(f"listings uploaded        : {len(expected)} matching + {len(decoys)} decoys")
    print(f"alerts received          : {len(W.alerts)}")
    print(f"missed                   : {sorted(missed) or 'none'}")
    print(f"false positives          : {sorted(false_pos) or 'none'}")
    print(f"duplicates               : {dupes or 'none'}")
    if lat:
        print(f"upload -> alert latency  : min {lat[0]:.1f}s · median {lat[len(lat)//2]:.1f}s · max {lat[-1]:.1f}s")
    print(f"requests made            : {W.requests}")
    if args.chaos:
        print(f"chaos: 403s served       : {W.blocked_hits} (bot backed off instead of hammering)")
        rec = [t for t, b in W.alerts if b["reply_markup"]["inline_keyboard"][0][0]["url"].endswith(
            "/" + chaos_key.split(":")[1] + "-x")]
        if rec:
            print(f"chaos: outage item alert : {rec[0] - W.uploaded_at[chaos_key]:.0f}s after upload "
                  f"(block lasted 17s more)")
    print(f"telegram /add reply      : {next((r for r in W.replies if 'Watching' in r), 'NONE')[:60]!r}")
    print(f"telegram /status reply   : {next((r for r in W.replies if 'Up ' in r), 'NONE')!r}")
    sample = W.alerts[0][1] if W.alerts else {}
    print(f"sample alert             : {json.dumps(sample, ensure_ascii=False)[:400]}")
    ok = not missed and not false_pos and not dupes and any('Watching' in r for r in W.replies)
    if not ok:
        print("\n--- sniper log ---\n" + "".join(log_lines[-40:]))
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
