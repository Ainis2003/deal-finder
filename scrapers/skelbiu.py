"""
Skelbiu.lt scraper adapter.

Wraps the owner's existing Skelbiu scraper (skelbiu_original.py),
reusing its Selenium driver, API fetching, keyword filtering, and
detail extraction. Converts output to the normalized listing format
and suppresses the original script's Telegram sends.
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

from scrapers.skelbiu_original import (
    SKELBIU_BASE,
    create_driver,
    fetch_new_items,
    is_computer_ad,
    extract_listing_details,
)

logger = logging.getLogger("deal-finder.scrapers.skelbiu")

_driver = None


def _get_driver():
    global _driver
    if _driver is None:
        logger.info("Initializing Skelbiu Selenium driver")
        _driver = create_driver()
    return _driver


def _parse_price(price_str: str | None) -> int | None:
    """Parse price string like '380 €' or '1 200 €' to int euros."""
    if not price_str:
        return None
    # Remove currency symbols and whitespace, keep digits
    digits = re.sub(r"[^\d]", "", price_str)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _clean_city(raw_city: str | None) -> str:
    """Extract just the city name, stripping promo text like 'Siųsti siuntą...'."""
    if not raw_city:
        return ""
    # City is the first word(s) before any promo/extra text
    # Common pattern: "Kaunas Siųsti siuntą vos nuo 1,94 €"
    match = re.match(r"^([\w\-ėųūąįšžčĖŲŪĄĮŠŽČ]+(?:\s[\w\-ėųūąįšžčĖŲŪĄĮŠŽČ]+)?)", raw_city)
    if match:
        candidate = match.group(1)
        # If next word looks like promo text, take only first word
        if "siųsti" in raw_city.lower() or "siuntą" in raw_city.lower():
            return raw_city.split()[0]
        return candidate
    return raw_city.strip()


def _normalize_listing(details: dict) -> dict:
    """Convert skelbiu_original detail dict to normalized listing format."""
    item_id = str(details.get("id", ""))
    price = _parse_price(details.get("price"))
    city = _clean_city(details.get("city"))

    return {
        "id": f"skelbiu_{item_id}",
        "platform": "skelbiu",
        "title": details.get("title") or "",
        "description": details.get("description") or "",
        "price": price,
        "condition": None,
        "seller_reviews": None,
        "seller_joined": None,
        "seller_location": city,
        "distance_km": None,
        "url": details.get("url") or f"{SKELBIU_BASE}/skelbimai/{item_id}.html",
        "image_url": None,
        "listed_at": datetime.now(timezone.utc).isoformat(),
        "platform_raw": details,
    }


def _poll_sync() -> list[dict]:
    """Synchronous poll — runs in a thread."""
    driver = _get_driver()
    data = fetch_new_items(driver)

    if not data or "newItems" not in data:
        logger.debug("Skelbiu: no data or no newItems key")
        return []

    new_items = data.get("newItems", [])
    logger.debug(f"Skelbiu API returned {len(new_items)} items")

    listings = []
    for item in new_items:
        item_id = str(item.get("id", ""))
        title = item.get("title", "")
        item_url = item.get("itemUrl", "")

        if not item_id:
            continue

        # Only process MacBook/computer ads
        if not is_computer_ad(title):
            continue

        # Fetch full details from the listing page
        details = extract_listing_details(driver, item_id, item_url)
        if not details:
            logger.warning(f"Skelbiu: could not extract details for {item_id}")
            continue

        normalized = _normalize_listing(details)
        listings.append(normalized)

    logger.debug(f"Skelbiu: returning {len(listings)} normalized listings")
    return listings


async def poll() -> list[dict]:
    """Poll Skelbiu.lt for new MacBook listings (async wrapper)."""
    return await asyncio.to_thread(_poll_sync)
