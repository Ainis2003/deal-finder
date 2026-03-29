import aiosqlite
import config


async def init_db():
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS seen_listings (
                id TEXT PRIMARY KEY,
                platform TEXT,
                seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


async def is_seen(listing_id: str) -> bool:
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM seen_listings WHERE id = ?", (listing_id,)
        )
        return await cursor.fetchone() is not None


async def mark_seen(listing_id: str, platform: str):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO seen_listings (id, platform) VALUES (?, ?)",
            (listing_id, platform),
        )
        await db.commit()


async def purge_old(days: int = None):
    if days is None:
        days = config.SEEN_LISTING_RETENTION_DAYS
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "DELETE FROM seen_listings WHERE seen_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        await db.commit()
