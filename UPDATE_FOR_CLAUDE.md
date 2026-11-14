# Resale Sniper: update brief for Claude Code

You are Claude Code running in a terminal on the owner's **Windows PC**. The owner has asked you
to apply this update to their existing Resale Sniper install. This file explains what the
project is, what changed and why, and the exact steps to apply and verify the update. Follow the
steps in order, show the owner each command's output, and stop and report if something fails.

---

## 1. Background: what this project is

Resale Sniper is a small Python bot that watches **Vinted** for new listings matching the owner's
rules (price, condition, size, brand, keywords) and alerts their phone within seconds via
**Telegram**. Each alert has the photo and a one-tap "Buy" button that opens the listing in the
Vinted app. It runs locally on this PC (`start.bat`) and costs nothing.

- Code: the `sniper/` package (Python 3.10+, dependencies `httpx` and `PyYAML`, plus
  `win11toast` on Windows).
- **Owner's personal files. Never overwrite or delete these:**
  - `config.yaml`: settings, including the **Telegram bot token and chat id (secret: never print
    them)**.
  - `watches.yaml`: the owner's saved searches.
  - `sniper.db`, `sniper.db-wal`, `sniper.db-shm`: memory of already-seen listings.
  - `sniper.log*`: logs.
- The install has a virtual environment in `.venv\`. Use `.venv\Scripts\python` for everything.

## 2. History so far, and why this update exists

1. **Vinted works live** from this PC. `doctor` showed "✅ vinted: 30 listings (30 fully parsed)"
   using Vinted's modern endpoint `https://api.vinted.co.uk/svc-catalogue/items`.
2. **Depop is blocked on purpose by Depop.** Its v3 search API returns `410 Deprecated endpoint`,
   and both the v2 API and the `depop.com/search` page return `403 Forbidden - Depop` to any
   non-browser client. That is a deliberate access control. **Do not try to get around it**: no
   browser-fingerprint impersonation, no proxies, no headless-browser workarounds. Depop stays
   disabled (`platforms.depop.enabled: false`).
3. **Problem the owner reported: alerts were for listings that had been up a long time.** The
   goal is to hear about uploads within seconds. The causes:
   - Vinted's "newest first" feed also contains **old listings that get bumped, promoted or
     reshuffled** into view. The bot had never seen them, so it treated them as new.
   - Checks ran only every ~20 s, and the age filter allowed listings up to 120 minutes old.
4. **The fix in this update ("snipe mode"):**
   - **Strict new-upload detection.** Vinted item IDs only ever increase. Each search remembers
     the highest ID it has seen (`watermark` table in `sniper.db`) and only alerts on listings
     with a higher ID. Old items resurfacing in the feed are logged as `skip ...: old listing
     resurfaced` and never alert.
   - **No grace window** (`fresh_grace_minutes: 0`) and a **3-minute hard ceiling**
     (`max_listing_age_minutes: 3`), so nothing stale ever alerts, even after downtime.
   - **Faster checks:** every ~6 s per watch (`interval_seconds: 6`), never more than one request
     per 3 s to Vinted (`min_gap_seconds: 3`).
   - **Adaptive slow-down:** if Vinted answers 429/403, the request gap doubles (up to 8×) and
     recovers gradually after ~60 clean checks. This protects the owner's home IP from a ban.
   - **Measured speed:** `/status` in Telegram and a 💓 heartbeat line every 5 minutes report
     "new uploads spotted ~N s after upload".
   - **Bug fixed:** a search with zero results at startup (e.g. a rare item) silently dropped
     its first new upload.
   - **New rule `any_of`** (at least one of these words must appear). Blank list entries such as
     `sizes: [""]` are now ignored. Previously they blocked every listing.
   - **New command** `python -m sniper feedcheck <query>` shows how fresh Vinted's feed really is.
   - **New tool** `tools/migrate_config.py` updates `config.yaml` to the new settings and keeps
     every secret.

Test status of this package: **63 automated tests pass**, and an end-to-end simulation passes.
The simulation runs the real bot against fake Vinted/Depop/Telegram servers, with decoys, an old
listing bumped into the feed, and a simulated 403 outage. Median upload→alert time was ~3 s,
with no misses, no stale alerts and no duplicates.

## 3. Steps to apply the update

Run everything from the install folder, e.g. `C:\ResaleSniper`, the folder containing
`start.bat`. This zip may have been extracted somewhere else. Call that location `<PKG>`.

**Step 0: Stop the bot.** Ask the owner to close the "Resale Sniper" window if it's running (or
check with `tasklist | findstr python`). Don't kill unrelated Python processes.

**Step 1: Back up the owner's files.**
```bat
mkdir backup_before_update 2>nul
copy /Y config.yaml backup_before_update\  2>nul
copy /Y watches.yaml backup_before_update\ 2>nul
copy /Y sniper.db backup_before_update\    2>nul
```

**Step 2: Copy the new code over the install, excluding the personal files.**
```bat
robocopy "<PKG>" "." /E /XF config.yaml watches.yaml *.db *.db-wal *.db-shm *.log UPDATE_FOR_CLAUDE.md /XD .git .venv backup_before_update
```
robocopy exit codes 0–7 mean success. 8 or higher means failure: stop and report.

**Step 3: Update the dependencies.**
```bat
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pip install pytest pytest-asyncio
```
If `.venv` is missing, run `install.bat` instead and continue.

**Step 4: Migrate `config.yaml`.** The script makes a timestamped backup, keeps all
tokens/ids/domains, and only changes speed, freshness and Depop:
```bat
.venv\Scripts\python tools\migrate_config.py config.yaml
```
Expected changes include `poll.interval_seconds -> 6`, `max_listing_age_minutes -> 3`,
`fresh_grace_minutes -> 0`, `platforms.depop.enabled -> False`. Show the owner the "changed:"
list. It never prints secrets.

**Step 5: Check `watches.yaml`.**
```bat
.venv\Scripts\python -m sniper list
```
- If it errors, fix the YAML. Show the owner the exact problem before editing, and keep their
  intent.
- If the owner has a Coach-bag watch like `query: coach bag` with `sizes: [""]` and
  `min_condition: satisfactory`, suggest (and with their OK, apply) the improved version below.
  It catches purses, totes, crossbodies and the rest, and skips wallets and fakes:
  ```yaml
  watches:
    - id: coach
      query: coach
      brands: [coach]
      any_of: [bag, purse, handbag, crossbody, tote, shoulder, satchel, clutch, baguette, hobo, wristlet, pochette]
      max_price: 200
      exclude: [replica, fake, dupe, style, inspired, wallet, keyring, charm, strap only]
  ```
  `sizes: [""]` is harmless now but pointless for bags. `min_condition: satisfactory` filters
  nothing, since it's the lowest tier.
- Advise the owner that **fewer, broader watches give faster alerts**. All watches share one
  request budget: 1–2 watches → each checked every ~6 s, 4 watches → every ~12 s.

**Step 6: Run the test suite.**
```bat
.venv\Scripts\python -m pytest -q
```
Expect `63 passed`. Optionally run the end-to-end simulation (~1.5 min):
```bat
.venv\Scripts\python tools\e2e_simulation.py --interval 6 --uploads 6 --chaos
```
Expect `PASS` with "missed: none", "false positives: none", "duplicates: none".

**Step 7: Run the live checks.**
```bat
.venv\Scripts\python -m sniper doctor
```
Expect `✅ vinted: ... listings` and `ℹ️ depop: disabled`. Then:
```bat
.venv\Scripts\python -m sniper feedcheck coach
```
It prints the feed in the order Vinted returns it, with listing ages. It then waits 90 s and
labels each new item as `NEW upload` or `old item resurfaced`. Save this output and give it to
the owner. It shows how quickly Vinted itself makes new uploads visible.

If Telegram is configured, also run `.venv\Scripts\python -m sniper test-notify`. The owner's
phone should get a TEST ALERT with a Buy button.

**Step 8: Restart the bot.** Ask the owner to double-click `start.bat`, or run
`start "Resale Sniper" start.bat`. In the window, within ~30 s:
- `loaded N watch(es)`, `vinted: using modern endpoint ...`, `[<id>/vinted] baseline: ...`.
  The baseline line is expected: listings already live at startup are deliberately skipped.
- Later: `💓 alive · vinted: N checks OK ...` every 5 minutes.
- `skip ...: old listing resurfaced` lines are good. They're stale items correctly ignored.

## 4. What to report back to the owner

1. Which steps succeeded, with the test result line, the `doctor` output and the `feedcheck`
   output.
2. Remind them they can send `/status` to their Telegram bot any time. After a while it shows
   `⚡ new uploads spotted ~N s after upload`. That number is the real end-to-end speed. It's
   bounded by how quickly Vinted shows new items in search, plus up to one check interval, plus
   ~1 s delivery.
3. If anything failed: the exact error, and that their backups are in `backup_before_update\`
   and `config.yaml.bak-*`.

## 5. Rules

- Never print, log or commit the Telegram bot token, chat id or webhook URLs.
- Don't make the bot faster than `min_gap_seconds: 3` unless the owner explicitly asks and
  accepts the risk of a temporary Vinted IP block. The adaptive slow-down must stay in.
- Don't build anything that circumvents Depop's (or Vinted's) bot protection.
- Don't delete `sniper.db`. It's what prevents duplicate and stale alerts.

## 6. Quick reference

| Command (`.venv\Scripts\python -m sniper ...`, or `sniper.bat ...`) | Purpose |
|---|---|
| `run` (or `start.bat`) | Start watching |
| `doctor` | Connectivity + parsing check |
| `feedcheck <query>` | How fresh Vinted's newest-first feed is |
| `search "<query>" --max 200` | Preview the newest results and why each passes or fails the rules |
| `list` / `add <words> max=.. size=..` | Show / add watches |
| `test-notify` | Send a test alert |
| `telegram-setup --token <T>` | Link Telegram |

Telegram commands: `/add coach brand=coach any=bag,purse,tote max=200`, `/list`, `/pause <id>`,
`/resume <id>`, `/remove <id>`, `/status`, `/recent`, `/help`.
