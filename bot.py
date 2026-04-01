import logging
import os
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import config

logger = logging.getLogger("deal-finder.bot")

# Shared state — updated by orchestrator
scraper_status: dict[str, datetime | None] = {
    "vinted": None,
    "facebook": None,
    "skelbiu": None,
}
alerts_paused: bool = False
scrapers_stopped: bool = False

# Per-platform daily stats (reset at midnight)
_EMPTY_PLATFORM_STATS = {"checked": 0, "sent": 0, "flagged": 0, "skipped": 0, "errors": 0}
daily_stats: dict[str, dict[str, int]] = {
    "vinted": {**_EMPTY_PLATFORM_STATS},
    "facebook": {**_EMPTY_PLATFORM_STATS},
    "skelbiu": {**_EMPTY_PLATFORM_STATS},
}
_stats_date: str | None = None


def record_stat(platform: str, verdict: str):
    """Record an AI verdict for a platform. Call after each listing is processed."""
    global _stats_date
    from datetime import date
    today = date.today().isoformat()
    if _stats_date != today:
        # Reset at midnight
        for p in daily_stats:
            daily_stats[p] = {**_EMPTY_PLATFORM_STATS}
        _stats_date = today

    if platform not in daily_stats:
        daily_stats[platform] = {**_EMPTY_PLATFORM_STATS}

    daily_stats[platform]["checked"] += 1
    if verdict == "SEND":
        daily_stats[platform]["sent"] += 1
    elif verdict == "SEND_FLAGGED":
        daily_stats[platform]["flagged"] += 1
    elif verdict == "SKIP":
        daily_stats[platform]["skipped"] += 1
    else:
        daily_stats[platform]["errors"] += 1


# ── Message formatting ──────────────────────────────────────────────


def format_deal_message(verdict: dict, listing: dict) -> str:
    v = verdict
    platform = listing["platform"].capitalize()
    location = listing.get("seller_location") or ""

    if v["verdict"] == "SEND":
        header = f"🟢 GOOD DEAL · {platform}"
    elif v["verdict"] == "SEND_NEGOTIATE":
        header = f"🔵 NEGOTIATE · {platform}"
    else:
        header = f"🟡 FLAGGED DEAL · {platform}"
    if location:
        header += f" · {location}"

    # Model line
    model_line = v.get("model_name") or listing["title"]
    if v.get("model_id"):
        model_line += f" ({v['model_id']})"

    # Specs line
    specs_parts = []
    if v.get("year"):
        specs_parts.append(str(v["year"]))
    if v.get("processor"):
        specs_parts.append(v["processor"])
    if v.get("ram"):
        specs_parts.append(v["ram"])
    if v.get("storage"):
        specs_parts.append(v["storage"])
    if v.get("screen_size"):
        specs_parts.append(v["screen_size"])
    specs_line = " · ".join(specs_parts) if specs_parts else ""

    # Condition
    repairs = v.get("repairs_needed", [])
    if v.get("is_broken") and repairs:
        repair_str = ", ".join(r.replace("_", " ") for r in repairs)
        condition_line = f"🔧 Broken — {repair_str}"
        if "motherboard_water_damage" in repairs:
            condition_line += " ⚠️"
    elif v.get("condition_notes"):
        condition_line = f"📦 {v['condition_notes']}"
    else:
        condition_line = "📦 Working"

    # Price + profit
    price = listing.get("price", "?")
    sell_price = v.get("sell_price")
    net_profit = v.get("net_profit")
    roi = v.get("roi_percent")

    if sell_price and net_profit is not None:
        price_line = f"💶 €{price} → Sell: €{sell_price}"
        hourly = v.get("effective_hourly_rate")
        hourly_str = f" · €{hourly:.0f}/hr" if hourly is not None else ""
        profit_line = f"💰 Profit: €{net_profit:.0f} · ROI: {roi:.0f}%{hourly_str}"
    else:
        price_line = f"💶 €{price}"
        profit_line = ""

    # Negotiate info
    neg = v.get("negotiate")
    if neg and v["verdict"] == "SEND_NEGOTIATE":
        negotiate_line = f"🎯 Negotiate to €{neg['target_price']} → profit €{neg['profit_if_negotiated']:.0f}, ROI {neg['roi_if_negotiated']:.0f}%"
    else:
        negotiate_line = ""

    # Cost breakdown
    cb = v.get("cost_breakdown", {})
    cost_parts = []
    if cb.get("buyer_protection_fee"):
        cost_parts.append(f"Buyer fee: €{cb['buyer_protection_fee']:.2f}")
    if cb.get("pickup_cost"):
        cost_parts.append(f"Pickup: €{cb['pickup_cost']:.0f}")
    if cb.get("repair_total"):
        cost_parts.append(f"Repair: €{cb['repair_total']:.0f}")
    if cb.get("selling_fee"):
        cost_parts.append(f"Selling: €{cb['selling_fee']:.0f}")
    costs_line = f"📋 {' · '.join(cost_parts)}" if cost_parts else ""

    # Scam risk
    scam_risk = v.get("scam_risk", "low").upper()
    if scam_risk == "LOW":
        risk_line = f"✅ Scam risk: {scam_risk}"
    else:
        risk_line = f"⚠️ Scam risk: {scam_risk}"

    flag_lines = []
    for flag in v.get("scam_flags", []):
        flag_lines.append(f"• {flag}")

    # Seller info
    seller_parts = []
    reviews = listing.get("seller_reviews")
    if reviews is not None:
        neg = listing.get("seller_negative_reviews")
        if neg is not None and neg > 0:
            seller_parts.append(f"{reviews} reviews ({neg} negative)")
        else:
            seller_parts.append(f"{reviews} reviews")
    joined = listing.get("seller_joined")
    if joined:
        seller_parts.append(f"since {joined}")
    seller_line = f"👤 {' · '.join(seller_parts)}" if seller_parts else ""

    # Verdict reason
    reason = v.get("verdict_reason", "")

    # Assemble
    parts = [header, "", model_line]
    if specs_line:
        parts.append(specs_line)
    parts.extend([condition_line, "", price_line])
    if profit_line:
        parts.append(profit_line)
    if negotiate_line:
        parts.append(negotiate_line)
    if costs_line:
        parts.append(costs_line)
    parts.append("")
    parts.append(risk_line)
    if flag_lines:
        parts.extend(flag_lines)
    if seller_line:
        parts.append(seller_line)
    if reason:
        parts.extend(["", reason])
    parts.extend(["", f"🔗 {listing['url']}"])

    return "\n".join(parts)


def format_health_alert(scraper_name: str, consecutive_failures: int, last_success: datetime | None) -> str:
    if last_success:
        delta = datetime.now(timezone.utc) - last_success
        hours = delta.total_seconds() / 3600
        minutes = delta.total_seconds() / 60
        if hours >= 1:
            ago = f"{hours:.0f}h {minutes % 60:.0f}m ago"
        else:
            ago = f"{minutes:.0f}m ago"
    else:
        ago = "never"

    name = scraper_name.capitalize()
    return (
        f"🔴 SCRAPER DOWN · {name}\n\n"
        f"{name} scraper failed {consecutive_failures} times in a row.\n"
        f"Likely cause: {'auth session expired' if scraper_name == 'facebook' else 'network or API error'}.\n\n"
        f"Last success: {ago}"
    )


# ── Telegram send functions ─────────────────────────────────────────


async def send_message(app: Application, text: str):
    await app.bot.send_message(
        chat_id=config.TELEGRAM_CHAT_ID,
        text=text,
        disable_web_page_preview=True,
    )


async def send_deal(app: Application, verdict: dict, listing: dict):
    if alerts_paused:
        logger.info(f"Alert paused, skipping {listing['id']}")
        return
    text = format_deal_message(verdict, listing)
    await send_message(app, text)
    logger.info(f"Sent {verdict['verdict']} alert for {listing['id']}")


async def send_health_alert(app: Application, scraper_name: str, consecutive_failures: int, last_success: datetime | None):
    text = format_health_alert(scraper_name, consecutive_failures, last_success)
    await send_message(app, text)
    logger.warning(f"Sent health alert for {scraper_name}")


# ── Command handlers ────────────────────────────────────────────────


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("alive ✅")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import db

    lines = ["📊 Scraper Status\n"]
    for name, last in scraper_status.items():
        if last:
            delta = datetime.now(timezone.utc) - last
            minutes = delta.total_seconds() / 60
            lines.append(f"  {name.capitalize()}: ✅ {minutes:.0f}m ago")
        else:
            lines.append(f"  {name.capitalize()}: ❌ never ran")

    # Per-platform listing counts (today + total)
    try:
        today_counts = await db.get_today_counts()
        total_counts = await db.get_total_counts()
    except Exception:
        today_counts = {}
        total_counts = {}

    lines.append("\n📋 Listings:")
    for name in ("vinted", "facebook", "skelbiu"):
        today = today_counts.get(name, 0)
        total = total_counts.get(name, 0)
        lines.append(f"  {name.capitalize()}: {today} today ({total:,} total)")

    # AI verdict stats
    total_sent = sum(s["sent"] for s in daily_stats.values())
    total_flagged = sum(s["flagged"] for s in daily_stats.values())
    total_skipped = sum(s["skipped"] for s in daily_stats.values())
    total_errors = sum(s["errors"] for s in daily_stats.values())
    lines.append(f"\n🤖 AI Today: ✅ {total_sent} SEND · ⚠️ {total_flagged} FLAGGED · ❌ {total_skipped} SKIP · 💀 {total_errors} errors")

    lines.append(f"\nAlerts: {'⏸️ paused' if alerts_paused else '▶️ active'}")
    lines.append(f"Scrapers: {'🛑 stopped' if scrapers_stopped else '🟢 running'}")
    await update.message.reply_text("\n".join(lines))


async def cmd_errors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_path = os.path.join(config.LOG_DIR, "scraper.log")
    if not os.path.exists(log_path):
        await update.message.reply_text("No log file found.")
        return

    with open(log_path, "r") as f:
        lines = f.readlines()

    error_lines = [l.rstrip() for l in lines if "[WARNING]" in l or "[ERROR]" in l]
    recent = error_lines[-20:] if error_lines else []

    if recent:
        await update.message.reply_text("🔴 Recent errors:\n\n" + "\n".join(recent))
    else:
        await update.message.reply_text("✅ No warnings or errors in current log.")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global alerts_paused
    alerts_paused = True
    logger.info("Alerts paused by user")
    await update.message.reply_text("⏸️ Alerts paused. Scrapers still running.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global alerts_paused
    alerts_paused = False
    logger.info("Alerts resumed by user")
    await update.message.reply_text("▶️ Alerts resumed.")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global scrapers_stopped
    scrapers_stopped = True
    logger.info("Scrapers stopped by user via /stop")
    await update.message.reply_text("🛑 Scrapers stopped. No polling, no API calls.\nUse /start to resume.")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global scrapers_stopped
    scrapers_stopped = False
    logger.info("Scrapers started by user via /start")
    await update.message.reply_text("🟢 Scrapers started. Polling and AI analysis resumed.")


# ── Bot setup ───────────────────────────────────────────────────────


def create_bot_app() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("errors", cmd_errors))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("start", cmd_start))
    return app
