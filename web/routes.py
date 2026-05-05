"""
VRChat BPlayer Proxy web panel routes (EasyTier internal only).
"""

import io
import base64
import hashlib
import secrets
import os

import qrcode
from fastapi import APIRouter, Request, Form, Query, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from config import DYNAMIC_SETTINGS, FFMPEG_PATH as CFG_FFMPEG_PATH
from utils.codec_adapter import QUALITY_MAP, QUALITY_LABELS
from bilibili.auth import (
    generate_qrcode,
    poll_qrcode,
    refresh_user_info,
    refresh_cookies,
    do_logout,
    get_user_info,
    get_current_cookies,
    load_cookies,
)
from bilibili.video import (
    get_video_info,
    get_play_url_comprehensive,
    get_episode_info,
)
from db.models import (
    get_daily_stats,
    get_monthly_stats,
    get_recent_logs,
    get_ip_stats,
    get_total_calls,
    get_today_calls,
    get_this_month_calls,
    get_month_daily_stats,
    get_day_detail,
    verify_user as db_verify_user,
    update_user_password,
    get_all_settings,
    rename_user,
)
from utils.middleware import create_session, destroy_session, validate_session, login_required

router = APIRouter()

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


# ============================================================
#  LOGIN ROUTES
# ============================================================

@router.get("/login", response_class=HTMLResponse)
async def web_login_page(request: Request):
    """Web login form. If web auth is disabled, skip directly."""
    from config import DYNAMIC_SETTINGS
    enable_web_auth = DYNAMIC_SETTINGS.get("enable_web_auth", "1")
    
    if enable_web_auth != "1":
        # Web auth disabled, skip to dashboard or QR login
        cookies = get_current_cookies() or load_cookies()
        if cookies:
            return RedirectResponse(url="/dashboard")
        return RedirectResponse(url="/qr-login")
    
    # Web auth enabled: always show login form
    return templates.TemplateResponse("web_login.html", {
        "request": request,
        "error": None,
    })


@router.post("/login")
async def web_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    """Handle login form submission."""
    valid = await db_verify_user(username, password)
    if not valid:
        return templates.TemplateResponse("web_login.html", {
            "request": request,
            "error": "用户名或密码错误",
        })

    # Create session
    token = create_session(username)
    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        max_age=86400,
        samesite="lax",
    )
    return response


@router.get("/logout")
async def web_logout(request: Request):
    """Logout: clear web session and bilibili cookies."""
    session_token = request.cookies.get("session_token", "")
    if session_token:
        destroy_session(session_token)
    do_logout()
    response = RedirectResponse(url="/login")
    response.delete_cookie("session_token")
    return response


# ============================================================
#  PROTECTED ROUTES (login_required)
# ============================================================

@router.get("/", response_class=HTMLResponse)
async def root_redirect():
    """Redirect root based on auth state.
    
    - enable_web_auth ON  → always go to /login first (web auth required)
    - enable_web_auth OFF → go to /dashboard (cookies exist) or /qr-login (need bilibili login)
    """
    from config import DYNAMIC_SETTINGS
    enable_web_auth = DYNAMIC_SETTINGS.get("enable_web_auth", "1")
    
    if enable_web_auth == "1":
        return RedirectResponse(url="/login")
    
    cookies = get_current_cookies() or load_cookies()
    if cookies:
        return RedirectResponse(url="/dashboard")
    else:
        return RedirectResponse(url="/qr-login")


@router.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(login_required)])
async def dashboard(request: Request):
    """Main dashboard."""
    # Ensure cookies are loaded (may have been loaded from file by root route)
    cookies = get_current_cookies() or load_cookies()
    if not cookies:
        # No bilibili login at all - redirect to QR login
        return RedirectResponse(url="/qr-login")
    
    await refresh_user_info()
    user = get_user_info()
    
    # If refresh showed not logged in but we have cookies, try one more time
    if not user.get("is_login") and cookies:
        await refresh_user_info()
        user = get_user_info()

    total = await get_total_calls()
    today = await get_today_calls()
    this_month = await get_this_month_calls()
    recent = await get_recent_logs(20)
    daily = await get_daily_stats(7)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "cookies_exist": bool(cookies),
        "total_calls": total,
        "today_calls": today,
        "this_month_calls": this_month,
        "recent_logs": recent,
        "daily_stats": daily,
        "quality_map": QUALITY_MAP,
    })


@router.get("/stats", response_class=HTMLResponse, dependencies=[Depends(login_required)])
async def stats(request: Request):
    """Usage statistics page."""
    monthly = await get_monthly_stats(12)
    daily = await get_daily_stats(30)
    ip_stats = await get_ip_stats(50)
    recent = await get_recent_logs(50)
    total = await get_total_calls()

    return templates.TemplateResponse("stats.html", {
        "request": request,
        "monthly_stats": monthly,
        "daily_stats": daily,
        "ip_stats": ip_stats,
        "recent_logs": recent,
        "total_calls": total,
        "user": get_user_info(),
    })


@router.get("/help", response_class=HTMLResponse, dependencies=[Depends(login_required)])
async def api_help(request: Request):
    """API documentation page."""
    return templates.TemplateResponse("help.html", {
        "request": request,
        "user": get_user_info(),
    })


@router.get("/generator", response_class=HTMLResponse, dependencies=[Depends(login_required)])
async def url_generator(request: Request):
    """URL generator page."""
    from config import API_TOKEN as CFG_TOKEN
    return templates.TemplateResponse("generator.html", {
        "request": request,
        "user": get_user_info(),
        "api_token": CFG_TOKEN,
        "public_base_url": DYNAMIC_SETTINGS.get("public_base_url", ""),
    })


@router.get("/preload", response_class=HTMLResponse, dependencies=[Depends(login_required)])
async def preload_page(request: Request):
    """Preload management page."""
    from config import API_TOKEN as CFG_TOKEN
    return templates.TemplateResponse("preload.html", {
        "request": request,
        "user": get_user_info(),
        "api_token": CFG_TOKEN,
        "public_base_url": DYNAMIC_SETTINGS.get("public_base_url", ""),
    })


# ============================================================
#  STATS API ENDPOINTS (AJAX)
# ============================================================

@router.get("/api/stats/daily")
async def api_stats_daily(year: int = Query(...), month: int = Query(...)):
    """Get daily call stats for a specific month."""
    stats = await get_month_daily_stats(year, month)
    return JSONResponse(stats)


@router.get("/api/stats/daily-detail")
async def api_stats_daily_detail(date: str = Query(...)):
    """Get detailed call records for a specific date."""
    details = await get_day_detail(date)
    return JSONResponse(details)


@router.get("/api/video-summary")
async def api_video_summary(
    bvid = Query(None),
    ep_id = Query(None),
):
    """Get comprehensive video summary for URL generator."""

    if not bvid and not ep_id:
        return JSONResponse({"detail": "需要提供 bvid 或 ep_id 参数"}, status_code=400)

    # Resolve ep_id to bvid if needed
    resolved_bvid = bvid
    if ep_id and not bvid:
        clean_ep_id = str(ep_id)
        if clean_ep_id.lower().startswith("ep"):
            clean_ep_id = clean_ep_id[2:]
        try:
            ep_data = await get_episode_info(clean_ep_id)
        except Exception:
            return JSONResponse({"detail": "获取剧集信息失败"}, status_code=400)
        if "error" in ep_data:
            return JSONResponse({"detail": ep_data["error"]}, status_code=400)
        current = ep_data.get("current", {})
        if not current:
            return JSONResponse({"detail": "未找到该剧集"}, status_code=400)
        resolved_bvid = current.get("bvid", "")
        if not resolved_bvid and ep_data.get("episodes"):
            resolved_bvid = ep_data["episodes"][0].get("bvid", "")

    if not resolved_bvid:
        return JSONResponse({"detail": "无法解析视频 ID"}, status_code=400)

    try:
        info = await get_video_info(resolved_bvid)
    except Exception:
        return JSONResponse({"detail": "获取视频信息失败"}, status_code=400)

    if "error" in info:
        return JSONResponse({"detail": info["error"]}, status_code=400)

    # Get available qualities
    try:
        play_data = await get_play_url_comprehensive(
            bvid=resolved_bvid,
            qx="1080p",
        )
    except Exception:
        play_data = {}

    qualities: list = []
    all_qns = play_data.get("all_available_qualities", [])
    for qn in all_qns:
        qualities.append({
            "qn": qn,
            "label": QUALITY_LABELS.get(qn, f"未知 ({qn})"),
        })

    return JSONResponse({
        "bvid": resolved_bvid,
        "title": info.get("title", ""),
        "cover": info.get("cover", ""),
        "duration": info.get("duration", 0),
        "owner": info.get("owner", {}),
        "pages": info.get("pages", []),
        "qualities": qualities,
    })


@router.get("/settings", response_class=HTMLResponse, dependencies=[Depends(login_required)])
async def settings_page(request: Request):
    """Settings page."""
    from config import API_TOKEN as CFG_TOKEN
    settings_data = await get_all_settings()
    quality_keys = list(QUALITY_MAP.keys())

    # Load transcode settings for max_output_resolution
    try:
        import json
        import os
        from config import BASE_DIR
        tsf = os.path.join(BASE_DIR, "data", "transcode_settings.json")
        if os.path.exists(tsf):
            with open(tsf) as f:
                ts = json.load(f)
            max_output_resolution = ts.get("max_output_resolution", "")
        else:
            max_output_resolution = ""
    except Exception:
        max_output_resolution = ""

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "user": get_user_info(),
        "quality_map": QUALITY_MAP,
        "quality_keys": quality_keys,
        "settings": settings_data,
        "api_token": CFG_TOKEN,
        "ffmpeg_path": CFG_FFMPEG_PATH,
        "default_quality": DYNAMIC_SETTINGS.get("default_quality", "1080p"),
        "api_port": DYNAMIC_SETTINGS.get("api_port", "14515"),
        "web_port": DYNAMIC_SETTINGS.get("web_port", "8080"),
        "log_retention_days": DYNAMIC_SETTINGS.get("log_retention_days", "30"),
        "cookie_refresh_interval": DYNAMIC_SETTINGS.get("cookie_refresh_interval", "24"),
        "enable_web_auth": DYNAMIC_SETTINGS.get("enable_web_auth", "1"),
        "max_output_resolution": max_output_resolution,
        "aria2_connections": DYNAMIC_SETTINGS.get("aria2_connections", "32"),
        "public_base_url": DYNAMIC_SETTINGS.get("public_base_url", ""),
    })


@router.post("/settings/save")
async def settings_save(
    request: Request,
    username: str = Form(None),
    new_username: str = Form(None),
    current_password: str = Form(None),
    new_password: str = Form(None),
    new_password_confirm: str = Form(None),
    api_token: str = Form(None),
    default_quality: str = Form(None),
    api_port: str = Form(None),
    web_port: str = Form(None),
    ffmpeg_path: str = Form(None),
    log_retention_days: str = Form(None),
    cookie_refresh_interval: str = Form(None),
    enable_web_auth: str = Form(None),
    max_output_resolution: str = Form(None),
    aria2_connections: str = Form(None),
    public_base_url: str = Form(None),
):
    """Save settings from the settings form."""
    from config import set_dynamic_setting, WEB_USERNAME

    messages = []

    # Handle username change
    if new_username and new_username.strip() and new_username.strip() != WEB_USERNAME:
        if not current_password:
            return JSONResponse({"success": False, "message": "修改用户名需要输入当前密码"})
        valid = await db_verify_user(WEB_USERNAME, current_password)
        if not valid:
            return JSONResponse({"success": False, "message": "当前密码错误"})
        ok = await rename_user(WEB_USERNAME, new_username.strip())
        if not ok:
            return JSONResponse({"success": False, "message": "新用户名已存在"})
        await set_dynamic_setting("web_username", new_username.strip())
        messages.append("用户名已更新")

    # Handle password change
    if new_password and new_password.strip():
        if new_password != new_password_confirm:
            return JSONResponse({"success": False, "message": "两次密码不一致"})
        if not current_password:
            return JSONResponse({"success": False, "message": "需要当前密码才能修改密码"})
        # Verify current password
        valid = await db_verify_user(WEB_USERNAME, current_password)
        if not valid:
            return JSONResponse({"success": False, "message": "当前密码错误"})
        new_hash = hashlib.sha256(new_password.encode()).hexdigest()
        await update_user_password(WEB_USERNAME, new_hash)
        messages.append("密码已更新")

    # Save API token
    if api_token is not None and api_token.strip():
        await set_dynamic_setting("api_token", api_token.strip())
        messages.append("API Token 已更新")

    # Save quality settings
    if default_quality is not None:
        await set_dynamic_setting("default_quality", default_quality)
        messages.append("默认清晰度已更新")

    # Save boolean settings
    if enable_web_auth is not None:
        await set_dynamic_setting("enable_web_auth", "1" if enable_web_auth in ("1", "on", "true", True) else "0")

    # Save server settings
    if ffmpeg_path is not None and ffmpeg_path.strip():
        await set_dynamic_setting("ffmpeg_path", ffmpeg_path.strip())
        messages.append("FFmpeg 路径已更新")

    if api_port is not None and api_port.strip().isdigit():
        await set_dynamic_setting("api_port", api_port.strip())
        messages.append("API 端口已更新（需重启）")

    if web_port is not None and web_port.strip().isdigit():
        await set_dynamic_setting("web_port", web_port.strip())
        messages.append("Web 面板端口已更新（需重启）")

    if log_retention_days is not None:
        await set_dynamic_setting("log_retention_days", str(log_retention_days))
        messages.append("日志保留天数已更新")

    if cookie_refresh_interval is not None:
        await set_dynamic_setting("cookie_refresh_interval", str(cookie_refresh_interval))
        messages.append("Cookie 刷新间隔已更新")

    # Save max output resolution (transcode cap)
    if max_output_resolution is not None:
        from api.routes import set_transcode_setting
        set_transcode_setting("max_output_resolution", max_output_resolution)
        if max_output_resolution:
            messages.append("最高画质上限已设为 %s p" % max_output_resolution)
        else:
            messages.append("最高画质上限已取消（无限制）")

    # Save aria2 connections
    if aria2_connections is not None and aria2_connections.strip().isdigit():
        conn_val = int(aria2_connections.strip())
        if 1 <= conn_val <= 64:
            await set_dynamic_setting("aria2_connections", str(conn_val))
            messages.append("aria2 下载线程数已更新")
        else:
            return JSONResponse({"success": False, "message": "aria2 线程数范围为 1-64"})

    # Save public base URL
    if public_base_url is not None:
        url_val = public_base_url.strip().rstrip("/")
        await set_dynamic_setting("public_base_url", url_val)
        messages.append("公开访问地址已更新")

    msg = "; ".join(messages) if messages else "设置已保存"
    return JSONResponse({"success": True, "message": msg})


@router.post("/settings/reset-token")
async def settings_reset_token():
    """Generate a new random API token."""
    from config import set_dynamic_setting, API_TOKEN as CFG_TOKEN
    new_token = secrets.token_hex(16)
    await set_dynamic_setting("api_token", new_token)
    return JSONResponse({"success": True, "token": new_token})


@router.post("/settings/logout-all")
async def settings_logout_all():
    """Logout all web sessions."""
    from utils.middleware import _web_sessions
    _web_sessions.clear()
    return JSONResponse({"success": True, "message": "所有 Web 会话已清除"})


@router.post("/settings/factory-reset")
async def settings_factory_reset():
    """Reset all dynamic settings to defaults."""
    from config import DYNAMIC_SETTINGS, set_dynamic_setting
    defaults = {
        "web_username": "admin",
        "ffmpeg_path": "ffmpeg",
        "default_quality": "1080p",
        "cookie_refresh_interval": "24",
        "log_retention_days": "30",
        "api_token": "thechwinlyu",
        "api_port": "14515",
        "web_port": "8080",
        "enable_web_auth": "1",
    }
    for key, val in defaults.items():
        await set_dynamic_setting(key, val)
    return JSONResponse({"success": True, "message": "已恢复出厂设置，服务需要重启生效"})


# ============================================================
#  BILIBILI AUTH (accessible from login flow)
# ============================================================

@router.get("/qr-login", response_class=HTMLResponse)
async def web_qr_login_page(request: Request):
    """QR code login page. Skips if already logged into bilibili."""
    await refresh_user_info()
    cookies = get_current_cookies() or load_cookies()
    
    # If already logged into bilibili, straight to dashboard
    if cookies and load_cookies():
        await refresh_user_info()
        user = get_user_info()
        if user.get("is_login"):
            return RedirectResponse(url="/dashboard")
    
    qr_data = await generate_qrcode()
    if "error" in qr_data:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": qr_data["error"],
            "qr_img": None,
            "qr_key": None,
            "user": None,
        })

    # Generate QR code image
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(qr_data["url"])
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": None,
        "qr_img": f"data:image/png;base64,{qr_b64}",
        "qr_key": qr_data["qrcode_key"],
        "user": None,
    })


@router.get("/poll-login")
async def poll_login(qrcode_key: str = Query(...)):
    """AJAX poll for QR login status."""
    result = await poll_qrcode(qrcode_key)
    return JSONResponse(result)


@router.post("/refresh-cookie")
async def web_refresh_cookie():
    """Manual cookie refresh."""
    result = await refresh_cookies()
    return JSONResponse(result)
