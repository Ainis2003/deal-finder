import asyncio
import concurrent.futures
import json
import logging
import os
import random
import re
import signal
from datetime import datetime, timezone

import httpx

import bot
import config
import db
import filter as ai_filter
from log_setup import setup_logging
from scrapers import skelbiu

logger = logging.getLogger("deal-finder.orchestrator")

listing_queue: asyncio.Queue = asyncio.Queue()
shutdown_event = asyncio.Event()

# Single-thread executors for Playwright scrapers — must always run on the same thread
_fb_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="facebook")
_vinted_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="vinted")


# ── Pre-filter ────────────────────────────────────────────────────

MACBOOK_KEYWORDS = re.compile(
    r"\bmac\s*book\b|\bmacbook\b",
    re.IGNORECASE,
)

ACCESSORY_ONLY = re.compile(
    r"^(?:.*\b(?:case|cover|sleeve|charger|adapter|cable|hub|dock|dongle|"
    r"mouse|stand|bag|screen protector|keyboard cover|skin|sticker|"
    r"dėklas|kroviklis|laikiklis|stovelis|pelė|kabelis)\b.*)$",
    re.IGNORECASE,
)


def _pre_filter(listing: dict) -> bool:
    """Quick check: skip obvious non-MacBooks (accessories). Everything else goes to AI."""
    title = listing.get("title", "")
    price = listing.get("price")

    if price is None:
        logger.debug(f"Pre-filter skip {listing['id']}: no price")
        return False

    if not MACBOOK_KEYWORDS.search(title):
        logger.debug(f"Pre-filter skip {listing['id']}: no MacBook keyword in title")
        return False

    if ACCESSORY_ONLY.search(title):
        has_model_hint = re.search(r"\b(?:air|pro|m[1-5]|20[12]\d|intel|i[579])\b", title, re.IGNORECASE)
        if not has_model_hint:
            logger.debug(f"Pre-filter skip {listing['id']}: looks like accessory only")
            return False

    return True


# ── Listing logger (JSONL) ────────────────────────────────────────


def _log_listing(listing: dict, verdict: dict | None):
    """Append one JSON line to daily listing log file."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        log_path = os.path.join(config.LOG_DIR, f"listings_{today}.jsonl")
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "id": listing.get("id"),
            "platform": listing.get("platform"),
            "url": listing.get("url"),
            "title": listing.get("title"),
            "price": listing.get("price"),
            "verdict": verdict.get("verdict") if verdict else "ERROR",
            "model": verdict.get("model_name") if verdict else None,
            "profit": verdict.get("net_profit") if verdict else None,
            "reason": verdict.get("verdict_reason") if verdict else None,
        }
        with open(log_path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"Failed to write listing log: {e}")


# ── Internet connectivity check ──────────────────────────────────


async def _wait_for_internet(max_wait: int = 300):
    """Wait until internet is available. Retries with backoff up to max_wait seconds."""
    delay = 5
    total_waited = 0
    while total_waited < max_wait:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get("https://www.google.com", follow_redirects=True)
                if resp.status_code < 500:
                    return True
        except Exception:
            pass
        logger.warning(f"No internet connection, retrying in {delay}s...")
        await asyncio.sleep(delay)
        total_waited += delay
        delay = min(delay * 2, 60)
    return False


# ── Startup notification + backfill ───────────────────────────────


async def _startup(bot_app):
    """Send startup notification and backfill missed listings."""
    now = datetime.now(timezone.utc)
    last_seen = await db.get_last_seen_time()

    if last_seen:
        delta = now - last_seen
        hours = delta.total_seconds() / 3600
        minutes = delta.total_seconds() / 60
        if hours >= 1:
            downtime_str = f"{hours:.0f}h {minutes % 60:.0f}m"
        else:
            downtime_str = f"{minutes:.0f}m"
    else:
        downtime_str = None
        delta = None

    # Send startup message
    try:
        if downtime_str:
            msg = f"✅ Deal-finder started.\nDowntime: {downtime_str}\nScrapers resuming."
        else:
            msg = "✅ Deal-finder started (first run).\nScrapers initializing."
        await bot.send_message(bot_app, msg)
    except Exception as e:
        logger.error(f"Failed to send startup notification: {e}")

    # Backfill if downtime was >2 minutes and <24 hours
    if delta and 120 < delta.total_seconds() < 86400:
        logger.info(f"Backfilling missed listings from {downtime_str} downtime")
        try:
            await _backfill_vinted(bot_app)
        except Exception as e:
            logger.error(f"Vinted backfill failed: {e}")
        # Skelbiu and Facebook will catch up on their first normal poll


async def _backfill_vinted(bot_app):
    """Fetch Vinted listings to catch up after downtime. Single poll (newest_first) covers most cases."""
    from scrapers.vinted import _poll_sync

    loop = asyncio.get_event_loop()
    try:
        listings = await loop.run_in_executor(_vinted_executor, _poll_sync)
        if listings:
            for listing in listings:
                await listing_queue.put(listing)
            logger.info(f"Backfill: Vinted → {len(listings)} listings queued")
    except Exception as e:
        logger.warning(f"Backfill Vinted failed: {e}")


# ── Scraper loops ───────────────────────────────────────────────────


async def skelbiu_loop():
    consecutive_failures = 0
    while not shutdown_event.is_set():
        if bot.scrapers_stopped:
            await _sleep_or_shutdown(5)
            continue
        try:
            listings = await skelbiu.poll()
            consecutive_failures = 0
            bot.scraper_status["skelbiu"] = datetime.now(timezone.utc)
            for listing in listings:
                await listing_queue.put(listing)
        except NotImplementedError:
            logger.info("Skelbiu adapter not ready, skipping")
            await _sleep_or_shutdown(60)
            continue
        except Exception as e:
            consecutive_failures += 1
            logger.warning(f"Skelbiu failed ({consecutive_failures}x): {e}")
            if consecutive_failures >= config.SCRAPER_FAILURE_ALERT_THRESHOLD:
                await _send_health_alert("skelbiu", consecutive_failures)
        await _sleep_or_shutdown(config.SKELBIU_POLL_INTERVAL_S)


async def vinted_loop():
    from scrapers.vinted import _poll_sync

    loop = asyncio.get_event_loop()
    consecutive_failures = 0
    while not shutdown_event.is_set():
        if bot.scrapers_stopped:
            await _sleep_or_shutdown(5)
            continue
        try:
            listings = await loop.run_in_executor(_vinted_executor, _poll_sync)
            consecutive_failures = 0
            bot.scraper_status["vinted"] = datetime.now(timezone.utc)
            for listing in listings:
                await listing_queue.put(listing)
        except Exception as e:
            consecutive_failures += 1
            logger.warning(f"Vinted failed ({consecutive_failures}x): {e}")
            if consecutive_failures >= config.SCRAPER_FAILURE_ALERT_THRESHOLD:
                await _send_health_alert("vinted", consecutive_failures)

        jitter = random.randint(-15, 15)
        await _sleep_or_shutdown(config.VINTED_POLL_INTERVAL_S + jitter)


async def facebook_loop():
    from scrapers import facebook

    loop = asyncio.get_event_loop()
    consecutive_failures = 0
    while not shutdown_event.is_set():
        if bot.scrapers_stopped:
            await _sleep_or_shutdown(5)
            continue
        for city in config.FB_CITIES:
            if shutdown_event.is_set() or bot.scrapers_stopped:
                break
            try:
                listings = await loop.run_in_executor(_fb_executor, facebook.poll_city, city)
                consecutive_failures = 0
                bot.scraper_status["facebook"] = datetime.now(timezone.utc)
                for listing in listings:
                    await listing_queue.put(listing)
            except Exception as e:
                consecutive_failures += 1
                logger.warning(f"Facebook/{city} failed ({consecutive_failures}x): {e}")
                if consecutive_failures >= config.SCRAPER_FAILURE_ALERT_THRESHOLD:
                    await _send_health_alert("facebook", consecutive_failures)

            await _sleep_or_shutdown(config.FB_POLL_INTERVAL_PER_CITY_S)


# ── Listing processor ──────────────────────────────────────────────


async def process_listings(bot_app):
    while not shutdown_event.is_set():
        try:
            listing = await asyncio.wait_for(listing_queue.get(), timeout=5.0)
        except asyncio.TimeoutError:
            continue

        try:
            # 0. If stopped, drain queue but don't process
            if bot.scrapers_stopped:
                continue

            # 1. Pre-filter: skip obvious non-MacBooks
            if not _pre_filter(listing):
                continue

            # 2. Dedup
            if await db.is_seen(listing["id"]):
                logger.debug(f"Skipping {listing['id']}: already seen")
                continue

            # 3. Mark seen (with details for history)
            await db.mark_seen(
                listing["id"],
                listing["platform"],
                url=listing.get("url"),
                title=listing.get("title"),
                price=listing.get("price"),
            )

            # 4. Fetch details for Facebook listings (same thread as poll_city)
            if listing["platform"] == "facebook" and not listing.get("description"):
                from scrapers import facebook
                loop = asyncio.get_event_loop()
                listing = await loop.run_in_executor(_fb_executor, facebook.fetch_listing_details, listing)

            # 5. AI filter
            verdict = await ai_filter.analyze_listing(listing)
            if verdict is None:
                logger.warning(f"AI filter returned None for {listing['id']}, skipping")
                bot.record_stat(listing["platform"], "ERROR")
                _log_listing(listing, None)
                await db.update_verdict(listing["id"], "ERROR")
                continue

            # 6. Record stats + log + update DB
            bot.record_stat(listing["platform"], verdict["verdict"])
            _log_listing(listing, verdict)
            await db.update_verdict(listing["id"], verdict["verdict"])

            # 7. Send alerts
            if verdict["verdict"] in ("SEND", "SEND_FLAGGED", "SEND_NEGOTIATE"):
                await bot.send_deal(bot_app, verdict, listing)
            else:
                logger.debug(f"SKIP: {listing['id']} — {verdict.get('verdict_reason', '')[:80]}")

        except Exception as e:
            logger.error(f"Error processing {listing.get('id', '?')}: {e}", exc_info=True)
            try:
                bot.record_stat(listing.get("platform", "unknown"), "ERROR")
            except Exception:
                pass


# ── Helpers ─────────────────────────────────────────────────────────


async def _sleep_or_shutdown(seconds: float):
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=max(0, seconds))
    except asyncio.TimeoutError:
        pass


async def _send_health_alert(scraper_name: str, consecutive_failures: int):
    last_success = bot.scraper_status.get(scraper_name)
    try:
        await bot.send_health_alert(bot_app_instance, scraper_name, consecutive_failures, last_success)
    except Exception as e:
        logger.error(f"Failed to send health alert for {scraper_name}: {e}")


bot_app_instance = None


# ── Main ────────────────────────────────────────────────────────────


async def main():
    global bot_app_instance

    setup_logging()
    logger.info("deal-finder starting up")

    # Wait for internet
    if not await _wait_for_internet():
        logger.error("No internet after 5 minutes, starting anyway")

    # Init DB
    await db.init_db()
    await db.purge_old()
    logger.info("Database initialized, old entries purged")

    # Create Telegram bot
    bot_app_instance = bot.create_bot_app()
    await bot_app_instance.initialize()
    await bot_app_instance.start()
    await bot_app_instance.updater.start_polling()
    logger.info("Telegram bot started")

    # Startup notification + backfill
    await _startup(bot_app_instance)

    # Launch tasks
    tasks = [
        asyncio.create_task(skelbiu_loop(), name="skelbiu"),
        asyncio.create_task(vinted_loop(), name="vinted"),
        asyncio.create_task(facebook_loop(), name="facebook"),
        asyncio.create_task(process_listings(bot_app_instance), name="processor"),
    ]

    logger.info("All scraper loops started")

    # Wait for shutdown
    await shutdown_event.wait()
    logger.info("Shutdown signal received, cleaning up...")

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    await bot_app_instance.updater.stop()
    await bot_app_instance.stop()
    await bot_app_instance.shutdown()

    logger.info("deal-finder stopped")


def handle_shutdown(sig, frame):
    logger.info(f"Received signal {sig}, initiating shutdown")
    shutdown_event.set()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    asyncio.run(main())
