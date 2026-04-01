import json
import time
import re
import os
import asyncio
from pathlib import Path
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException

# -------------------- Configuration --------------------
TESTING_MODE = False  # Set to True for testing mode, False for production mode

SKELBIU_BASE = "https://www.skelbiu.lt"
CHECKED_IDS_FILE = Path(__file__).with_name("skelbiu_checked_ids.json")
TEST_IDS_FILE = Path(__file__).with_name("test_ids.txt")

# Telegram setup — only initialized when running standalone
bot = None
CHAT_IDs = []
CHECK_INTERVAL = 15  # seconds between checks

COMPUTER_KEYWORDS = [
    r'\bmac\b',
    r'\bmacbook\b',
    r'\bmac\s*book\b',
    r'\bimac\b',
    r'\bmac\s*air\b',
    r'\bmac\s*pro\b',
    r'\bmacbook\s*air\b',
    r'\bmacbook\s*pro\b',
]


# -------------------- Persistent ID tracking --------------------
def load_checked_ids():
    try:
        if CHECKED_IDS_FILE.is_file():
            with CHECKED_IDS_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return set(str(x) for x in data)
            if isinstance(data, dict) and isinstance(data.get("ids"), list):
                return set(str(x) for x in data["ids"])
    except Exception as e:
        print(f"Warning: Failed to read {CHECKED_IDS_FILE.name}: {e}")
    return set()


def save_checked_ids(ids):
    try:
        tmp_path = CHECKED_IDS_FILE.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(sorted(ids), f, ensure_ascii=False, indent=2)
        tmp_path.replace(CHECKED_IDS_FILE)
    except Exception as e:
        print(f"Error: Failed to save checked IDs: {e}")


def add_checked_id(rid, ids_cache):
    if rid and rid not in ids_cache:
        ids_cache.add(rid)
        save_checked_ids(ids_cache)


def load_test_ids():
    try:
        if TEST_IDS_FILE.is_file():
            with TEST_IDS_FILE.open("r", encoding="utf-8") as f:
                ids = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
            print(f"Loaded {len(ids)} test IDs from {TEST_IDS_FILE.name}")
            return ids
        else:
            print(f"Test file {TEST_IDS_FILE.name} not found. Creating empty file.")
            TEST_IDS_FILE.write_text("# Add one ID per line\n# Example:\n# 81861590\n")
            return []
    except Exception as e:
        print(f"Error loading test IDs: {e}")
        return []


# -------------------- Computer detection --------------------
def is_computer_ad(title):
    title_lower = title.lower()
    for pattern in COMPUTER_KEYWORDS:
        if re.search(pattern, title_lower):
            return True
    return False


# -------------------- Selenium setup --------------------
def create_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    )
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(options=chrome_options)
    driver.execute_cdp_cmd("Network.setUserAgentOverride", {
        "userAgent": driver.execute_script("return navigator.userAgent").replace("Headless", "")
    })
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    return driver


def fetch_new_items(driver):
    """Fetch new items from skelbiu.lt API using Selenium."""
    try:
        # Must be on the same origin before fetch() will work — CORS fix
        if "skelbiu.lt" not in driver.current_url:
            driver.get(SKELBIU_BASE)
            time.sleep(2)

        script = """
        return fetch('https://www.skelbiu.lt/index.php?mod=ajax&action=getNewItems&first_hit=0', {
            method: 'POST',
            headers: {
                'accept': 'application/json, text/javascript, */*; q=0.01',
                'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'x-requested-with': 'XMLHttpRequest'
            },
            body: 'type=false'
        }).then(response => response.json());
        """

        data = driver.execute_script(script)
        return data
    except Exception as e:
        print(f"Error fetching items: {e}")
        return None


def extract_listing_details(driver, item_id, item_url=None):
    """Extract title, price, description, and city from a listing page."""
    if item_url:
        # itemUrl from API is relative, e.g. /skelbimai/some-title-84147619.html
        url = item_url if item_url.startswith("http") else f"{SKELBIU_BASE}{item_url}"
    else:
        url = f"{SKELBIU_BASE}/skelbimai/{item_id}.html"

    try:
        driver.get(url)
        time.sleep(3)

        details = {
            "id": item_id,
            "url": url,
            "title": None,
            "price": None,
            "description": None,
            "city": None,
        }

        # Title — <h1> inside .item-title-container
        try:
            details["title"] = driver.find_element(
                By.CSS_SELECTOR, ".item-title-container h1"
            ).text.strip()
        except NoSuchElementException:
            try:
                details["title"] = driver.find_element(By.TAG_NAME, "h1").text.strip()
            except NoSuchElementException:
                print(f"Could not find title for ID {item_id}")

        # Price — <p class="price"> (absent for free/donation ads)
        try:
            details["price"] = driver.find_element(
                By.CSS_SELECTOR, "p.price"
            ).text.strip()
        except NoSuchElementException:
            print(f"Could not find price for ID {item_id} (may be free)")

        # City — <p class="cities"> inside .item-title-container
        try:
            details["city"] = driver.find_element(
                By.CSS_SELECTOR, ".item-title-container p.cities"
            ).text.strip()
        except NoSuchElementException:
            print(f"Could not find city for ID {item_id}")

        # Description — <div class="description ..."> inside .item-description
        try:
            details["description"] = driver.find_element(
                By.CSS_SELECTOR, ".item-description .description"
            ).text.strip()
        except NoSuchElementException:
            print(f"Could not find description for ID {item_id}")

        return details

    except Exception as e:
        print(f"Error extracting details for ID {item_id}: {e}")
        return None


# -------------------- Notifications --------------------
async def send_telegram_notification(details):
    """Send a Telegram notification with listing details."""
    message = "\n".join([
        "🎉 Hooray! Computer listing found!",
        "=" * 60,
        f"ID: {details['id']}",
        f"Title: {details['title']}",
        f"Price: {details['price']}",
        f"City: {details['city']}",
        f"Description: {details['description']}",
        f"URL: {details['url']}",
        "=" * 60,
    ])

    try:
        for chat_id in CHAT_IDs:
            await bot.send_message(
                chat_id=chat_id,
                text=message,
                disable_web_page_preview=True,
            )
        print(f"✅ Telegram notification sent to {len(CHAT_IDs)} recipient(s)")
    except Exception as e:
        print(f"❌ Error sending Telegram notification: {e}")


def format_details_message(details):
    return (
        f"\n{'=' * 60}\n"
        f"🎉 Hooray! Computer listing found!\n"
        f"{'=' * 60}\n"
        f"ID: {details['id']}\n"
        f"Title: {details['title']}\n"
        f"Price: {details['price']}\n"
        f"City: {details['city']}\n"
        f"Description: {details['description']}\n"
        f"URL: {details['url']}\n"
        f"{'=' * 60}\n"
    )


# -------------------- Monitor loops --------------------
async def monitor_production(driver, checked_ids):
    """Production mode: Check API for new items."""
    data = fetch_new_items(driver)

    if not data or "newItems" not in data:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] No data received or invalid format")
        return 0, 0

    new_items = data.get("newItems", [])
    new_count = 0
    computer_count = 0

    for item in new_items:
        item_id = str(item.get("id", ""))
        title = item.get("title", "")
        item_url = item.get("itemUrl", "")  # e.g. /skelbimai/some-title-84147619.html

        if not item_id or item_id in checked_ids:
            continue

        new_count += 1

        if is_computer_ad(title):
            computer_count += 1
            details = extract_listing_details(driver, item_id, item_url)

            if details:
                print(format_details_message(details))
                await send_telegram_notification(details)
            else:
                print(f"\n⚠️ Found computer ad {item_id} but could not extract details\n")

        add_checked_id(item_id, checked_ids)

    return new_count, computer_count


async def monitor_testing(driver, test_ids, checked_ids):
    """Testing mode: Process IDs from test file."""
    computer_count = 0

    for item_id in test_ids:
        if item_id in checked_ids:
            print(f"Skipping already checked ID: {item_id}")
            continue

        print(f"\n[TESTING] Processing ID: {item_id}")
        print(f"[TESTING] Visiting: {SKELBIU_BASE}/skelbimai/{item_id}.html")

        details = extract_listing_details(driver, item_id)

        if details and details["title"]:
            print(f"[TESTING] Title found: {details['title']}")

            if is_computer_ad(details["title"]):
                computer_count += 1
                print("[TESTING] ✓ This IS a MacBook/computer listing!")
                print(format_details_message(details))
                await send_telegram_notification(details)
            else:
                print("[TESTING] ✗ This is NOT a computer listing")
        else:
            print(f"\n⚠️ Could not extract details for ID {item_id}\n")
            if details:
                print(f"Debug - Details object: {details}")

        add_checked_id(item_id, checked_ids)
        time.sleep(2)

    return len(test_ids), computer_count


# -------------------- Entry point --------------------
async def main():
    mode_str = "TESTING MODE" if TESTING_MODE else "PRODUCTION MODE"
    print(f"Starting skelbiu.lt computer monitor in {mode_str}")
    print(f"Checking every {CHECK_INTERVAL} seconds")
    print(f"Checked IDs file: {CHECKED_IDS_FILE}")
    print("-" * 60)

    checked_ids = load_checked_ids()
    print(f"Loaded {len(checked_ids)} previously checked IDs")

    if TESTING_MODE:
        test_ids = load_test_ids()
        if not test_ids:
            print("No test IDs found. Please add IDs to test_ids.txt (one per line)")
            return

    driver = create_driver()

    try:
        if TESTING_MODE:
            print("\n[TESTING MODE] Processing test IDs...")
            total, computers = await monitor_testing(driver, test_ids, checked_ids)
            print(f"\n[TESTING MODE] Processed {total} test IDs, {computers} computers found")
        else:
            while True:
                try:
                    new_count, computer_count = await monitor_production(driver, checked_ids)

                    timestamp = datetime.now().strftime("%H:%M:%S")
                    if new_count > 0:
                        print(f"[{timestamp}] Processed {new_count} new items, {computer_count} computers found")
                    else:
                        print(f"[{timestamp}] No new items")

                except Exception as e:
                    print(f"Error in monitor loop: {e}")

                await asyncio.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        print("\nShutting down monitor...")
    finally:
        driver.quit()
        print("Driver closed. Goodbye!")


def _init_telegram():
    """Initialize Telegram bot — only needed when running standalone."""
    global bot, CHAT_IDs
    from dotenv import load_dotenv
    from telegram import Bot
    from telegram.request import HTTPXRequest

    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in .env")

    CHAT_IDs[:] = [
        int(cid.strip())
        for cid in (os.getenv("TELEGRAM_CHAT_IDS") or "").split(",")
        if cid.strip()
    ]
    if not CHAT_IDs:
        raise RuntimeError("TELEGRAM_CHAT_IDS is empty or invalid")

    httpx_request = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=65.0,
        write_timeout=20.0,
        pool_timeout=10.0,
        connection_pool_size=20,
    )
    bot = Bot(token=token, request=httpx_request)


if __name__ == "__main__":
    _init_telegram()
    asyncio.run(main())
