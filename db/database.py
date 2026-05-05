import hashlib
from typing import Optional
import aiosqlite
from config import DB_PATH

_conn: Optional[aiosqlite.Connection] = None


async def get_db() -> aiosqlite.Connection:
    global _conn
    if _conn is None:
        _conn = await aiosqlite.connect(DB_PATH)
        _conn.row_factory = aiosqlite.Row
        await _conn.execute("PRAGMA journal_mode=WAL")
        await _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


async def init_db():
    db = await get_db()
    await db.execute("""
        CREATE TABLE IF NOT EXISTS api_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            caller_ip TEXT NOT NULL,
            caller_geo TEXT DEFAULT '',
            bvid TEXT NOT NULL,
            qx TEXT DEFAULT '',
            qn INTEGER DEFAULT 0,
            video_url TEXT DEFAULT '',
            audio_url TEXT DEFAULT '',
            user_agent TEXT DEFAULT ''
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS web_users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    await db.commit()

    # Insert default admin user if no users exist
    cursor = await db.execute("SELECT COUNT(*) as cnt FROM web_users")
    row = await cursor.fetchone()
    if row and row["cnt"] == 0:
        default_hash = hashlib.sha256("password".encode()).hexdigest()
        await db.execute(
            "INSERT INTO web_users (username, password_hash) VALUES (?, ?)",
            ("admin", default_hash),
        )
        await db.commit()


async def close_db():
    global _conn
    if _conn:
        await _conn.close()
        _conn = None
