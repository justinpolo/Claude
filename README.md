# Resale Sniper 🎯

Watches **Depop** and **Vinted** for new listings that match your rules (price, condition, size,
brand, keywords…) and pings your phone the moment one appears, with the photo, price, size and a
one-tap **🛒 Buy** button that opens the listing straight in the Depop/Vinted app.

> **Status (Nov 2026):** ✅ **Vinted works**: verified live on Windows. ⛔ **Depop is off by
> default.** Depop has retired its v3 search API (HTTP 410) and returns `403 Forbidden` to
> every non-browser client, including its own search page. That is a deliberate block, not a
> bug, and this tool doesn't try to get around it. The Depop code is kept: if Depop reopens
> access, set `platforms.depop.enabled: true` and run `sniper.bat doctor` to check.

- **Free to run**: no servers, no paid APIs, no proxies. It runs on your Windows PC.
- **Fast**: every watch is re-checked about every 20 s (configurable), so alerts typically land
  within seconds of the upload.
- **Accurate**: rules are re-checked locally, so "size 9" never matches "9.5" and `exclude: [kids]`
  really excludes. Items already listed when you add a watch are skipped, and you never get the
  same item twice, even after a restart.
- **Phone control**: add, pause or remove searches from Telegram (`/add nike dunk max=60 size=9`).
- **Self-healing**: if a site blocks or rate-limits you, it backs off, retries by itself and tells
  you when it's working again.

---

## 1. Install (Windows, ~5 min)

1. Install **Python 3.10+** from <https://www.python.org/downloads/>. In the installer, tick
   **"Add python.exe to PATH"**.
2. Put this folder somewhere permanent, e.g. `C:\ResaleSniper`.
3. Double-click **`install.bat`**. It creates a private Python environment, installs the two
   dependencies, creates `config.yaml` + `watches.yaml`, and runs a connection check (`doctor`).

`doctor` should show something like:

```
✅ vinted: 30 listings (30 fully parsed) in 0.6s
✅ depop: 24 listings (24 fully parsed) in 0.8s
```

## 2. Phone alerts: Telegram (recommended, 2 min)

Telegram gives instant push notifications with the item photo and a **Buy** button. It's free
and has no message limits that matter here.

1. Install Telegram on your phone. Open a chat with **@BotFather** and send `/newbot`. Pick any
   name. It replies with a **token** like `7234567890:AAH…`.
2. Open the chat with your new bot (BotFather links it) and send it any message, e.g. `hi`.
3. On the PC, run:
   ```
   sniper.bat telegram-setup --token 7234567890:AAH...
   ```
   It finds your chat, saves everything to `config.yaml` and sends you a **TEST ALERT**.

When an alert arrives, tap **🛒 Buy on Vinted — £45** and the listing opens in the Vinted/Depop
app, where you can buy immediately.

### Alternatives (can be combined)
| Channel | Setup | Notes |
|---|---|---|
| **ntfy** | Install the ntfy app → subscribe to a random topic name → put it in `config.yaml` | No account needed. Tapping the notification opens the listing. The free server limits messages per day per IP, which is fine for normal use. Anyone who guesses the topic can read it. |
| **Discord** | Channel → Edit → Integrations → Webhooks → copy URL into `config.yaml` | Embeds with photo and link. Set `mention: "@here"` to force a ping. |
| **Windows toast** | On by default | Click opens the listing in your browser. |

## 3. Tell it what to look for

Edit **`watches.yaml`**. The running bot reloads it automatically, so there's no need to restart:

```yaml
watches:
  - id: dunk-panda
    query: nike dunk low panda
    max_price: 60
    min_condition: good          # this or better
    sizes: ["UK 9", "9", "44"]
    exclude: [kids, gs, replica]

  - id: carhartt
    query: carhartt detroit jacket
    platforms: [depop]           # default: both
    max_price: 90
    sizes: [L, XL]
```

All rules are documented at the top of `watches.example.yaml`: `min_price`, `max_price`,
`use_total_price` (Vinted incl. fees), `min_condition` / `conditions`, `sizes`, `brands`,
`must_include`, `exclude`, `max_age_minutes`, `enabled`, plus raw `vinted:` / `depop:` filter
params for advanced use.

**Conditions** (worst → best): `fair`, `good`, `very_good`, `like_new`, `new`, `new_with_tags`

| Ours | Vinted | Depop |
|---|---|---|
| new_with_tags | New with tags | – |
| new | New without tags | Brand new |
| like_new | – | Like new |
| very_good | Very good | Excellent |
| good | Good | Good |
| fair | Satisfactory | Fair |

### From your phone (Telegram)
```
/add nike dunk low max=60 size=9,9.5 cond=good+ not=kids,gs
/add stone island £150 on=vinted
/add "north face" nuptse max=120 id=puffer
/list    /pause puffer    /resume puffer    /remove puffer
/status  (health + how often each watch is checked)
/recent  (last alerts)
```

### From the PC
```
sniper.bat add carhartt jacket max=40 size=L
sniper.bat list
sniper.bat search "nike dunk" --max 60 --cond good --size "UK 9"   (preview + why each item passes/fails)
```

## 4. Run it

- **`start.bat`**: starts watching. If it ever crashes it restarts itself after 10 s.
- **`autostart_on.bat`**: starts it (minimised) every time you log in. `autostart_off.bat` undoes this.
- Set Windows **Sleep → Never** (while plugged in), or alerts stop while the PC sleeps.
- Logs go to `sniper.log`.
- **`update.bat`**: downloads the latest version (keeps your config, watches and history).

## Speed vs. getting blocked

`config.yaml → poll`:

- `interval_seconds: 20`: each watch is re-checked about this often.
- `min_gap_seconds: 3`: never more than one request every 3 s per site, however many watches
  you have. With many watches, each one is checked every `watches × 3 s`, e.g. 10 watches → 30 s.

Requests are spread evenly with random jitter, so the bot looks like a normal browser tab
refreshing. Depop and Vinted are checked in parallel and don't slow each other down. Going much
faster than these defaults risks temporary IP blocks. If you get blocked, the bot backs off
automatically (30 s → 1 min → 2 min … up to 15 min) and messages you.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `doctor` says **BLOCKED** / HTTP 403 | Open the site in your normal browser on the same PC and pass any check. Wait 10–15 min. Raise `min_gap_seconds`. VPNs often trigger this, so turn them off. |
| `HTML page instead of JSON` | Same as above (bot-check page). |
| Vinted works but finds nothing in your country | Set `platforms.vinted.domain` (e.g. `www.vinted.fr`) |
| `doctor` says depop BLOCKED with `Forbidden - Depop` | Expected: Depop blocks automated access. Leave `platforms.depop.enabled: false`. |
| No Telegram messages | `sniper.bat test-notify`. Make sure you sent your bot a message before `telegram-setup`. |
| Parsing warnings | Run `sniper.bat --log-level DEBUG doctor` and look at the output; the sites change their APIs from time to time. |

## How it works

```
watches.yaml ─┐
              ▼
       ┌─────────────┐   newest-first search    ┌──────────────┐
       │  scheduler  │ ───────────────────────▶ │ Vinted / Depop│
       │ (1 loop per │ ◀─────────────────────── │   web APIs    │
       │   site)     │        listings          └──────────────┘
       └─────┬───────┘
             ▼
     SQLite "seen" diff ──▶ rule engine ──▶ Telegram / ntfy / Discord / Windows toast
```

- **Vinted**: `https://api.vinted.<tld>/svc-catalogue/items?order=newest_first…` using the
  anonymous session cookie the homepage sets (same as the website). Falls back automatically to
  the older `/api/v2/catalog/items`.
- **Depop** (disabled, see Status): `webapi.depop.com/api/v2/search/products?sort=newlyListed`,
  falling back to the product data embedded in the `depop.com/search` page. `doctor` prints
  every attempt with a diagnosis (retired / Cloudflare challenge / hard 403).
- Price and condition filters are sent to the site to keep responses small, and all rules are
  re-checked locally.
- When a watch's search changes, it re-baselines quietly so you don't get flooded with old
  listings.
- Old items that get bumped into the "newest" feed (promoted/relisted) are skipped via
  `max_listing_age_minutes` (default 120).

## Development

```
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest              # 52 unit/integration tests
.venv\Scripts\python tools\e2e_simulation.py --chaos   # full end-to-end run against fake sites
```

`tools/e2e_simulation.py` starts the real bot process against local fake Vinted/Depop/Telegram
servers. It uploads listings plus decoys at random moments, sends `/add` and `/status` from a
fake phone, injects a 403 outage, and reports missed / false / duplicate alerts and
upload→alert latency.

Use responsibly: this reads public listing data at a gentle, human-like rate for personal
shopping. Don't crank the poll rate or resell the data, and respect each site's terms.
