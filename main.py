"""
VRChat Media Pipeline — HLS transcoding + distribution pipeline for VRChat.

Two listeners:
  - API: 0.0.0.0:14515 (internal, nginx reverse proxy)
  - Web: 0.0.0.0:8080 (management panel)
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from config import INTERNAL_API_HOST, API_PORT, WEB_PORT, init_config
from db.database import init_db, close_db
from bilibili.auth import refresh_user_info, check_cookie_valid
from api.routes import (
    router as api_router,
    init_codec,
    _cleanup_stale_hls,
    _load_quality_aliases,
    _load_transcode_settings,
)
from utils.downloader import cleanup_expired_cache
from web.routes import router as web_router

# Logging
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "proxy.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger("bilibili-proxy")


async def _cookie_check_loop():
    """Background: check B站 cookie validity every 6 hours."""
    await asyncio.sleep(60)  # Wait 1 min after startup
    while True:
        result = await check_cookie_valid()
        if not result.get("valid"):
            logger.warning("[CookieCheck] B站 Cookie 已过期，需要重新扫码登录")
        else:
            logger.info("[CookieCheck] B站 Cookie 有效")
        await asyncio.sleep(6 * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()
    await init_config()
    await refresh_user_info()

    # Load settings
    _load_quality_aliases()
    _load_transcode_settings()

    # Probe FFmpeg hardware encoders
    init_codec()

    # Clean up expired DASH cache on startup
    cleanup_expired_cache()

    # Background tasks
    cookie_task = asyncio.create_task(_cookie_check_loop())
    cleanup_task = asyncio.create_task(_cleanup_stale_hls())

    logger.info("VRChat BPlayer Proxy started (HLS mode)")
    yield

    # Shutdown
    cookie_task.cancel()
    cleanup_task.cancel()
    try:
        await cookie_task
        await cleanup_task
    except asyncio.CancelledError:
        pass
    await close_db()
    logger.info("VRChat BPlayer Proxy stopped")


app = FastAPI(
    title="VRChat Media Pipeline",
    description="Media preprocessing and HLS distribution pipeline for VRChat",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routes
app.include_router(api_router, tags=["API"])

# Web panel (separate server instance)
web_app = FastAPI(title="VRChat BPlayer Proxy - Web Panel")
web_app.include_router(web_router, tags=["Web"])


async def main():
    configs = [
        uvicorn.Config(app, host=INTERNAL_API_HOST, port=API_PORT, log_level="info"),
        uvicorn.Config(web_app, host="0.0.0.0", port=WEB_PORT, log_level="info"),
    ]
    servers = [uvicorn.Server(c) for c in configs]
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
