"""
One-time Facebook login script.

Opens a Chromium browser via Playwright, lets you log in manually,
then saves the session to fb_auth_state.json for the scraper to use.
"""

from playwright.sync_api import sync_playwright

import config


def main():
    print("Opening browser for Facebook login...")
    print("Log in to your account, then come back here and press Enter.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://www.facebook.com")

        input("Press Enter after you've logged in successfully...")

        context.storage_state(path=config.FB_AUTH_STATE_PATH)
        print(f"\nSession saved to {config.FB_AUTH_STATE_PATH}")

        browser.close()

    print("Done! You can now run the scraper.")


if __name__ == "__main__":
    main()
