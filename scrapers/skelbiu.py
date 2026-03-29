"""
Skelbiu.lt scraper adapter.

This module wraps the owner's existing Skelbiu scraper script.
The adapter converts its output to the normalized listing format
and suppresses the original script's Telegram sends.

Implementation deferred until owner shares their existing script.
"""

import logging

logger = logging.getLogger("deal-finder.scrapers.skelbiu")


async def poll() -> list[dict]:
    """Poll Skelbiu.lt for new MacBook listings.

    Returns a list of normalized listing dicts.
    Raises NotImplementedError until the owner's script is integrated.
    """
    raise NotImplementedError(
        "Skelbiu adapter not yet implemented — awaiting owner's existing script"
    )
