"""
Database query helpers.
"""
import aiosqlite
from db.database import get_db


async def insert_log(
    caller_ip: str,
    caller_geo: str,
    bvid: str,
    qx: str = "",
    qn: int = 0,
    video_url: str = "",
    audio_url: str = "",
    user_agent: str = "",
):
    db = await get_db()
    await db.execute(
        """INSERT INTO api_logs (caller_ip, caller_geo, bvid, qx, qn, video_url, audio_url, user_agent)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (caller_ip, caller_geo, bvid, qx, qn, video_url, audio_url, user_agent),
    )
    await db.commit()


async def get_daily_stats(days: int = 30) -> list[dict]:
    db = await get_db()
    cursor = await db.execute(
        """SELECT date(timestamp) as day, COUNT(*) as calls
           FROM api_logs
           WHERE timestamp >= datetime('now', ?)
           GROUP BY day ORDER BY day DESC""",
        (f"-{days} days",),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_monthly_stats(months: int = 12) -> list[dict]:
    db = await get_db()
    cursor = await db.execute(
        """SELECT strftime('%Y-%m', timestamp) as month, COUNT(*) as calls
           FROM api_logs
           WHERE timestamp >= datetime('now', ?)
           GROUP BY month ORDER BY month DESC""",
        (f"-{months * 30} days",),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_recent_logs(limit: int = 100) -> list[dict]:
    db = await get_db()
    cursor = await db.execute(
        """SELECT id, timestamp, caller_ip, caller_geo, bvid, qx, qn, user_agent
           FROM api_logs ORDER BY id DESC LIMIT ?""",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_ip_stats(limit: int = 50) -> list[dict]:
    db = await get_db()
    cursor = await db.execute(
        """SELECT caller_ip, caller_geo, COUNT(*) as calls, MAX(timestamp) as last_call
           FROM api_logs
           GROUP BY caller_ip ORDER BY calls DESC LIMIT ?""",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_total_calls() -> int:
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) as total FROM api_logs")
    row = await cursor.fetchone()
    return row["total"] if row else 0


async def get_today_calls() -> int:
    """Get total API calls for today."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT COUNT(*) as total FROM api_logs WHERE date(timestamp) = date('now')"
    )
    row = await cursor.fetchone()
    return row["total"] if row else 0


async def get_this_month_calls() -> int:
    """Get total API calls for this month."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT COUNT(*) as total FROM api_logs "
        "WHERE strftime('%Y-%m', timestamp) = strftime('%Y-%m', 'now')"
    )
    row = await cursor.fetchone()
    return row["total"] if row else 0


async def get_month_daily_stats(year: int, month: int) -> list:
    """Get daily call counts for a specific month."""
    db = await get_db()
    month_str = f"{year:04d}-{month:02d}"
    cursor = await db.execute(
        "SELECT date(timestamp) as day, COUNT(*) as calls "
        "FROM api_logs "
        "WHERE strftime('%Y-%m', timestamp) = ? "
        "GROUP BY day ORDER BY day ASC",
        (month_str,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_day_detail(date_str: str) -> list:
    """Get detailed logs for a specific date (BVIDs, ips, etc)."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT bvid, caller_ip, caller_geo, qx, qn, timestamp "
        "FROM api_logs "
        "WHERE date(timestamp) = ? "
        "ORDER BY timestamp DESC",
        (date_str,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def cleanup_old_logs(retention_days: int = 30):
    db = await get_db()
    await db.execute(
        "DELETE FROM api_logs WHERE timestamp < datetime('now', ?)",
        (f"-{retention_days} days",),
    )
    await db.commit()


# === Settings table helpers ===

async def get_setting(key: str) -> str:
    """Get a single setting value by key. Returns empty string if not found."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT value FROM settings WHERE key = ?",
        (key,),
    )
    row = await cursor.fetchone()
    return row["value"] if row else ""


async def set_setting(key: str, value: str) -> None:
    """Insert or update a setting key-value pair."""
    db = await get_db()
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    await db.commit()


async def get_all_settings() -> dict:
    """Get all settings as a dict."""
    db = await get_db()
    cursor = await db.execute("SELECT key, value FROM settings")
    rows = await cursor.fetchall()
    return {r["key"]: r["value"] for r in rows}


# === Web users table helpers ===

async def create_user(username: str, password_hash: str) -> bool:
    """Create a new web user. Returns True on success, False if username exists."""
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO web_users (username, password_hash) VALUES (?, ?)",
            (username, password_hash),
        )
        await db.commit()
        return True
    except Exception:
        return False


async def verify_user(username: str, password: str) -> bool:
    """Verify username and password against DB. Returns True if valid."""
    import hashlib
    db = await get_db()
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    cursor = await db.execute(
        "SELECT password_hash FROM web_users WHERE username = ?",
        (username,),
    )
    row = await cursor.fetchone()
    if row and row["password_hash"] == password_hash:
        return True
    return False


async def update_user_password(username: str, new_hash: str) -> bool:
    """Update a user's password hash. Returns True on success."""
    db = await get_db()
    cursor = await db.execute(
        "UPDATE web_users SET password_hash = ? WHERE username = ?",
        (new_hash, username),
    )
    await db.commit()
    return cursor.rowcount > 0


async def rename_user(old_username: str, new_username: str) -> bool:
    """Rename a web user. Returns True on success, False if new username already exists."""
    db = await get_db()
    # Check if new username already exists
    cursor = await db.execute(
        "SELECT COUNT(*) as cnt FROM web_users WHERE username = ?",
        (new_username,),
    )
    row = await cursor.fetchone()
    if row and row["cnt"] > 0:
        return False
    # Rename the user
    cursor = await db.execute(
        "UPDATE web_users SET username = ? WHERE username = ?",
        (new_username, old_username),
    )
    await db.commit()
    return cursor.rowcount > 0
