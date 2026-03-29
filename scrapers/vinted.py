"""
Vinted.lt scraper — polls the Vinted API for new MacBook listings.

Cookie-based auth: GET request to vinted.lt homepage → session cookies.
API endpoint: GET /api/v2/catalog/items with search params.
User details fetched via /api/v2/users/{id} to get review count.
Uses sync httpx.Client (Vinted's cookies don't transfer properly with async client).
Based on patterns from https://github.com/Fuyucch1/Vinted-Notifications
"""

import asyncio
import logging
import random
import time

import httpx

import config

logger = logging.getLogger("deal-finder.scrapers.vinted")

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

BASE_URL = f"https://www.{config.VINTED_DOMAIN}"
API_URL = f"{BASE_URL}/api/v2/catalog/items"
USER_API_URL = f"{BASE_URL}/api/v2/users"

_client: httpx.Client | None = None

# Cache user info to avoid repeated lookups (user_id -> feedback_count)
_user_cache: dict[int, dict] = {}


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=30.0, follow_redirects=True)
        _refresh_cookies()
    return _client


def _refresh_cookies():
    client = _get_client() if _client is not None else _client
    if client is None:
        return

    for attempt in range(3):
        try:
            ua = random.choice(USER_AGENTS)
            resp = client.get(BASE_URL, headers={
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,lt;q=0.8",
            })
            if resp.status_code == 200:
                logger.info("Vinted cookies refreshed")
                return
        except Exception as e:
            logger.warning(f"Cookie refresh attempt {attempt + 1}/3 failed: {e}")

    raise RuntimeError("Failed to refresh Vinted cookies after 3 attempts")


def _api_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{BASE_URL}/catalog?search_text={config.VINTED_SEARCH_TERM}",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }


def _fetch_user_info(client: httpx.Client, user_id: int) -> dict:
    """Fetch user details from Vinted API. Returns cached if available."""
    if user_id in _user_cache:
        return _user_cache[user_id]

    try:
        resp = client.get(f"{USER_API_URL}/{user_id}", headers=_api_headers())
        if resp.status_code == 200:
            user = resp.json().get("user", {})
            info = {
                "feedback_count": user.get("feedback_count", 0),
                "feedback_reputation": user.get("feedback_reputation"),
                "country_title": user.get("country_title"),
            }
            _user_cache[user_id] = info
            return info
    except Exception as e:
        logger.debug(f"Failed to fetch user {user_id}: {e}")

    return {"feedback_count": None, "feedback_reputation": None, "country_title": None}


def _normalize_item(item: dict, user_info: dict) -> dict | None:
    """Convert a Vinted API item to our normalized listing format."""
    try:
        item_id = str(item.get("id", ""))
        if not item_id:
            return None

        # Price
        price_data = item.get("price") or {}
        price_str = price_data.get("amount") if isinstance(price_data, dict) else price_data
        try:
            price = int(float(str(price_str)))
        except (ValueError, TypeError):
            price = None

        # Photo
        photo = item.get("photo") or {}
        image_url = photo.get("url") if isinstance(photo, dict) else None

        # User info from separate API call
        user = item.get("user") or {}
        feedback_count = user_info.get("feedback_count")

        # URL
        url = item.get("url") or f"{BASE_URL}/items/{item_id}"
        if url.startswith("/"):
            url = BASE_URL + url

        return {
            "id": f"vinted_{item_id}",
            "platform": "vinted",
            "title": item.get("title", ""),
            "description": item.get("description", ""),
            "price": price,
            "condition": item.get("status"),
            "seller_reviews": feedback_count,
            "seller_joined": None,
            "seller_location": user_info.get("country_title"),
            "distance_km": None,
            "url": url,
            "image_url": image_url,
            "listed_at": None,
            "platform_raw": item,
        }
    except Exception as e:
        logger.warning(f"Failed to normalize Vinted item: {e}")
        return None


def _poll_sync() -> list[dict]:
    """Sync poll — called via asyncio.to_thread."""
    client = _get_client()

    params = {
        "search_text": config.VINTED_SEARCH_TERM,
        "order": "newest_first",
        "per_page": 20,
    }

    resp = client.get(API_URL, params=params, headers=_api_headers())

    # Auth failure — refresh and retry once
    if resp.status_code in (401, 403):
        logger.warning(f"Vinted returned {resp.status_code}, refreshing cookies")
        _refresh_cookies()
        resp = client.get(API_URL, params=params, headers=_api_headers())

    if resp.status_code == 429:
        raise RuntimeError("Vinted rate limited (429)")

    resp.raise_for_status()

    data = resp.json()
    items = data.get("items", [])
    logger.debug(f"Vinted returned {len(items)} items")

    # Collect unique user IDs to fetch
    user_ids = set()
    for item in items:
        user = item.get("user") or {}
        uid = user.get("id")
        if uid:
            user_ids.add(uid)

    # Fetch user details (with small delay between to avoid rate limits)
    user_infos = {}
    for uid in user_ids:
        if uid not in _user_cache:
            time.sleep(0.2)  # gentle rate limiting
        user_infos[uid] = _fetch_user_info(client, uid)

    # Normalize and filter
    listings = []
    skipped_zero = 0
    for item in items:
        user = item.get("user") or {}
        uid = user.get("id")
        user_info = user_infos.get(uid, {})

        # Skip zero-review sellers — almost always scams
        feedback = user_info.get("feedback_count")
        if feedback is not None and feedback == 0:
            skipped_zero += 1
            continue

        normalized = _normalize_item(item, user_info)
        if normalized is not None:
            listings.append(normalized)

    if skipped_zero:
        logger.info(f"Filtered out {skipped_zero} listings from zero-review sellers")

    return listings


async def poll() -> list[dict]:
    """Poll Vinted.lt API for new MacBook listings (async wrapper)."""
    return await asyncio.to_thread(_poll_sync)
