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

# City search config: slug + radius in km
# Note: Facebook Marketplace in Lithuania only recognizes "vilnius" as a valid slug.
# Other cities (kaunas, klaipeda, etc.) redirect to a generic page with 0 results.
# We use "vilnius" with a large radius to cover all of Lithuania.
CITY_CONFIG = {
    "kaunas":  {"slug": "vilnius", "radius": 120},
    "vilnius": {"slug": "vilnius", "radius": 60},
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


def _parse_card(link, city: str) -> dict | None:
    """Parse a single listing card element into a normalized dict."""
    try:
        href = link.get_attribute("href") or ""
        match = re.search(r"/marketplace/item/(\d+)", href)
        if not match:
            return None

        item_id = match.group(1)
        url = f"https://www.facebook.com/marketplace/item/{item_id}/"

        # Card text layout: "price\ntitle\nlocation"
        card_text = link.inner_text()
        lines = [l.strip() for l in card_text.split("\n") if l.strip()]

        price = None
        title = ""
        location = None

        for line in lines:
            # Skip all price lines — some cards have two (original + discounted)
            price_match = re.search(r"[€$]\s*([\d,.\s]+)", line)
            if not price_match:
                price_match = re.search(r"([\d,.\s]+)\s*[€$]", line)
            if price_match:
                # Keep the first (lowest/current) price
                if price is None:
                    price_str = price_match.group(1).replace(",", "").replace(" ", "").replace(".", "")
                    try:
                        price = int(price_str)
                    except ValueError:
                        pass
                continue

            # After price lines, next meaningful line is title, then location
            if not title and len(line) > 3 and len(line) < 200:
                title = line
                continue

            # Location line — typically "City, XX" format
            if title and location is None and len(line) < 60:
                location = line
                continue

        # Image
        img = link.query_selector("img")
        image_url = img.get_attribute("src") if img else None

        return {
            "id": f"facebook_{item_id}",
            "platform": "facebook",
            "title": title,
            "description": "",  # Fetched on demand for promising listings
            "price": price,
            "condition": None,
            "seller_reviews": None,
            "seller_joined": None,
            "seller_location": location or city.capitalize(),
            "distance_km": None,
            "url": url,
            "image_url": image_url,
            "listed_at": datetime.now(timezone.utc).isoformat(),
            "platform_raw": {"card_text": card_text},
        }
    except Exception as e:
        logger.debug(f"Failed to parse listing card: {e}")
        return None


def _extract_listings(page, city: str) -> list[dict]:
    """Extract listing data from the current marketplace page."""
    links = page.query_selector_all('a[href*="/marketplace/item/"]')
    logger.debug(f"Found {len(links)} listing links for {city}")

    listings = []
    seen_ids = set()
    for link in links:
        parsed = _parse_card(link, city)
        if parsed is None:
            continue
        if parsed["id"] in seen_ids:
            continue
        seen_ids.add(parsed["id"])
        listings.append(parsed)

    return listings


def fetch_listing_details(listing: dict) -> dict:
    """Fetch description and seller info from individual listing page.

    Called on demand for promising listings (after AI filter).
    Mutates and returns the listing dict with added details.

    FB listing page structure (line-by-line from inner_text):
      Title
      Price
      "Listed X ago in City, XX"
      ...
      "Details" or "Condition"
      condition value (e.g. "Used - Fair")
      description text (may end with "... See more")
      "City, XX"
      ...
      "Seller details"
      Seller name
      "Joined Facebook in YYYY"
    """
    _ensure_browser()

    page = _context.new_page()
    try:
        page.goto(listing["url"], wait_until="domcontentloaded", timeout=30000)
        time.sleep(4)

        # Check for auth failure
        if "/login" in page.url or "checkpoint" in page.url:
            logger.warning("Facebook auth expired during detail fetch")
            return listing

        page_text = page.inner_text("body")
        lines = page_text.split("\n")

        # Extract description: text between "Condition" value and location/sponsored lines
        description = ""
        condition = None
        in_details = False
        skip_labels = {
            "details", "condition", "seller details", "seller information",
            "message seller", "message", "sponsored", "location is approximate",
            "today's picks", "see all listings",
        }
        for i, line in enumerate(lines):
            stripped = line.strip()
            low = stripped.lower()

            if low in ("details", "condition"):
                in_details = True
                continue

            if in_details:
                # Condition value (e.g. "Used - Fair", "Used - Good")
                if condition is None and ("used" in low or "new" in low or "naudot" in low or "nauj" in low):
                    condition = stripped
                    listing["condition"] = condition
                    continue

                # Description — the next substantial text line
                if not description and len(stripped) > 15 and low not in skip_labels:
                    # Strip "... See more" / "... Rodyti daugiau" suffix
                    description = re.sub(r"\.\.\.\s*(?:See more|Rodyti daugiau)\s*$", "...", stripped)
                    listing["description"] = description
                    continue

                # Stop at location line or seller section
                if stripped and (
                    low in skip_labels
                    or re.match(r"^[A-ZĀ-Ž][\w\s-]+,\s*[A-Z]{2}$", stripped)  # "City, XX"
                    or low.startswith("seller")
                ):
                    break

        # Seller joined date — "Joined Facebook in YYYY" or "Prisijungė YYYY m."
        joined_match = re.search(
            r"Joined Facebook in (\d{4})|Prisijung.\s+(\d{4})\s*m\.",
            page_text,
        )
        if joined_match:
            listing["seller_joined"] = joined_match.group(1) or joined_match.group(2)

        # Listed time and location — "Listed X ago in City, XX"
        listed_match = re.search(
            r"Listed .+? in (.+?)$",
            page_text,
            re.MULTILINE,
        )
        if listed_match:
            listing["seller_location"] = listed_match.group(1).strip()

        logger.debug(
            f"Fetched details for {listing['id']}: "
            f"desc={len(description)} chars, "
            f"condition={condition}, "
            f"joined={listing.get('seller_joined')}"
        )

    except Exception as e:
        logger.warning(f"Failed to fetch details for {listing['id']}: {e}")
    finally:
        page.close()

    return listing


def poll_city(city: str) -> list[dict]:
    """Poll Facebook Marketplace for a single city. Called from thread."""
    _ensure_browser()

    city_cfg = CITY_CONFIG.get(city, {"slug": city, "radius": 50})
    query = quote_plus("macbook")
    url = (
        f"https://www.facebook.com/marketplace/{city_cfg['slug']}/search/"
        f"?query={query}&radius={city_cfg['radius']}&exact=false"
    )

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
