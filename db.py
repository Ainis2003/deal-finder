import logging
from datetime import date, datetime, timezone

import aiosqlite

import config

logger = logging.getLogger("deal-finder.db")


async def init_db():
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS seen_listings (
                id TEXT PRIMARY KEY,
                platform TEXT,
                url TEXT,
                title TEXT,
                price INTEGER,
                verdict TEXT,
                seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

        # Migration: add columns if they don't exist (for existing DBs)
        cursor = await db.execute("PRAGMA table_info(seen_listings)")
        columns = {row[1] for row in await cursor.fetchall()}
        for col, col_type in [("url", "TEXT"), ("title", "TEXT"), ("price", "INTEGER"), ("verdict", "TEXT")]:
            if col not in columns:
                await db.execute(f"ALTER TABLE seen_listings ADD COLUMN {col} {col_type}")
                logger.info(f"Migrated seen_listings: added column '{col}'")
        await db.commit()


async def is_seen(listing_id: str) -> bool:
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM seen_listings WHERE id = ?", (listing_id,)
        )
        return await cursor.fetchone() is not None


async def mark_seen(listing_id: str, platform: str, url: str = None, title: str = None, price: int = None):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO seen_listings (id, platform, url, title, price) VALUES (?, ?, ?, ?, ?)",
            (listing_id, platform, url, title, price),
        )
        await db.commit()


async def update_verdict(listing_id: str, verdict: str):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE seen_listings SET verdict = ? WHERE id = ?",
            (verdict, listing_id),
        )
        await db.commit()


async def get_last_seen_time() -> datetime | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "SELECT MAX(seen_at) FROM seen_listings"
        )
        row = await cursor.fetchone()
        if row and row[0]:
            try:
                return datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                return None
    return None


async def get_today_counts() -> dict[str, int]:
    today = date.today().isoformat()
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "SELECT platform, COUNT(*) FROM seen_listings WHERE date(seen_at) = ? GROUP BY platform",
            (today,),
        )
        rows = await cursor.fetchall()
    return {row[0]: row[1] for row in rows}


async def get_total_counts() -> dict[str, int]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "SELECT platform, COUNT(*) FROM seen_listings GROUP BY platform"
        )
        rows = await cursor.fetchall()
    return {row[0]: row[1] for row in rows}


async def purge_old(days: int = None):
    if days is None:
        days = config.SEEN_LISTING_RETENTION_DAYS
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "DELETE FROM seen_listings WHERE seen_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        await db.commit()
