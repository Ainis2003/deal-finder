# MacBook Deal Scraper — Claude Code Project Brief

## Overview
Automated MacBook deal-finding system that monitors Vinted.lt, Facebook Marketplace (Lithuania), and Skelbiu.lt in near real-time. Every new listing is passed through Claude Sonnet for scam detection and full profit calculation. Qualifying deals are sent as Telegram alerts. Runs 24/7 on a Mac Mini M1 (macOS, 16GB RAM, 256GB SSD), managed remotely via SSH.

---

## Hardware & OS
- **Device:** Mac Mini M1, macOS (default OS, do not change)
- **Remote access:** SSH
- **Always-on:** yes, runs 24/7

---

## Repository Setup
Initialize a Git repository as part of project setup. Include a `.gitignore` that excludes:
- `config.py` (contains secrets)
- `fb_auth_state.json` (Facebook session)
- `*.db` (SQLite database)
- `logs/`
- `.venv/`

---

## Project Structure
```
macbook-scraper/
├── scrapers/
│   ├── vinted.py            # Polls vinted.lt API for new MacBook listings
│   ├── facebook.py          # Custom Playwright-based FB Marketplace scraper
│   └── skelbiu.py           # Adapter wrapping owner's existing Skelbiu scraper
├── filter.py                # Claude Sonnet AI filtering + profit calculation
├── bot.py                   # Telegram alert sender + command listener
├── orchestrator.py          # Main process — coordinates all scrapers
├── data/
│   ├── prices.json          # MacBook resale prices (owner fills in)
│   ├── repair_costs.json    # Repair cost per model + repair type (owner fills in)
│   ├── disassembly_costs.json  # Technician disassembly fee per model (owner fills in)
│   └── shipping_costs.json  # Shipping cost estimates (owner fills in)
├── seen.db                  # SQLite deduplication store (gitignored)
├── config.py                # All secrets and settings (gitignored)
├── setup_fb_auth.py         # One-time FB login script to save session
├── setup_check.py           # Pre-run verification script
├── requirements.txt
├── .gitignore
└── com.macbook-scraper.plist   # launchd plist for auto-start on boot
```

---

## Data Files (owner-maintained, in `data/`)

All four files below are filled in and maintained by the owner. The AI receives all of them in every prompt. They are never auto-updated by the system. Exact format to be decided by owner — Claude Code should ask the owner how they want to structure these before creating them.

### `data/prices.json`
Resale prices on the Lithuanian market. Keyed by MacBook model name. Contains working sell estimate and broken (post-repair) sell estimate per model. Owner updates ~monthly.

### `data/repair_costs.json`
Repair cost per Apple model identifier (e.g. A2337) and repair type (LCD replacement, battery, motherboard, water damage, keyboard, etc.). Values provided by owner based on pricing from their Chinese repair contact.

### `data/disassembly_costs.json`
Flat disassembly fee per model, charged by owner's local technician to remove the part before it is shipped to China for repair.

### `data/shipping_costs.json`
Shipping cost range for sending a part to China (typically €5–10), and typical customs cost on return from China (typically up to €5). Owner fills in based on real experience.

---

## Business Logic & Cost Model

This is the core of the system. The AI must calculate **true net profit** and **effective hourly rate** for every deal, not just a rough margin. All costs below must be deducted before arriving at a verdict.

### Full cost stack per deal

#### Buying costs (always apply)
| Cost | Platform | Value |
|---|---|---|
| Purchase price | All | from listing |
| Vinted buyer protection fee | Vinted only | €0.70 + 5% if price < €500; 2% if price ≥ €500 |
| Fuel (driving to pickup) | FB + Skelbiu | €0.10/km × estimated round-trip km |
| Owner time (pickup) | FB + Skelbiu | always 1 hour (meeting in city assumed) |

#### Repair costs (only if broken)
| Cost | Value |
|---|---|
| Disassembly fee | from `disassembly_costs.json` by model |
| Repair cost | from `repair_costs.json` by model + repair type |
| Shipping part to China | from `shipping_costs.json` (use midpoint of min/max) |
| Customs on return | from `shipping_costs.json` |
| Owner time (repair coordination) | add 0.5 hr to total time |

#### Selling costs (always apply)
| Cost | Platform | Value |
|---|---|---|
| Skelbiu listing fee | Skelbiu only | €10 (use midpoint of €5–15 range) |
| Facebook listing fee | FB | €0 |
| Vinted listing fee | Vinted | €0 |

### Net profit formula
```
total_costs = purchase_price
            + buying_fees (platform-specific)
            + fuel_cost (if FB/Skelbiu)
            + repair_costs (if broken: disassembly + repair + shipping_to_china + customs_return)
            + selling_fees (platform-specific)

net_profit = sell_estimate - total_costs

roi_percent = (net_profit / purchase_price) * 100
```

### Time and effective hourly rate
The AI must calculate total owner time invested and the resulting effective hourly rate:

```
total_hours = 1.0   (pickup, always for FB/Skelbiu)
            + 0.5   (if repair needed: coordination with technician and China contact)
            + 0.0   (Vinted: no pickup needed, no extra time)

effective_hourly_rate = net_profit / total_hours
```

The AI does NOT apply a hard hourly rate. Instead it calculates `effective_hourly_rate` and flags it clearly:
- If `effective_hourly_rate >= 20` → acceptable, no flag
- If `effective_hourly_rate < 20` → flag in verdict: "Effective hourly rate is €X/hr — below €20 threshold"

### Repair risk multiplier
The AI applies a risk commentary (not a numerical multiplier to the raw figures) based on repair type. Raw numbers stay accurate. Risk is expressed in the `verdict_reason` text:
- **Screen / battery / keyboard:** routine repair, predictable outcome → no special flag
- **Motherboard repair:** high risk — board shipped to China, repair can fail, part can be lost in transit → flag as HIGH REPAIR RISK in verdict
- **Water damage:** very high risk — outcome unpredictable, repair may not succeed → flag as VERY HIGH REPAIR RISK in verdict

For motherboard and water damage repairs, the AI must mention in `verdict_reason` that the broken_sell_estimate assumes a successful repair, and the actual outcome may differ.

### Capital lock-up awareness
High capital requirement with low ROI is worse than low capital with same ROI. The AI must mention this in `verdict_reason` for deals over €1,000 purchase price. Example:
- MacBook Pro M3 Max at €3,500 with €200 net profit = 5.7% ROI, €100/hr → SKIP even if raw number sounds good
- MacBook Air M1 at €350 with €180 net profit = 51% ROI, €180/hr → excellent

---

## Scraper 1 — Skelbiu

### Context
Owner already has a working Skelbiu scraper: single Python script, refreshes every 15 seconds, extracts title / price / description / location / URL / post datetime, currently sends its own Telegram message and saves to file.

### Integration approach
Do NOT rewrite the Skelbiu scraper. Create `scrapers/skelbiu.py` as a thin adapter that:
1. Imports the existing script's core logic (or subprocess-calls it if tightly coupled)
2. Converts output to the normalized listing format (see below)
3. Suppresses the existing script's own Telegram sends — this project handles all Telegram output

### What to ask the owner at start of coding session
Share the existing Skelbiu script so the adapter can be written correctly. Confirm whether it can be imported as a module or must be subprocess-called.

---

## Scraper 2 — Vinted.lt

### Source
Reference repo for API patterns: https://github.com/Fuyucch1/Vinted-Notifications

### Integration approach
Do NOT run Fuyucch1 as Docker. Extract and reimplement the Vinted API polling logic directly into `scrapers/vinted.py`. One Python process, no Docker dependency.

### Details
- **Domain:** `vinted.lt` only
- **Search term:** `"macbook"` (single term, broad)
- **Refresh rate:** every 30–45 seconds with random jitter (±10s)
- **Auth:** cookie-based, replicate the cookie-fetching approach from Fuyucch1 repo
- **Output:** normalized listing dict

---

## Scraper 3 — Facebook Marketplace

### Approach
Custom Playwright scraper written from scratch in `scrapers/facebook.py`. Use OpenClaw as reference only: https://github.com/Scratchycarl/OpenClaw_Facebook_Marketplace_Scraper

### Details
- **Cities:** Kaunas, Vilnius, Klaipėda — scrape all three
- **Search term:** `"macbook"`
- **Refresh rate:** 10 minutes per city, staggered (city 1 at T+0, city 2 at T+10, city 3 at T+20, repeat)
- **Auth:** manual login once via `setup_fb_auth.py` → session saved to `fb_auth_state.json` → Playwright loads on every run
- **Fields to extract:** title, price, description (if visible), seller name, seller join date (if visible), seller location, listing URL, first image URL, timestamp seen
- **Session expiry:** if Playwright gets redirected to login or cannot find listings → treat as auth failure → health alert after 3 consecutive failures

---

## Normalized Listing Format
Every scraper outputs this exact dict before the AI filter:

```python
{
    "id": "vinted_12345678",         # platform prefix + platform's own listing ID
    "platform": "vinted",            # "vinted" | "facebook" | "skelbiu"
    "title": "MacBook Air M1 8GB 256GB Space Gray",
    "description": "Parduodu macbook...",  # full raw description, any language
    "price": 380,                    # integer euros. None if missing or unparseable → SKIP immediately
    "condition": "good",             # raw string from seller, or None
    "seller_reviews": 47,            # int or None
    "seller_joined": "2021-03",      # "YYYY-MM" string or None
    "seller_location": "Kaunas",     # city name string or None
    "distance_km": None,             # estimated km from Kaunas if calculable, else None
    "url": "https://...",
    "image_url": "https://...",      # first image only, or None
    "listed_at": "2025-03-18T10:23:00",  # ISO 8601
    "platform_raw": {}               # optional dump of raw fields for debugging
}
```

**If `price` is `None`: skip the listing before it reaches the AI. Do not send to Telegram. Log as DEBUG.**

---

## Deduplication (`seen.db`)
```sql
CREATE TABLE seen_listings (
    id TEXT PRIMARY KEY,
    platform TEXT,
    seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```
- Check `id` before AI filter. If seen → discard silently.
- Insert immediately on first sight regardless of verdict.
- Purge rows older than 30 days on startup.

---

## AI Filter (`filter.py`)

### Model
`claude-sonnet-4-20250514`

### Input to Claude (every call)
1. Full normalized listing dict
2. Full contents of all four data JSON files (`prices.json`, `repair_costs.json`, `disassembly_costs.json`, `shipping_costs.json`)
3. System prompt with all rules, cost formulas, scam signals, and verdict logic defined in this brief

### Output from Claude
Strict JSON only, no extra text. Claude Code should define the exact schema during implementation. The response must include at minimum: whether it's a MacBook, identified model and confidence level, Apple model ID, whether it's broken and repair type if so, listing price, sell estimate used, price source (prices_json or ai_estimate), full itemised cost breakdown, total costs, net profit, capital required, ROI %, total hours, effective hourly rate, hourly rate flag, repair risk level, list of scam flags, scam risk level, verdict, verdict reason, and detected language.

### Verdict values
- `"SEND"` — passes all thresholds, low or medium scam risk
- `"SEND_FLAGGED"` — passes profit thresholds but has notable scam or repair risk signals
- `"SKIP"` — fails profit thresholds OR scam risk is overwhelming

### Profit thresholds (after ALL costs deducted)

**Working MacBook:**
- ROI ≥ 15% AND net profit ≥ €100 AND effective_hourly_rate ≥ €20 → eligible for SEND
- Any threshold missed → SKIP

**Broken MacBook:**
- ROI ≥ 30% AND net profit ≥ €150 AND effective_hourly_rate ≥ €20 → eligible for SEND
- Any threshold missed → SKIP

If `price_source` is `"ai_estimate"` (model not in `prices.json`), downgrade any SEND to SEND_FLAGGED and note it in `verdict_reason`.

### Scam signal detection

**All platforms:**
- Mentions WhatsApp, Viber, direct bank transfer, "contact outside platform", "pay first"
- Price > 70% below `working_sell_estimate` with no damage explanation
- Description under 10 words with no specs

**Vinted-specific:**
- Account < 30 days old AND 0 reviews → high scam risk
- "Shipping not through Vinted" or payment outside platform → high scam risk

**Facebook-specific:**
- `seller_joined` < 3 months ago → flag
- Seller location outside Lithuania → flag (owner only buys locally on FB/Skelbiu)
- `distance_km` > 150 → flag
- Only 1 photo → minor flag

**Skelbiu-specific:**
- Description entirely in Russian, no Lithuanian → minor flag
- Message-only contact (no phone number) → minor flag

**Scam risk levels:**
- `"low"` — 0 flags or 1 minor flag
- `"medium"` — 2 minor flags OR 1 major flag → verdict can be SEND_FLAGGED
- `"high"` — 2+ major flags OR 3+ minor flags → verdict always SKIP

### Language handling
AI handles any language (Lithuanian, Russian, English, etc.) natively. `language_detected` records it. No special preprocessing needed.

---

## Telegram Bot (`bot.py`)

### Credentials
Stored in `config.py` (owner already has these):
```python
TELEGRAM_BOT_TOKEN = "..."
TELEGRAM_CHAT_ID = "..."
```

### Alert format — SEND

```
🟢 GOOD DEAL · Vinted

MacBook Air M1 8GB 256GB (A2337)
💶 Price: €380 → Sell est.: €600
💰 Net profit: €200 · ROI: 52.7% · €200/hr

📋 Costs breakdown:
  Buyer protection fee: €19.70
  Selling fee: €0
  Repair: none

✅ Scam risk: LOW
👤 47 reviews · Seller since Mar 2021

Clean M1 Air, strong ROI. No red flags.

🔗 https://vinted.lt/...
```

### Alert format — SEND_FLAGGED

```
🟡 FLAGGED DEAL · Facebook · Vilnius

MacBook Pro 14" M1 Pro (broken screen)
💶 Price: €500 → Sell est. after repair: €1,050
💰 Net profit: €310 · ROI: 62% · €207/hr

📋 Costs breakdown:
  Fuel: €0 (city pickup)
  Disassembly: €25
  Repair (LCD): €80
  Shipping to China: €7.50
  Customs return: €5
  Selling fee: €0

⚠️ Scam risk: MEDIUM
• Seller joined FB: Jan 2025 (3 months ago)
• Only 1 photo
⚠️ Repair risk: ROUTINE (screen replacement)

Strong numbers but new seller. Verify before buying.

🔗 https://facebook.com/marketplace/...
```

### Alert format — scraper health failure

```
🔴 SCRAPER DOWN · Facebook

FB Marketplace scraper failed 3 times in a row.
Likely cause: auth session expired.

Fix: ssh into Mac Mini → python setup_fb_auth.py
Last success: 2h 14m ago
```

### Telegram commands (owner messages the bot)
- `/status` — last successful scrape time per platform + listings seen today
- `/errors` — last 20 WARNING/ERROR lines from current log file
- `/pause` — pause Telegram alerts (scrapers keep running, just no messages sent)
- `/resume` — resume alerts
- `/ping` — bot replies "alive ✅"

---

## Orchestrator (`orchestrator.py`)
- Runs all three scrapers concurrently (`asyncio` or threads)
- Skelbiu: continuous loop, 15s interval
- Vinted: continuous loop, 30–45s with random jitter
- Facebook: 10 min per city, staggered across 3 cities
- Pipeline per listing: dedup check → AI filter → Telegram send
- Track consecutive failure count per scraper → health alert at 3 failures
- Telegram command listener runs in parallel
- Graceful shutdown on SIGTERM/SIGINT

---

## Logging
- File: `logs/scraper_YYYY-MM-DD.log`, daily rotation, 14 days retention
- Also print to console (stdout)
- Levels: DEBUG (every listing seen), INFO (every listing sent), WARNING (scraper errors), ERROR (failures)
- `/errors` command returns last 20 WARNING/ERROR lines from current log

---

## Config (`config.py`) — gitignored
```python
ANTHROPIC_API_KEY = ""
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""

FB_AUTH_STATE_PATH = "./fb_auth_state.json"
DATA_DIR = "./data"
DB_PATH = "./seen.db"
LOG_DIR = "./logs"

FB_CITIES = ["kaunas", "vilnius", "klaipeda"]
VINTED_DOMAIN = "vinted.lt"
VINTED_SEARCH_TERM = "macbook"

VINTED_POLL_INTERVAL_S = 40       # ±10s random jitter applied
FB_POLL_INTERVAL_PER_CITY_S = 600 # 10 min per city
SKELBIU_POLL_INTERVAL_S = 15

SCRAPER_FAILURE_ALERT_THRESHOLD = 3
LOG_RETENTION_DAYS = 14
SEEN_LISTING_RETENTION_DAYS = 30

FUEL_COST_PER_KM = 0.10           # €/km
DEFAULT_PICKUP_HOURS = 1.0        # hours added for every local pickup
REPAIR_COORDINATION_HOURS = 0.5  # extra hours if repair involved
MIN_ACCEPTABLE_HOURLY_RATE = 20   # €/hr — flag if below this
SKELBIU_LISTING_FEE = 10          # € midpoint of €5-15 range

# Vinted buyer protection fee formula:
# price < 500: 0.70 + price * 0.05
# price >= 500: price * 0.02
```

---

## Setup Scripts

### `setup_fb_auth.py`
- Opens Chromium via Playwright
- Navigates to facebook.com
- Waits for user to log in manually
- Saves session to `fb_auth_state.json`
- Prints confirmation and closes

### `setup_check.py`
Verifies everything before first run:
- All `config.py` values filled
- Anthropic API reachable
- Telegram bot working (sends test message)
- `fb_auth_state.json` exists
- All four data JSON files exist and have at least one non-zero entry
- Prints ✅ or ❌ per check

---

## Auto-start on Boot (macOS launchd)

`com.macbook-scraper.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.macbook-scraper</string>
  <key>ProgramArguments</key>
  <array>
    <string>/REPLACE/WITH/VENV/PATH/bin/python</string>
    <string>/REPLACE/WITH/PROJECT/PATH/orchestrator.py</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/REPLACE/WITH/PROJECT/PATH/logs/launchd.log</string>
  <key>StandardErrorPath</key>
  <string>/REPLACE/WITH/PROJECT/PATH/logs/launchd_error.log</string>
</dict>
</plist>
```

Install:
```bash
cp com.macbook-scraper.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.macbook-scraper.plist
```

---

## Build Order
Follow strictly — each step is testable before moving on:

1. Repo init, `.gitignore`, `requirements.txt`, `config.py` skeleton
2. `data/` folder with all four placeholder JSON files
3. `seen.db` schema + deduplication logic
4. `bot.py` — Telegram sender + command listener (test with `/ping`)
5. `scrapers/skelbiu.py` — adapter (**owner must share existing script first**)
6. `filter.py` — Claude Sonnet AI filter with full cost model (test with mock listing dicts)
7. Wire Skelbiu → filter → Telegram in `orchestrator.py` (first full working pipeline)
8. `scrapers/vinted.py` — Vinted.lt polling
9. Wire Vinted into orchestrator
10. `setup_fb_auth.py` + `scrapers/facebook.py` — Playwright FB scraper
11. Wire Facebook into orchestrator
12. `setup_check.py` — verification script
13. `com.macbook-scraper.plist` + launchd install instructions

---

## Explicitly Out of Scope
- No web dashboard
- No auto-buying
- No automatic price fetching from external sources
- No image analysis for scam detection
- No multi-user support
- No Docker
- No Vinted domains other than vinted.lt
- No automatic tax calculation (not declared, handled by owner)
