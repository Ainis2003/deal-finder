import asyncio
import logging
import random
import signal
from datetime import datetime, timezone

import bot
import config
import db
import filter as ai_filter
from log_setup import setup_logging
from scrapers import skelbiu

logger = logging.getLogger("deal-finder.orchestrator")

listing_queue: asyncio.Queue = asyncio.Queue()
shutdown_event = asyncio.Event()


# ── Scraper loops ───────────────────────────────────────────────────


async def skelbiu_loop():
    consecutive_failures = 0
    while not shutdown_event.is_set():
        try:
            listings = await skelbiu.poll()
            consecutive_failures = 0
            for listing in listings:
                await listing_queue.put(listing)
        except NotImplementedError:
            logger.info("Skelbiu adapter not ready, skipping")
            # Don't retry rapidly for NotImplementedError
            await _sleep_or_shutdown(60)
            continue
        except Exception as e:
            consecutive_failures += 1
            logger.warning(f"Skelbiu failed ({consecutive_failures}x): {e}")
            if consecutive_failures >= config.SCRAPER_FAILURE_ALERT_THRESHOLD:
                await _send_health_alert("skelbiu", consecutive_failures)
        await _sleep_or_shutdown(config.SKELBIU_POLL_INTERVAL_S)


async def vinted_loop():
    # Import here so orchestrator can start even if vinted module has issues
    from scrapers import vinted

    consecutive_failures = 0
    while not shutdown_event.is_set():
        try:
            listings = await vinted.poll()
            consecutive_failures = 0
            bot.scraper_status["vinted"] = datetime.now(timezone.utc)
            for listing in listings:
                await listing_queue.put(listing)
        except Exception as e:
            consecutive_failures += 1
            logger.warning(f"Vinted failed ({consecutive_failures}x): {e}")
            if consecutive_failures >= config.SCRAPER_FAILURE_ALERT_THRESHOLD:
                await _send_health_alert("vinted", consecutive_failures)

        jitter = random.randint(-10, 10)
        await _sleep_or_shutdown(config.VINTED_POLL_INTERVAL_S + jitter)


async def facebook_loop():
    from scrapers import facebook

    consecutive_failures = 0
    while not shutdown_event.is_set():
        for city in config.FB_CITIES:
            if shutdown_event.is_set():
                break
            try:
                listings = await asyncio.to_thread(facebook.poll_city, city)
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
            # Skip if no price
            if listing.get("price") is None:
                logger.debug(f"Skipping {listing['id']}: no price")
                continue

            # Dedup
            if await db.is_seen(listing["id"]):
                logger.debug(f"Skipping {listing['id']}: already seen")
                continue

            # Mark seen immediately
            await db.mark_seen(listing["id"], listing["platform"])
            bot.listings_seen_today += 1

            # AI filter
            verdict = await ai_filter.analyze_listing(listing)
            if verdict is None:
                logger.warning(f"AI filter returned None for {listing['id']}, skipping")
                continue

            if not verdict.get("is_macbook"):
                logger.debug(f"Skipping {listing['id']}: not a MacBook")
                continue

            # Send alerts
            if verdict["verdict"] in ("SEND", "SEND_FLAGGED"):
                await bot.send_deal(bot_app, verdict, listing)
            else:
                logger.debug(f"SKIP: {listing['id']} — {verdict.get('verdict_reason', '')[:80]}")

        except Exception as e:
            logger.error(f"Error processing {listing.get('id', '?')}: {e}", exc_info=True)


# ── Helpers ─────────────────────────────────────────────────────────


async def _sleep_or_shutdown(seconds: float):
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=max(0, seconds))
    except asyncio.TimeoutError:
        pass  # Normal — shutdown didn't happen, continue


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

    # Cancel tasks
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    # Stop bot
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
