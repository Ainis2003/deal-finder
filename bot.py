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
listings_seen_today: int = 0
alerts_paused: bool = False


# ── Message formatting ──────────────────────────────────────────────


def format_deal_message(verdict: dict, listing: dict) -> str:
    v = verdict
    platform = listing["platform"].capitalize()
    location = listing.get("seller_location") or ""

    if v["verdict"] == "SEND":
        header = f"🟢 GOOD DEAL · {platform}"
    else:
        header = f"🟡 FLAGGED DEAL · {platform}"
    if location:
        header += f" · {location}"

    # Model line
    model_line = v.get("model_name") or listing["title"]
    if v.get("is_broken") and v.get("repair_type"):
        model_line += f" (broken {v['repair_type']})"
    if v.get("model_id"):
        model_line += f" ({v['model_id']})"

    # Price line
    if v.get("is_broken"):
        price_line = f"💶 Price: €{v['listing_price']} → Sell est. after repair: €{v['sell_estimate']}"
    else:
        price_line = f"💶 Price: €{v['listing_price']} → Sell est.: €{v['sell_estimate']}"

    # Profit line
    hourly = v.get("effective_hourly_rate")
    hourly_str = f"€{hourly:.0f}/hr" if hourly is not None else "N/A (no time)"
    profit_line = (
        f"💰 Net profit: €{v['net_profit']:.0f} · "
        f"ROI: {v['roi_percent']:.1f}% · {hourly_str}"
    )

    # Cost breakdown
    cb = v.get("cost_breakdown", {})
    cost_lines = ["📋 Costs breakdown:"]
    if cb.get("buyer_protection_fee"):
        cost_lines.append(f"  Buyer protection fee: €{cb['buyer_protection_fee']:.2f}")
    if cb.get("fuel_cost"):
        cost_lines.append(f"  Pickup cost: €{cb['fuel_cost']:.0f}")
    if cb.get("disassembly_fee"):
        cost_lines.append(f"  Disassembly: €{cb['disassembly_fee']:.0f}")
    if cb.get("repair_cost"):
        cost_lines.append(f"  Repair ({v.get('repair_type', '?')}): €{cb['repair_cost']:.0f}")
    if cb.get("shipping_to_china"):
        cost_lines.append(f"  Shipping to China: €{cb['shipping_to_china']:.2f}")
    if cb.get("customs_return"):
        cost_lines.append(f"  Customs return: €{cb['customs_return']:.2f}")
    if cb.get("selling_fee"):
        cost_lines.append(f"  Selling fee: €{cb['selling_fee']:.0f}")
    if len(cost_lines) == 1:
        cost_lines.append("  No extra costs")
    costs_block = "\n".join(cost_lines)

    # Scam + repair risk
    scam_risk = v.get("scam_risk", "low").upper()
    if scam_risk == "LOW":
        risk_line = f"✅ Scam risk: {scam_risk}"
    else:
        risk_line = f"⚠️ Scam risk: {scam_risk}"

    flag_lines = []
    for flag in v.get("scam_flags", []):
        flag_lines.append(f"• {flag}")

    if v.get("repair_risk") and v["repair_risk"] != "none":
        risk_label = v["repair_risk"].upper().replace("_", " ")
        repair_type = v.get("repair_type", "unknown")
        flag_lines.append(f"⚠️ Repair risk: {risk_label} ({repair_type})")

    if v.get("hourly_rate_flag"):
        flag_lines.append(f"⏱️ {v['hourly_rate_flag']}")

    flags_block = "\n".join(flag_lines)

    # Seller info
    seller_parts = []
    reviews = listing.get("seller_reviews")
    if reviews is not None:
        seller_parts.append(f"{reviews} reviews")
    joined = listing.get("seller_joined")
    if joined:
        seller_parts.append(f"Seller since {joined}")
    seller_line = f"👤 {' · '.join(seller_parts)}" if seller_parts else ""

    # Verdict reason
    reason = v.get("verdict_reason", "")

    # Assemble
    parts = [header, "", model_line, price_line, profit_line, "", costs_block, "", risk_line]
    if flags_block:
        parts.append(flags_block)
    if seller_line:
        parts.append(seller_line)
    parts.extend(["", reason, "", f"🔗 {listing['url']}"])

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
    lines = ["📊 Scraper Status\n"]
    for name, last in scraper_status.items():
        if last:
            delta = datetime.now(timezone.utc) - last
            minutes = delta.total_seconds() / 60
            lines.append(f"  {name.capitalize()}: ✅ {minutes:.0f}m ago")
        else:
            lines.append(f"  {name.capitalize()}: ❌ never ran")
    lines.append(f"\nListings seen today: {listings_seen_today}")
    lines.append(f"Alerts: {'⏸️ paused' if alerts_paused else '▶️ active'}")
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


# ── Bot setup ───────────────────────────────────────────────────────


def create_bot_app() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("errors", cmd_errors))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    return app
