#!/usr/bin/env python3
"""Test the full pipeline: scraper → pre-filter → AI filter → verdict."""
import asyncio
import json
import logging
import sys
import time

sys.path.insert(0, ".")
import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("test-pipeline")


# ── Pre-filter (from orchestrator.py) ──────────────────────────────
import re

MACBOOK_KEYWORDS = re.compile(r"macbook|mac\s*book", re.IGNORECASE)
ACCESSORY_KEYWORDS = re.compile(
    r"\b(case|cover|sleeve|charger|adapter|cable|skin|sticker|stand|dock|hub|keyboard\s*cover|screen\s*protector|bag|backpack)\b",
    re.IGNORECASE,
)
MODEL_HINTS = re.compile(r"\b(air|pro|m[1-5]|intel|i[357]|retina)\b", re.IGNORECASE)


def pre_filter(listing: dict) -> bool:
    if listing.get("price") is None:
        return False
    title = listing.get("title", "")
    if not MACBOOK_KEYWORDS.search(title):
        return False
    if ACCESSORY_KEYWORDS.search(title) and not MODEL_HINTS.search(title):
        return False
    return True


# ── Test Vinted ────────────────────────────────────────────────────
async def test_vinted():
    logger.info("=" * 60)
    logger.info("TESTING VINTED SCRAPER")
    logger.info("=" * 60)
    try:
        from scrapers.vinted import poll as vinted_poll
        listings = await vinted_poll()
        logger.info(f"Vinted returned {len(listings)} listings")

        passed = [l for l in listings if pre_filter(l)]
        logger.info(f"Vinted: {len(passed)} passed pre-filter out of {len(listings)}")

        for l in passed[:3]:
            logger.info(f"  [{l['id']}] €{l['price']} - {l['title'][:60]}")
            if l.get("seller_reviews") is not None:
                logger.info(f"    Seller: {l['seller_reviews']} reviews, location: {l.get('seller_location')}")

        return passed
    except Exception as e:
        logger.error(f"Vinted scraper failed: {e}", exc_info=True)
        return []


# ── Test Skelbiu ───────────────────────────────────────────────────
async def test_skelbiu():
    logger.info("=" * 60)
    logger.info("TESTING SKELBIU SCRAPER")
    logger.info("=" * 60)
    try:
        from scrapers.skelbiu import poll as skelbiu_poll
        listings = await skelbiu_poll()
        logger.info(f"Skelbiu returned {len(listings)} listings")

        passed = [l for l in listings if pre_filter(l)]
        logger.info(f"Skelbiu: {len(passed)} passed pre-filter out of {len(listings)}")

        for l in passed[:3]:
            logger.info(f"  [{l['id']}] €{l['price']} - {l['title'][:60]}")
            logger.info(f"    Location: {l.get('seller_location')}, URL: {l.get('url', '')[:80]}")

        return passed
    except Exception as e:
        logger.error(f"Skelbiu scraper failed: {e}", exc_info=True)
        return []


# ── Test Facebook ──────────────────────────────────────────────────
async def test_facebook():
    logger.info("=" * 60)
    logger.info("TESTING FACEBOOK SCRAPER")
    logger.info("=" * 60)
    try:
        from scrapers.facebook import poll_city
        listings = poll_city("kaunas")
        logger.info(f"Facebook (Kaunas) returned {len(listings)} listings")

        passed = [l for l in listings if pre_filter(l)]
        logger.info(f"Facebook: {len(passed)} passed pre-filter out of {len(listings)}")

        for l in passed[:3]:
            logger.info(f"  [{l['id']}] €{l['price']} - {l['title'][:60]}")

        return passed
    except Exception as e:
        logger.error(f"Facebook scraper failed: {e}", exc_info=True)
        return []


# ── Test AI Filter ─────────────────────────────────────────────────
async def test_ai_filter(listing: dict):
    logger.info("-" * 60)
    logger.info(f"AI FILTER: {listing['id']}")
    logger.info(f"  Platform: {listing['platform']}")
    logger.info(f"  Title: {listing['title']}")
    logger.info(f"  Price: €{listing['price']}")
    logger.info(f"  Description: {(listing.get('description') or '')[:150]}...")
    logger.info("-" * 60)

    from filter import analyze_listing

    start = time.time()
    result = await analyze_listing(listing)
    elapsed = time.time() - start

    if result is None:
        logger.error(f"  AI returned None (API error)")
        return None

    logger.info(f"  AI response in {elapsed:.1f}s")
    logger.info(f"  Verdict: {result['verdict']}")
    logger.info(f"  Model: {result.get('model_name')} ({result.get('model_id')})")
    logger.info(f"  Confidence: {result.get('model_confidence')}")
    logger.info(f"  Specs: {result.get('ram')}/{result.get('storage')}")
    logger.info(f"  Broken: {result.get('is_broken')}, Repairs: {result.get('repairs_needed')}")
    logger.info(f"  Sell price: €{result.get('sell_price')} ({result.get('price_source')})")
    logger.info(f"  Net profit: €{result.get('net_profit')}")
    logger.info(f"  ROI: {result.get('roi_percent')}%")
    logger.info(f"  Hourly rate: €{result.get('effective_hourly_rate')}")
    logger.info(f"  Scam risk: {result.get('scam_risk')} - flags: {result.get('scam_flags')}")
    logger.info(f"  Reason: {result.get('verdict_reason')}")

    if result["verdict"] in ("SEND", "SEND_FLAGGED"):
        logger.info(f"  Cost breakdown: {json.dumps(result.get('cost_breakdown'), indent=4)}")

        # Test bot formatting
        from bot import format_deal_message
        msg = format_deal_message(result, listing)
        logger.info(f"\n{'=' * 40} TELEGRAM MESSAGE {'=' * 40}\n{msg}\n{'=' * 97}")

    return result


# ── Main ───────────────────────────────────────────────────────────
async def main():
    all_listings = []

    # Test each scraper
    vinted_listings = await test_vinted()
    all_listings.extend(vinted_listings)

    skelbiu_listings = await test_skelbiu()
    all_listings.extend(skelbiu_listings)

    # Facebook often needs auth, try but don't fail
    # fb_listings = await test_facebook()
    # all_listings.extend(fb_listings)

    logger.info("=" * 60)
    logger.info(f"TOTAL: {len(all_listings)} listings passed pre-filter")
    logger.info("=" * 60)

    if not all_listings:
        logger.warning("No listings found! Scrapers may need fixing.")
        return

    # Run AI filter on up to 3 listings
    test_count = min(3, len(all_listings))
    logger.info(f"\nRunning AI filter on {test_count} listings...\n")

    results = {"SEND": 0, "SEND_FLAGGED": 0, "SKIP": 0, "ERROR": 0}
    for listing in all_listings[:test_count]:
        result = await test_ai_filter(listing)
        if result is None:
            results["ERROR"] += 1
        else:
            results[result["verdict"]] += 1
        print()

    logger.info("=" * 60)
    logger.info(f"RESULTS: {results}")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
