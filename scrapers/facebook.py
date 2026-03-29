"""
Facebook Marketplace scraper — Playwright-based.

Loads saved auth session, scrapes listings for each Lithuanian city.
Runs in a dedicated thread (called via asyncio.to_thread from orchestrator).
Based on patterns from https://github.com/Scratchycarl/OpenClaw_Facebook_Marketplace_Scraper
"""

import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright, Browser, BrowserContext

import config

logger = logging.getLogger("deal-finder.scrapers.facebook")

# City marketplace URL slugs — may need numeric IDs if slugs don't work.
# To find IDs: visit marketplace in browser, check URL for numeric city ID.
CITY_URLS = {
    "kaunas": "kaunas",
    "vilnius": "vilnius",
    "klaipeda": "klaipeda",
}

DISTANCE_FROM_KAUNAS = {
    "kaunas": 0,
    "vilnius": 100,
    "klaipeda": 220,
}

_browser: Browser | None = None
_context: BrowserContext | None = None


def _ensure_browser():
    """Initialize Playwright browser and load auth session."""
    global _browser, _context

    if _browser is not None and _context is not None:
        return

    pw = sync_playwright().start()
    _browser = pw.chromium.launch(headless=True)
    _context = _browser.new_context(
        storage_state=config.FB_AUTH_STATE_PATH,
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    )
    logger.info("Facebook browser initialized with saved session")


def _close_browser():
    """Clean up browser resources."""
    global _browser, _context
    if _context:
        _context.close()
        _context = None
    if _browser:
        _browser.close()
        _browser = None


def _extract_listings(page, city: str) -> list[dict]:
    """Extract listing data from the current marketplace page."""
    listings = []

    # Find all listing links
    links = page.query_selector_all('a[href*="/marketplace/item/"]')
    logger.debug(f"Found {len(links)} listing links for {city}")

    seen_ids = set()
    for link in links:
        try:
            href = link.get_attribute("href") or ""

            # Extract item ID from URL
            match = re.search(r"/marketplace/item/(\d+)", href)
            if not match:
                continue
            item_id = match.group(1)
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)

            # Build full URL
            url = f"https://www.facebook.com/marketplace/item/{item_id}/"

            # Extract text content from the listing card
            card_text = link.inner_text()
            lines = [l.strip() for l in card_text.split("\n") if l.strip()]

            # Extract price — look for € amount
            price = None
            for line in lines:
                price_match = re.search(r"[€]\s*([\d,.\s]+)", line)
                if price_match:
                    price_str = price_match.group(1).replace(",", "").replace(" ", "").replace(".", "")
                    try:
                        price = int(price_str)
                    except ValueError:
                        pass
                    break
                # Also try plain number with € symbol
                price_match = re.search(r"([\d,.\s]+)\s*[€]", line)
                if price_match:
                    price_str = price_match.group(1).replace(",", "").replace(" ", "").replace(".", "")
                    try:
                        price = int(price_str)
                    except ValueError:
                        pass
                    break

            # Title — typically the first non-price line
            title = ""
            for line in lines:
                if "€" not in line and len(line) > 3 and len(line) < 200:
                    title = line
                    break

            # Location — look for a shorter line that might be a city name
            location = None
            for line in lines:
                if "€" not in line and line != title and len(line) < 60:
                    location = line
                    break

            # Image
            img = link.query_selector("img")
            image_url = img.get_attribute("src") if img else None

            # Distance from Kaunas
            distance_km = DISTANCE_FROM_KAUNAS.get(city)

            listing = {
                "id": f"facebook_{item_id}",
                "platform": "facebook",
                "title": title,
                "description": "",  # Description requires visiting individual page
                "price": price,
                "condition": None,
                "seller_reviews": None,
                "seller_joined": None,
                "seller_location": city.capitalize(),
                "distance_km": distance_km,
                "url": url,
                "image_url": image_url,
                "listed_at": datetime.now(timezone.utc).isoformat(),
                "platform_raw": {"card_text": card_text},
            }
            listings.append(listing)

        except Exception as e:
            logger.debug(f"Failed to extract listing from card: {e}")
            continue

    return listings


def poll_city(city: str) -> list[dict]:
    """Poll Facebook Marketplace for a single city. Called from thread."""
    _ensure_browser()

    city_slug = CITY_URLS.get(city, city)
    query = quote_plus("macbook")
    url = f"https://www.facebook.com/marketplace/{city_slug}/search/?query={query}"

    page = _context.new_page()
    try:
        logger.debug(f"Navigating to FB Marketplace: {city}")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Wait for content to load
        time.sleep(4)

        # Check for auth failure — redirected to login
        current_url = page.url
        if "/login" in current_url or "checkpoint" in current_url:
            raise RuntimeError(f"Facebook auth expired — redirected to {current_url}")

        # Scroll down to load more listings
        page.evaluate("window.scrollBy(0, 1000)")
        time.sleep(2)
        page.evaluate("window.scrollBy(0, 1000)")
        time.sleep(2)

        listings = _extract_listings(page, city)

        if not listings:
            logger.warning(f"No listings found for {city} — possible auth issue or empty results")

        logger.info(f"Facebook/{city}: found {len(listings)} listings")
        return listings

    finally:
        page.close()
