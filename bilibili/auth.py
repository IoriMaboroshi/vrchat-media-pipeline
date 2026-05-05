"""
Bilibili QR code login and user info.
"""

import time
import logging
import httpx

from config import BILIBILI_UA, BILIBILI_REFERER, save_cookies, load_cookies, clear_cookies

logger = logging.getLogger("bilibili-proxy.auth")

# Global session holding cookies
_current_cookies: dict = {}
_bili_user_info: dict = {}
_cookie_last_check: float = 0
_cookie_expiry_info: str = "unknown"


def get_current_cookies() -> dict:
    return _current_cookies


def get_user_info() -> dict:
    return _bili_user_info


def _cookie_expiry_estimate() -> str:
    """Return estimated cookie expiry info."""
    return _cookie_expiry_info


async def check_cookie_valid() -> dict:
    """
    Check if current B站 cookies are still valid by calling /x/web-interface/nav.
    Returns {"valid": bool, "user": dict or None}
    """
    global _cookie_expiry_info, _cookie_last_check
    cookies = _current_cookies or load_cookies()
    if not cookies:
        _cookie_expiry_info = "expired"
        return {"valid": False, "user": None}

    async with httpx.AsyncClient(timeout=10, cookies=cookies) as client:
        try:
            resp = await client.get(
                "https://api.bilibili.com/x/web-interface/nav",
                headers={
                    "User-Agent": BILIBILI_UA,
                    "Referer": BILIBILI_REFERER,
                },
            )
            data = resp.json()
            if data.get("code") == 0 and data.get("data", {}).get("isLogin"):
                info = data["data"]
                _cookie_expiry_info = "valid"
                _cookie_last_check = time.time()
                return {
                    "valid": True,
                    "user": {
                        "uid": info.get("mid"),
                        "nickname": info.get("uname"),
                        "avatar": info.get("face"),
                        "vip": info.get("vipStatus", 0),
                        "level": info.get("level_info", {}).get("current_level", 0),
                    },
                }
            else:
                _cookie_expiry_info = "expired"
                _cookie_last_check = time.time()
                return {"valid": False, "user": None}
        except Exception as e:
            logger.warning("Cookie validity check failed: %s", e)
            return {"valid": False, "user": None}


async def generate_qrcode() -> dict:
    """Generate a QR code login session. Returns {url, qrcode_key}."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
            headers={
                "User-Agent": BILIBILI_UA,
                "Referer": BILIBILI_REFERER,
            },
        )
        data = resp.json()
        if data["code"] != 0:
            return {"error": data.get("message", "Unknown error")}
        return {
            "url": data["data"]["url"],
            "qrcode_key": data["data"]["qrcode_key"],
        }


async def poll_qrcode(qrcode_key: str) -> dict:
    """
    Poll QR code login status.
    Returns:
      code 0: success (cookies are auto-saved)
      code 86101: not scanned
      code 86090: scanned but not confirmed
      code 86038: expired
    """
    global _current_cookies, _bili_user_info

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
            params={"qrcode_key": qrcode_key},
            headers={
                "User-Agent": BILIBILI_UA,
                "Referer": BILIBILI_REFERER,
            },
        )
        data = resp.json()["data"]

        code_msg = {
            0: "登录成功",
            86101: "等待扫码",
            86090: "已扫码，请在手机上确认",
            86038: "二维码已过期",
        }

        result = {
            "code": data.get("code", -1),
            "message": code_msg.get(data.get("code"), data.get("message", "未知状态")),
        }

        if data.get("code") == 0:
            # Login success — extract cookies from response
            raw_cookies = {}
            for cookie in resp.headers.get_list("set-cookie", ""):
                if isinstance(cookie, str) and "=" in cookie:
                    parts = cookie.split(";")[0].split("=", 1)
                    if len(parts) == 2:
                        raw_cookies[parts[0].strip()] = parts[1].strip()

            _current_cookies = raw_cookies
            save_cookies(raw_cookies)

            # Fetch user info
            await refresh_user_info()
            result["user"] = _bili_user_info

        return result


async def refresh_user_info() -> None:
    """Fetch current user info and update global state."""
    global _bili_user_info, _current_cookies, _cookie_expiry_info
    cookies = _current_cookies or load_cookies()
    if not cookies:
        _bili_user_info = {}
        _cookie_expiry_info = "expired"
        return

    async with httpx.AsyncClient(timeout=10, cookies=cookies) as client:
        try:
            resp = await client.get(
                "https://api.bilibili.com/x/web-interface/nav",
                headers={
                    "User-Agent": BILIBILI_UA,
                    "Referer": BILIBILI_REFERER,
                },
            )
            data = resp.json()
            if data["code"] == 0 and data.get("data", {}).get("isLogin"):
                info = data["data"]
                _bili_user_info = {
                    "uid": info.get("mid"),
                    "nickname": info.get("uname"),
                    "avatar": info.get("face"),
                    "vip": info.get("vipStatus", 0),
                    "level": info.get("level_info", {}).get("current_level", 0),
                    "is_login": True,
                }
                _current_cookies = cookies
                _cookie_expiry_info = "valid"
                save_cookies(cookies)
            else:
                _bili_user_info = {"is_login": False}
                _cookie_expiry_info = "expired"
        except Exception:
            _bili_user_info = {"is_login": False}
            _cookie_expiry_info = "unknown"


async def refresh_cookies() -> dict:
    """
    Try to refresh Bilibili cookies.
    Returns new cookie info or error.
    """
    global _current_cookies
    cookies = _current_cookies or load_cookies()
    if not cookies:
        return {"error": "没有已保存的 Cookie"}

    async with httpx.AsyncClient(timeout=10, cookies=cookies) as client:
        try:
            resp = await client.get(
                "https://passport.bilibili.com/x/passport-login/web/cookie/info",
                headers={
                    "User-Agent": BILIBILI_UA,
                    "Referer": BILIBILI_REFERER,
                },
            )
            data = resp.json()
            if data["code"] == 0:
                await refresh_user_info()
                return {"status": "ok", "user": _bili_user_info}
            return {"error": data.get("message", "Cookie 刷新失败")}
        except Exception as e:
            return {"error": str(e)}


def do_logout() -> None:
    """Clear all stored cookies and user info."""
    global _current_cookies, _bili_user_info, _cookie_expiry_info
    _current_cookies = {}
    _bili_user_info = {}
    _cookie_expiry_info = "expired"
    clear_cookies()
