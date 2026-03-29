"""
Pre-run verification script.
Checks all dependencies, secrets, and data files before first run.
"""

import asyncio
import json
import os
import sys

import config


def check(name: str, passed: bool, detail: str = ""):
    icon = "✅" if passed else "❌"
    msg = f"{icon} {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return passed


def check_config():
    """Check all config.py secrets are filled in."""
    secrets = {
        "ANTHROPIC_API_KEY": config.ANTHROPIC_API_KEY,
        "TELEGRAM_BOT_TOKEN": config.TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": config.TELEGRAM_CHAT_ID,
    }
    all_ok = True
    for name, value in secrets.items():
        ok = bool(value and value.strip())
        check(f"config.{name}", ok, "set" if ok else "EMPTY")
        if not ok:
            all_ok = False
    return all_ok


def check_data_files():
    """Check all 4 data JSON files exist and have content."""
    files = ["prices.json", "repair_costs.json", "disassembly_costs.json", "shipping_costs.json"]
    all_ok = True
    for fname in files:
        path = os.path.join(config.DATA_DIR, fname)
        if not os.path.exists(path):
            check(f"data/{fname}", False, "file not found")
            all_ok = False
            continue
        try:
            with open(path) as f:
                data = json.load(f)
            has_content = len(data) > 0
            check(f"data/{fname}", has_content, f"{len(data)} entries" if has_content else "EMPTY")
            if not has_content:
                all_ok = False
        except json.JSONDecodeError as e:
            check(f"data/{fname}", False, f"invalid JSON: {e}")
            all_ok = False
    return all_ok


def check_fb_auth():
    """Check Facebook auth state file exists."""
    exists = os.path.exists(config.FB_AUTH_STATE_PATH)
    check("Facebook auth state", exists, config.FB_AUTH_STATE_PATH if exists else "run setup_fb_auth.py first")
    return exists


async def check_anthropic():
    """Check Anthropic API is reachable."""
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=10,
            messages=[{"role": "user", "content": "Reply with just 'ok'"}],
        )
        text = response.content[0].text.strip()
        check("Anthropic API", True, f"response: {text}")
        return True
    except Exception as e:
        check("Anthropic API", False, str(e))
        return False


async def check_telegram():
    """Check Telegram bot can send a message."""
    try:
        from telegram import Bot
        tbot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        await tbot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text="🔧 deal-finder setup check — if you see this, Telegram is working!",
        )
        check("Telegram bot", True, "test message sent")
        return True
    except Exception as e:
        check("Telegram bot", False, str(e))
        return False


async def main():
    print("deal-finder — Pre-run check\n")

    results = []

    # Config
    results.append(check_config())

    # Data files
    results.append(check_data_files())

    # FB auth
    results.append(check_fb_auth())

    # Anthropic (only if key is set)
    if config.ANTHROPIC_API_KEY:
        results.append(await check_anthropic())
    else:
        check("Anthropic API", False, "skipped — no API key")
        results.append(False)

    # Telegram (only if token is set)
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        results.append(await check_telegram())
    else:
        check("Telegram bot", False, "skipped — no token/chat ID")
        results.append(False)

    print()
    if all(results):
        print("All checks passed! Ready to run.")
    else:
        print("Some checks failed. Fix the issues above before running.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
