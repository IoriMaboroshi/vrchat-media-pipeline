"""
Bilibili WBI signing algorithm.
Ref: https://github.com/SocialSisterYi/bilibili-API-collect (archived)
"""

import hashlib
import time
import httpx

from config import BILIBILI_UA

# Global cache for wbi keys (refreshed periodically)
_wbi_mixin_key: str = ""
_wbi_key_ts: float = 0.0
_WBI_KEY_TTL = 3600  # 1 hour


def _get_mixin_key(raw: str) -> str:
    """Extract mixin key from img_key + sub_key (first 32 chars)."""
    mixin = []
    for i in range(32):
        o = ord(raw[i])
        mixin.append(raw[o % len(raw)])
    return "".join(mixin)


async def _fetch_wbi_keys() -> tuple[str, str]:
    """Fetch img_key and sub_key from Bilibili nav API."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers={
                "User-Agent": BILIBILI_UA,
                "Referer": "https://www.bilibili.com",
            },
        )
        data = resp.json()["data"]
        wbi_img = data["wbi_img"]
        # wbi_img is like: {"img_url": "...", "sub_url": "..."}
        img_url = wbi_img["img_url"]
        sub_url = wbi_img["sub_url"]
        img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
        sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
        return img_key, sub_key


async def get_mixin_key() -> str:
    """Get or refresh the WBI mixin key."""
    global _wbi_mixin_key, _wbi_key_ts
    now = time.time()
    if not _wbi_mixin_key or (now - _wbi_key_ts) > _WBI_KEY_TTL:
        img_key, sub_key = await _fetch_wbi_keys()
        _wbi_mixin_key = _get_mixin_key(img_key + sub_key)[:32]
        _wbi_key_ts = now
    return _wbi_mixin_key


async def sign_params(params: dict) -> dict:
    """Sign params dict with WBI signature. Returns params with w_rid and wts."""
    mixin_key = await get_mixin_key()
    params["wts"] = int(time.time())
    # Sort keys and build query string
    sorted_keys = sorted(params.keys())
    query = "&".join(f"{k}={params[k]}" for k in sorted_keys)
    query += mixin_key
    params["w_rid"] = hashlib.md5(query.encode()).hexdigest()
    return params
