"""
Vinted.lt scraper — polls the Vinted API for new MacBook listings.

Uses curl_cffi with Chrome TLS impersonation through a residential proxy
to bypass Cloudflare. Cookies are acquired by visiting the homepage first.
On 401/403: session is refreshed with new cookies.
"""

import asyncio
import html as html_module
import logging
import re
import time

from curl_cffi.requests import Session

import config

logger = logging.getLogger("deal-finder.scrapers.vinted")

BASE_URL = f"https://www.{config.VINTED_DOMAIN}"
API_URL = f"{BASE_URL}/api/v2/catalog/items"
USER_API_URL = f"{BASE_URL}/api/v2/users"

# curl_cffi session (with proxy + browser TLS fingerprint)
_session: Session | None = None
_consecutive_fails: int = 0
_proxy_index: int = 0

# Caches
_user_cache: dict[int, dict] = {}
_description_cache: dict[str, str] = {}

_proxy_list: list[str] = getattr(config, "PROXY_LIST", [])


def _current_proxy() -> dict | None:
    if not _proxy_list:
        return None
    url = _proxy_list[_proxy_index % len(_proxy_list)]
    return {"http": url, "https": url}


def _rotate_proxy():
    """Switch to the next proxy in the list."""
    global _proxy_index
    _proxy_index = (_proxy_index + 1) % len(_proxy_list)
    ip = _proxy_list[_proxy_index].split("@")[1].split(":")[0]
    logger.info(f"Rotated to proxy #{_proxy_index + 1}/{len(_proxy_list)} ({ip})")


def _create_session() -> Session:
    """Create a new curl_cffi session with current proxy and Chrome TLS impersonation."""
    s = Session(
        impersonate="chrome",
        timeout=30,
        allow_redirects=True,
        proxies=_current_proxy(),
        verify=False,
    )
    s.headers.update({
        "Accept-Language": "lt-LT,lt;q=0.9,en;q=0.8",
        "DNT": "1",
        "Connection": "keep-alive",
    })
    return s


def _acquire_cookies(session: Session):
    """Visit Vinted homepage to acquire session cookies (access_token_web)."""
    logger.info("Acquiring Vinted cookies via homepage visit...")
    resp = session.get(BASE_URL, headers={
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    })

    if resp.status_code != 200:
        logger.warning(f"Homepage returned {resp.status_code}")
        return

    cookies = dict(session.cookies)
    if "access_token_web" in cookies:
        logger.info(f"Got access_token_web + {len(cookies)} cookies")
    else:
        logger.warning(f"No access_token_web in cookies, got: {list(cookies.keys())}")


def _get_session() -> Session:
    """Get or create a session with valid cookies."""
    global _session
    if _session is None:
        _session = _create_session()
        _acquire_cookies(_session)
    return _session


def _refresh_session():
    """Drop current session and create a fresh one with new cookies."""
    global _session
    if _session:
        try:
            _session.close()
        except Exception:
            pass
    _session = None
    return _get_session()


def _api_headers() -> dict:
    return {
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{BASE_URL}/catalog?search_text={config.VINTED_SEARCH_TERM}",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Origin": BASE_URL,
    }


def _fetch_user_info(session: Session, user_id: int) -> dict:
    """Fetch user details from Vinted API. Returns cached if available."""
    if user_id in _user_cache:
        return _user_cache[user_id]

    try:
        resp = session.get(f"{USER_API_URL}/{user_id}", headers=_api_headers())
        if resp.status_code == 200:
            user = resp.json().get("user", {})
            info = {
                "feedback_count": user.get("feedback_count", 0),
                "feedback_reputation": user.get("feedback_reputation"),
                "positive_feedback_count": user.get("positive_feedback_count"),
                "neutral_feedback_count": user.get("neutral_feedback_count"),
                "negative_feedback_count": user.get("negative_feedback_count"),
                "country_title": user.get("country_title"),
                "city": user.get("city"),
                "created_at": user.get("created_at") or user.get("registered_at"),
            }
            _user_cache[user_id] = info
            return info
    except Exception as e:
        logger.debug(f"Failed to fetch user {user_id}: {e}")

    return {
        "feedback_count": None, "feedback_reputation": None,
        "positive_feedback_count": None, "neutral_feedback_count": None,
        "negative_feedback_count": None, "country_title": None,
        "city": None, "created_at": None,
    }


def _fetch_item_description(session: Session, item_id: str, url: str) -> str:
    """Fetch item description from the item's HTML page via og:description meta tag."""
    if item_id in _description_cache:
        return _description_cache[item_id]

    try:
        resp = session.get(url, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
        })
        if resp.status_code != 200:
            return ""

        match = re.search(
            r'<meta\s+property="og:description"\s+content="(.+?)"',
            resp.text,
            re.DOTALL,
        )
        if match:
            desc = html_module.unescape(match.group(1))
            if " - " in desc:
                desc = desc.split(" - ", 1)[1]
            _description_cache[item_id] = desc
            return desc

        match = re.search(
            r'<meta\s+name="description"\s+content="(.+?)"',
            resp.text,
            re.DOTALL,
        )
        if match:
            desc = html_module.unescape(match.group(1))
            if " - " in desc:
                desc = desc.split(" - ", 1)[1]
            _description_cache[item_id] = desc
            return desc

    except Exception as e:
        logger.debug(f"Failed to fetch description for item {item_id}: {e}")

    return ""


def _normalize_item(item: dict, user_info: dict) -> dict | None:
    """Convert a Vinted API item to our normalized listing format."""
    try:
        item_id = str(item.get("id", ""))
        if not item_id:
            return None

        price_data = item.get("price") or {}
        price_str = price_data.get("amount") if isinstance(price_data, dict) else price_data
        try:
            price = int(float(str(price_str)))
        except (ValueError, TypeError):
            price = None

        photo = item.get("photo") or {}
        image_url = photo.get("url") if isinstance(photo, dict) else None

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
            "seller_reviews": user_info.get("feedback_count"),
            "seller_positive_reviews": user_info.get("positive_feedback_count"),
            "seller_negative_reviews": user_info.get("negative_feedback_count"),
            "seller_joined": None,
            "seller_location": user_info.get("country_title"),
            "seller_city": user_info.get("city"),
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
    """Sync poll — called via dedicated thread executor."""
    global _consecutive_fails

    # Backoff with shorter waits since we have proxy rotation
    if _consecutive_fails > 0:
        backoffs = [30, 60, 120, 300, 600]
        backoff = backoffs[min(_consecutive_fails - 1, len(backoffs) - 1)]
        logger.info(f"Vinted backoff: {backoff}s after {_consecutive_fails} consecutive blocks (proxy #{_proxy_index + 1})")
        time.sleep(backoff)
        _refresh_session()

    session = _get_session()

    params = {
        "search_text": config.VINTED_SEARCH_TERM,
        "order": "newest_first",
        "per_page": 20,
        "time": str(int(time.time())),
    }

    resp = session.get(API_URL, params=params, headers=_api_headers())

    # Auth failure — rotate proxy and retry
    if resp.status_code in (401, 403):
        logger.warning(f"Vinted {resp.status_code}, rotating proxy and refreshing session")
        _rotate_proxy()
        _refresh_session()
        session = _get_session()
        time.sleep(2)
        resp = session.get(API_URL, params=params, headers=_api_headers())

    if resp.status_code in (401, 403, 429):
        _consecutive_fails += 1
        _rotate_proxy()  # pre-rotate for next attempt
        raise RuntimeError(f"Vinted blocked ({resp.status_code}) — backoff #{_consecutive_fails}")

    if resp.status_code != 200:
        raise RuntimeError(f"Vinted unexpected status {resp.status_code}")

    # Success
    _consecutive_fails = 0

    data = resp.json()
    items = data.get("items", [])
    logger.debug(f"Vinted returned {len(items)} items")

    # Collect unique user IDs
    user_ids = set()
    for item in items:
        user = item.get("user") or {}
        uid = user.get("id")
        if uid:
            user_ids.add(uid)

    # Fetch user details (with rate limiting)
    user_infos = {}
    for uid in user_ids:
        if uid not in _user_cache:
            time.sleep(0.3)
        user_infos[uid] = _fetch_user_info(session, uid)

    # Normalize and filter
    listings = []
    skipped_zero = 0
    for item in items:
        user = item.get("user") or {}
        uid = user.get("id")
        user_info = user_infos.get(uid, {})

        feedback = user_info.get("feedback_count")
        if feedback is not None and feedback == 0:
            skipped_zero += 1
            continue

        normalized = _normalize_item(item, user_info)
        if normalized is not None:
            # Fetch full description from item page
            item_id = str(item.get("id", ""))
            if item_id not in _description_cache:
                time.sleep(0.5)
                desc = _fetch_item_description(session, item_id, normalized["url"])
                if desc:
                    normalized["description"] = desc
            else:
                normalized["description"] = _description_cache[item_id]
            listings.append(normalized)

    if skipped_zero:
        logger.info(f"Filtered out {skipped_zero} listings from zero-review sellers")

    return listings


async def poll() -> list[dict]:
    """Poll Vinted.lt API for new MacBook listings (async wrapper)."""
    return await asyncio.to_thread(_poll_sync)
