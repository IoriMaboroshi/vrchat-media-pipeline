import os
import json
import hashlib

# === VRChat Media Pipeline ===
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "14515"))
INTERNAL_API_HOST = os.getenv("INTERNAL_API_HOST", "0.0.0.0")
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))

# === Auth ===
API_TOKEN = os.getenv("API_TOKEN", "change_this_token")

# === Platform API ===
PLATFORM_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
PLATFORM_REFERER = "https://www.bilibili.com"

# === FFmpeg ===
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")

# === Aria2 ===
ARIA2_CONNECTIONS = 32

# === Quality mapping (qx → quality number) ===
QUALITY_MAP: dict[str, int] = {
    "4k": 120,
    "1080p60": 112,
    "1080p": 64,
    "720p": 32,
    "480p": 16,
    "360p": 8,
}

DEFAULT_QN = 64
DEFAULT_QX = "1080p"

# Quality fallback order: best → worst
QUALITY_FALLBACK_ORDER: list[int] = [120, 112, 64, 32, 16, 8]

# === Web auth defaults ===
WEB_USERNAME = "admin"
WEB_PASSWORD_HASH = hashlib.sha256("password".encode()).hexdigest()

# === Dynamic settings (runtime, from DB) ===
DYNAMIC_SETTINGS: dict[str, str] = {
    "web_username": "admin",
    "ffmpeg_path": "ffmpeg",
    "default_quality": "1080p",
    "api_token": "change_this_token",
    "api_port": "14515",
    "web_port": "8080",
    "enable_web_auth": "1",
    "log_retention_days": "30",
    "cookie_refresh_interval": "24",
    "aria2_connections": "32",
    "public_base_url": "",
}

# === Paths ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "media_pipeline.db")
COOKIE_FILE = os.path.join(DATA_DIR, "cookies.json")

os.makedirs(DATA_DIR, exist_ok=True)


def save_cookies(cookies: dict) -> None:
    with open(COOKIE_FILE, "w") as f:
        json.dump(cookies, f)


def load_cookies() -> dict:
    if os.path.exists(COOKIE_FILE):
        with open(COOKIE_FILE) as f:
            return json.load(f)
    return {}


def clear_cookies() -> None:
    if os.path.exists(COOKIE_FILE):
        os.remove(COOKIE_FILE)


# === Backward compatibility aliases ===
BILIBILI_UA = PLATFORM_UA
BILIBILI_REFERER = PLATFORM_REFERER

async def load_dynamic_settings() -> None:
    """Load all dynamic settings from DB into the DYNAMIC_SETTINGS dict."""
    from db.models import get_all_settings
    try:
        db_settings = await get_all_settings()
        for key in DYNAMIC_SETTINGS:
            if key in db_settings:
                DYNAMIC_SETTINGS[key] = db_settings[key]
        if "api_token" in db_settings:
            global API_TOKEN
            API_TOKEN = db_settings["api_token"]
        if "ffmpeg_path" in db_settings and db_settings["ffmpeg_path"]:
            global FFMPEG_PATH
            FFMPEG_PATH = db_settings["ffmpeg_path"]
        if "web_username" in db_settings and db_settings["web_username"]:
            global WEB_USERNAME
            WEB_USERNAME = db_settings["web_username"]
    except Exception:
        pass


async def get_dynamic_setting(key: str) -> str:
    return DYNAMIC_SETTINGS.get(key, "")


async def set_dynamic_setting(key: str, value: str) -> None:
    if key in DYNAMIC_SETTINGS:
        DYNAMIC_SETTINGS[key] = value
    from db.models import set_setting
    await set_setting(key, value)
    if key == "api_token":
        global API_TOKEN
        API_TOKEN = value
    if key == "ffmpeg_path" and value:
        global FFMPEG_PATH
        FFMPEG_PATH = value
    if key == "web_username" and value:
        global WEB_USERNAME
        WEB_USERNAME = value
    if key == "default_quality":
        global DEFAULT_QX
        DEFAULT_QX = value
        global DEFAULT_QN
        DEFAULT_QN = QUALITY_MAP.get(value, 64)
    if key == "aria2_connections" and value.isdigit():
        global ARIA2_CONNECTIONS
        ARIA2_CONNECTIONS = int(value)


async def init_config() -> None:
    await load_dynamic_settings()
