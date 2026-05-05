"""
VRChat BPlayer Proxy API endpoints.
M3U8/HLS-only streaming with automatic codec adaptation.
"""

import subprocess
import asyncio
import os
import shutil
import uuid
import time
import logging
import tempfile
import json
from typing import Optional, Dict
from urllib.parse import quote

from fastapi import APIRouter, Request, Query, HTTPException
from fastapi.responses import JSONResponse, Response, RedirectResponse

from config import (
    BILIBILI_UA,
    BILIBILI_REFERER,
    FFMPEG_PATH,
    API_TOKEN,
    BASE_DIR,
)
from utils.geo import lookup_ip
from utils.codec_adapter import (
    detect_encoders,
    select_encoder,
    build_hls_command,
    source_codec_name,
    CODECID_AVC,
    CODECID_HEVC,
    CODECID_AV1,
    QUALITY_MAP as STD_QUALITY_MAP,
    QUALITY_LABELS,
    STANDARD_QUALITIES,
    get_qx_resolution,
    get_downscale_qx,
    needs_downscale,
    EncoderInfo,
)
from bilibili.video import (
    get_play_url_comprehensive,
    get_video_info,
    get_video_pages,
    get_season_info,
    get_episode_info,
)
from bilibili.auth import get_current_cookies, load_cookies, _cookie_expiry_estimate
from db.models import insert_log
from utils.downloader import (
    download_dash_sync,
    get_cached_paths,
    get_cache_stats,
    cleanup_expired_cache as _cleanup_expired_dash_cache,
    delete_cache_entry,
    download_dash_background,
    get_download_progress,
    cleanup_old_progress,
)
from utils.pipeline import (
    run_pipeline,
    get_pipeline_status,
    get_all_tasks,
    list_aliwh1_videos,
    delete_aliwh1_video,
    _load_task_db,
    _save_task_db,
    _set_progress,
)

logger = logging.getLogger("bilibili-proxy.api")

router = APIRouter()

# Cache available encoders at startup
_available_encoders: list[EncoderInfo] = []
_best_encoder: Optional[EncoderInfo] = None


def init_codec():
    """Probe FFmpeg encoders at startup (called once)."""
    global _available_encoders, _best_encoder
    _available_encoders = detect_encoders()
    _best_encoder = select_encoder(_available_encoders)
    if _best_encoder:
        logger.info(
            "Using encoder: %s (hwaccel=%s)",
            _best_encoder.encoder, _best_encoder.hwaccel or "none",
        )
    else:
        logger.warning("No H.264 encoder found! Check FFmpeg installation.")


# ============================================================
#  QUALITY ALIASES (custom qx → standard qx mapping)
# ============================================================

_QUALITY_ALIASES_FILE = os.path.join(BASE_DIR, "data", "quality_aliases.json")
_quality_aliases: dict[str, str] = {}  # custom name → standard qx (e.g. "1k" → "1080p")


def _load_quality_aliases():
    """Load custom quality aliases from JSON file."""
    global _quality_aliases
    try:
        if os.path.exists(_QUALITY_ALIASES_FILE):
            with open(_QUALITY_ALIASES_FILE) as f:
                _quality_aliases = json.load(f)
            logger.info("Loaded %d quality aliases", len(_quality_aliases))
    except Exception as e:
        logger.warning("Failed to load quality aliases: %s", e)
        _quality_aliases = {}


def _save_quality_aliases():
    """Save quality aliases to JSON file."""
    try:
        os.makedirs(os.path.dirname(_QUALITY_ALIASES_FILE), exist_ok=True)
        with open(_QUALITY_ALIASES_FILE, "w") as f:
            json.dump(_quality_aliases, f, indent=2)
    except Exception as e:
        logger.error("Failed to save quality aliases: %s", e)


def _resolve_qx(qx: str) -> str:
    """Resolve custom qx alias to standard quality name."""
    if qx in STD_QUALITY_MAP:
        return qx
    return _quality_aliases.get(qx, qx)


# ============================================================
#  TRANSCODING SETTINGS
# ============================================================

_TRANSCODE_SETTINGS_FILE = os.path.join(BASE_DIR, "data", "transcode_settings.json")
_transcode_settings: dict = {}  # { "blocked_qualities": [...], "max_output_resolution": "1080" }


def _load_transcode_settings():
    """Load transcoding settings from JSON file."""
    global _transcode_settings
    try:
        if os.path.exists(_TRANSCODE_SETTINGS_FILE):
            with open(_TRANSCODE_SETTINGS_FILE) as f:
                _transcode_settings = json.load(f)
        logger.info("Loaded transcode settings: %s", _transcode_settings)
    except Exception as e:
        logger.warning("Failed to load transcode settings: %s", e)
        _transcode_settings = {}


def _save_transcode_settings():
    """Save transcoding settings to JSON file."""
    try:
        os.makedirs(os.path.dirname(_TRANSCODE_SETTINGS_FILE), exist_ok=True)
        with open(_TRANSCODE_SETTINGS_FILE, "w") as f:
            json.dump(_transcode_settings, f, indent=2)
    except Exception as e:
        logger.error("Failed to save transcode settings: %s", e)


def set_transcode_setting(key: str, value) -> None:
    """Set a transcoding setting at runtime and persist to file.
    
    Callable from web panel to update settings without restart.
    """
    global _transcode_settings
    _transcode_settings[key] = value
    _save_transcode_settings()


def _get_blocked_qualities() -> set[str]:
    """Return set of quality names that are blocked from direct streaming."""
    raw = _transcode_settings.get("blocked_qualities", [])
    return set(raw)


def _get_max_output_height() -> Optional[int]:
    """Return max output resolution height, or None if no cap."""
    raw = _transcode_settings.get("max_output_resolution", "")
    if raw and raw.isdigit():
        return int(raw)
    return None


def _check_qx_allowed(qx: str) -> Optional[str]:
    """
    Check if a resolved qx is allowed. If blocked, return the closest allowed qx.
    Returns None if the qx itself is allowed, or a fallback qx if blocked.
    """
    blocked = _get_blocked_qualities()
    if qx not in blocked:
        return None  # allowed

    # Walk fallback order (lowest to highest) and find first non-blocked
    fallback_order = ["360p", "480p", "720p", "1080p", "1080p60", "4k"]
    for fb in fallback_order:
        if fb not in blocked and fb in STD_QUALITY_MAP:
            return fb
    return "360p"  # ultimate fallback


# ============================================================
#  HLS STREAM TRACKING
# ============================================================

_active_hls: Dict[str, dict] = {}
_stream_status: Dict[str, dict] = {}
_status_lock = asyncio.Lock()


async def _set_stream_status(stream_id: str, status: str, progress_pct: float = 0, eta_seconds: float = 0):
    async with _status_lock:
        _stream_status[stream_id] = {
            "status": status,
            "progress_pct": progress_pct,
            "eta_seconds": eta_seconds,
            "updated_at": time.time(),
        }


async def _cleanup_stream_status(stream_id: str):
    async with _status_lock:
        _stream_status.pop(stream_id, None)


async def _cleanup_stale_hls():
    """Background: remove HLS streams older than 10 minutes + expired DASH cache."""
    _last_cache_cleanup = 0
    while True:
        await asyncio.sleep(60)
        now = time.time()

        # Cleanup stale HLS streams
        stale_ids = [
            sid for sid, info in _active_hls.items()
            if now - info.get("started_at", 0) > 600
        ]
        for sid in stale_ids:
            info = _active_hls.pop(sid, None)
            if info:
                proc = info.get("process")
                if proc and proc.poll() is None:
                    proc.kill()
                shutil.rmtree(info["tmp_dir"], ignore_errors=True)
                await _cleanup_stream_status(sid)
                logger.info("Cleaned up stale HLS stream: %s", sid)

        # Cleanup expired DASH cache (once per hour)
        if now - _last_cache_cleanup > 3600:
            _cleanup_expired_dash_cache()
            _last_cache_cleanup = now


async def _log_call(request: Request, bvid: str, qx: str, qn: int, codecid: int):
    """Log API call asynchronously."""
    try:
        caller_ip = request.client.host if request.client else "unknown"
        geo = await lookup_ip(caller_ip)
        ua = request.headers.get("user-agent", "")
        await insert_log(caller_ip, geo, bvid, qx, qn, "", "", ua)
    except Exception:
        pass


# ============================================================
#  STREAM HELPERS
# ============================================================

def _build_avc_copy_command(
    video_url: str,
    audio_url: str,
    tmp_dir: str,
    cookie_str: str,
    seek_seconds: int = None,
    video_local_path: str = None,
    audio_local_path: str = None,
) -> list:
    """Fast-path FFmpeg command for AVC source with direct stream copy (no transcode).

    If video_local_path/audio_local_path are provided, uses local cached files
    instead of HTTP URLs — enabling ultra-fast multi-connection downloaded remux.

    Audio sync fixes:
    - -max_interleave_delta: enforces strict audio/video interleaving
    - -avoid_negative_ts make_zero: shifts all output timestamps to start at 0
    - aresample=async=1:first_pts=0: forces audio resample clock to start at 0
    """
    cmd = [
        FFMPEG_PATH, "-y", "-hide_banner",
        "-fflags", "+genpts+igndts+discardcorrupt",
        "-analyzeduration", "5M", "-probesize", "5M",
    ]
    if not video_local_path:
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    if seek_seconds and seek_seconds > 0:
        cmd += ["-ss", str(seek_seconds)]

    if video_local_path:
        # Use cached local file — no HTTP headers needed
        cmd += ["-i", video_local_path]
    else:
        headers_val = f"Referer: {BILIBILI_REFERER}\r\nUser-Agent: {BILIBILI_UA}\r\n"
        if cookie_str:
            headers_val += f"Cookie: {cookie_str}\r\n"
        cmd += ["-headers", headers_val, "-i", video_url]

    if audio_url or audio_local_path:
        if audio_local_path:
            cmd += ["-i", audio_local_path]
        else:
            audio_hdrs = f"Referer: {BILIBILI_REFERER}\r\nUser-Agent: {BILIBILI_UA}\r\n"
            if cookie_str:
                audio_hdrs += f"Cookie: {cookie_str}\r\n"
            cmd += ["-headers", audio_hdrs, "-i", audio_url]
        cmd += [
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:a", "aac", "-b:a", "128k",
            "-af", "aresample=async=1:first_pts=0",
        ]
    else:
        cmd += ["-map", "0:v:0"]

    cmd += [
        "-c:v", "copy",
        "-max_interleave_delta", "5000000",
        "-avoid_negative_ts", "make_zero",
        "-f", "hls",
        "-hls_init_time", "2", "-hls_time", "4",
        "-hls_list_size", "0",
        "-hls_playlist_type", "event",
        "-hls_flags", "independent_segments+delete_segments",
        "-hls_segment_filename", os.path.join(tmp_dir, "seg_%03d.ts"),
        os.path.join(tmp_dir, "playlist.m3u8"),
    ]
    return cmd


# ============================================================
#  /play — M3U8/HLS STREAMING
# ============================================================

@router.get("/play")
async def play(
    request: Request,
    bvid: Optional[str] = Query(None, description="Bilibili BV号"),
    qx: str = Query("1080p", description="清晰度: 360p/480p/720p/1080p/1080p60/4k, 或自定义别名"),
    cid: Optional[int] = Query(None, description="直接指定 cid"),
    t: Optional[int] = Query(None, description="起始时间(秒)"),
    page: Optional[int] = Query(None, description="分P编号(1-indexed)"),
    ep_id: Optional[str] = Query(None, description="番剧 ep_id (如 ep1482617)"),
    dis: str = Query("no", description="调试模式: yes=显示调试面板"),
    _token_valid: None = None,
):
    """
    Stream video as HLS (M3U8 + fMP4 segments).
    Returns 302 redirect to /segments/{id}/playlist.m3u8 when ready.

    - AVC source: direct stream copy (zero CPU)
    - HEVC/AV1 source: hardware transcode to H.264
    - Always outputs H.264 for maximum player compatibility
    """
    token = request.query_params.get("token", "")
    if token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token")

    cookies = get_current_cookies() or load_cookies()
    if not cookies:
        raise HTTPException(status_code=503, detail="未登录 Bilibili 账号")

    if not bvid and not ep_id:
        raise HTTPException(status_code=400, detail="需要提供 bvid 或 ep_id 参数")

    # Resolve quality alias
    resolved_qx = _resolve_qx(qx)

    # Check quality restrictions
    blocked_fallback = _check_qx_allowed(resolved_qx)
    if blocked_fallback:
        logger.info("Quality '%s' blocked, falling back to '%s'", resolved_qx, blocked_fallback)
        resolved_qx = blocked_fallback

    # Resolve ep_id → bvid+cid
    resolved_bvid = bvid
    resolved_cid = cid

    if ep_id and not bvid:
        clean_ep_id = ep_id[2:] if str(ep_id).lower().startswith("ep") else ep_id
        ep_data = await get_episode_info(clean_ep_id)
        if "error" in ep_data:
            raise HTTPException(status_code=400, detail=ep_data["error"])
        current = ep_data.get("current", {})
        if not current:
            raise HTTPException(status_code=400, detail="未找到该剧集")
        resolved_bvid = current.get("bvid", "")
        if not resolved_bvid and ep_data.get("episodes"):
            resolved_bvid = ep_data["episodes"][0].get("bvid", "")
        if not resolved_cid:
            resolved_cid = current.get("cid", 0)

    if page is not None and page > 0 and not resolved_cid:
        pages_data = await get_video_pages(resolved_bvid)
        if "error" in pages_data:
            raise HTTPException(status_code=400, detail=pages_data["error"])
        pages = pages_data.get("pages", [])
        if page > len(pages):
            raise HTTPException(status_code=400, detail=f"分P编号超出范围 (共 {len(pages)} 个分P)")
        resolved_cid = pages[page - 1]["cid"]

    # Get DASH URLs from Bilibili
    play_data = await get_play_url_comprehensive(
        bvid=resolved_bvid,
        cid=resolved_cid,
        qx=resolved_qx,
    )

    if "error" in play_data:
        await _log_call(request, resolved_bvid or "", qx, 0, 0)
        raise HTTPException(status_code=400, detail=play_data["error"])

    video_url = play_data.get("video_url", "")
    audio_url = play_data.get("audio_url", "")
    qn = play_data.get("actual_qn", 0)
    duration = play_data.get("duration", 0)
    title = play_data.get("title", "")
    source_codecid = play_data.get("codecid", 0)

    await _log_call(request, resolved_bvid or "", qx, qn, source_codecid)

    if not video_url:
        raise HTTPException(status_code=400, detail="未获取到视频地址")

    # Build cookie string for FFmpeg headers
    cookies = get_current_cookies() or load_cookies()
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items()) if cookies else ""

    # === DEBUG MODE ===
    if dis.lower() == "yes":
        return _build_debug_page(
            title=title,
            bvid=resolved_bvid or "",
            cid=resolved_cid or 0,
            duration=duration,
            qx=qx,
            qn=qn,
            codecid=source_codecid,
            token=token,
        )

    # === HLS STREAMING ===
    if not _best_encoder:
        raise HTTPException(status_code=503, detail="没有可用的 H.264 编码器，请检查 FFmpeg 安装")

    stream_id = uuid.uuid4().hex[:8]
    tmp_dir = os.path.join(tempfile.gettempdir(), f"hls_{stream_id}")
    os.makedirs(tmp_dir, exist_ok=True)

    # Check output resolution cap
    # Do NOT downgrade qx here - fetch the source at the requested quality
    # and let FFmpeg's scale filter handle downscaling if needed
    max_output_height = _get_max_output_height()
    if max_output_height and needs_downscale(resolved_qx, max_output_height):
        logger.info(
            "Resolution cap %dp: source=%s will be downscaled by FFmpeg",
            max_output_height, resolved_qx,
        )

    # === CHECK LOCAL DASH CACHE ===
    # If DASH files are cached locally (from preload or previous play), use them
    # for ultra-fast FFmpeg remux (avoids CDN download bottleneck).
    local_video_path: Optional[str] = None
    local_audio_path: Optional[str] = None

    cached = get_cached_paths(resolved_bvid or "", source_codecid, qn)
    if cached:
        local_video_path = cached["video_path"]
        local_audio_path = cached.get("audio_path")
        logger.info(
            "Using CACHED DASH files for %s (%.1f MB) — ultra-fast remux",
            resolved_bvid, cached.get("size_bytes", 0) / 1024 / 1024,
        )

    # Build FFmpeg command
    if source_codecid == CODECID_AVC and not (max_output_height and needs_downscale(resolved_qx, max_output_height)):
        # AVC + no downscale = direct copy (fast path)
        ff_cmd = _build_avc_copy_command(
            video_url, audio_url, tmp_dir, cookie_str, t,
            video_local_path=local_video_path,
            audio_local_path=local_audio_path,
        )
    else:
        ff_cmd = build_hls_command(
            ffmpeg_path=FFMPEG_PATH,
            video_url=video_url,
            audio_url=audio_url,
            source_codecid=source_codecid,
            tmp_dir=tmp_dir,
            encoder=_best_encoder,
            seek_seconds=t if t and t > 0 else None,
            user_agent=BILIBILI_UA,
            referer=BILIBILI_REFERER,
            cookies=cookie_str,
            max_output_height=max_output_height,
        )

    logger.info(
        "HLS stream %s: codec=%s(%s), encoder=%s, qx=%s, qn=%d",
        stream_id,
        source_codec_name(source_codecid), source_codecid,
        _best_encoder.name, resolved_qx, qn,
    )

    # Log stderr to a file for debugging
    stderr_path = os.path.join(tmp_dir, "ffmpeg_stderr.log")
    stderr_file = open(stderr_path, "w")

    ffmpeg_proc = subprocess.Popen(
        ff_cmd,
        stdout=subprocess.PIPE,
        stderr=stderr_file,
    )

    playlist_path = os.path.join(tmp_dir, "playlist.m3u8")

    # Register stream immediately so segment serving works
    _active_hls[stream_id] = {
        "tmp_dir": tmp_dir,
        "playlist_path": playlist_path,
        "process": ffmpeg_proc,
        "started_at": time.time(),
        "duration": duration,
        "codecid": source_codecid,
        "title": title,
        "resolved_qx": resolved_qx,
        "actual_qn": qn,
    }

    await _set_stream_status(stream_id, "processing", 0, duration or 30)

    # Background monitoring
    asyncio.create_task(_monitor_hls(stream_id, ffmpeg_proc, playlist_path, duration))

    # Wait for first segment, checking for early FFmpeg exit every iteration
    start_wait = time.time()
    first_segment_ready = False
    while time.time() - start_wait < 4:
        await asyncio.sleep(0.2)
        # Check if FFmpeg exited early
        if ffmpeg_proc.poll() is not None:
            stderr_file.close()
            with open(stderr_path) as f:
                err_detail = f.read()[-2000:]
            logger.error("FFmpeg exited early (code=%d): %s", ffmpeg_proc.returncode, err_detail[:500])
            _active_hls.pop(stream_id, None)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            await _cleanup_stream_status(stream_id)
            raise HTTPException(status_code=500, detail=f"FFmpeg 处理失败 (exit={ffmpeg_proc.returncode})")
        # Check playlist
        if os.path.exists(playlist_path):
            with open(playlist_path) as f:
                content = f.read()
            if "#EXTINF" in content or "seg_0" in content:
                first_segment_ready = True
                break

    if not first_segment_ready:
        # Give a final chance
        await asyncio.sleep(2)
        if os.path.exists(playlist_path):
            with open(playlist_path) as f:
                content = f.read()
            if "seg_0" in content or "#EXTINF" in content:
                first_segment_ready = True

    if not first_segment_ready:
        stderr_file.close()
        with open(stderr_path) as f:
            err_detail = f.read()[-2000:]
        logger.error("HLS timeout - FFmpeg stuck. Stderr: %s", err_detail[:500])
        ffmpeg_proc.kill()
        ffmpeg_proc.wait()
        _active_hls.pop(stream_id, None)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        await _cleanup_stream_status(stream_id)
        raise HTTPException(status_code=500, detail="HLS 编码启动超时")
    else:
        stderr_file.close()

    # Determine wait strategy: always prefer VOD (complete playlist with ENDLIST)
    # for proper duration display and precise seeking in players.
    #
    # Estimated encoding speeds on RX 7900 XTX:
    #   AVC copy:   ~80x realtime → 1-hour video completes in ~45s
    #   GPU transcode: ~3x realtime → 1-hour video completes in ~20min
    is_copy_mode = source_codecid == CODECID_AVC and not (max_output_height and needs_downscale(resolved_qx, max_output_height))

    # Detect client type (for response format decision only)
    user_agent = request.headers.get("user-agent", "").lower()
    media_players = ["potplayer", "vlc", "mpv", "kodi", "infuse", "nplayer", "realplayer", "gopro", "bilibili", "dlna",
                     "dart", "lavf", "ffmpeg", "libmpv", "mplayer", "stagefright"]
    is_media_player = any(p in user_agent for p in media_players)
    browser_strings = ["mozilla", "chrome", "safari", "firefox", "edge", "opera"]
    is_browser = any(b in user_agent for b in browser_strings) and not is_media_player

    # ?raw=1 forces redirect to m3u8 (for players that misidentify as browsers)
    force_raw = request.query_params.get("raw", "0") == "1"

    if is_copy_mode:
        # AVC copy speed ~50-80x realtime (depends on GOP size, CDN speed).
        # Conservative estimate: duration/50 + 25 → 95s for 1-hour video.
        max_wait = min(int(duration / 50) + 25, 180)
        max_wait = max(15, max_wait)
    else:
        # Transcode is slower — up to 5 min, fall back to EVENT if not done
        max_wait = min(int(duration / 3) + 30, 300)
        max_wait = max(30, max_wait)

    logger.info(
        "Waiting up to %ds for FFmpeg completion (mode=%s, duration=%ds)...",
        max_wait, "copy" if is_copy_mode else "transcode", duration,
    )
    wait_start = time.time()
    ffmpeg_completed = False
    while time.time() - wait_start < max_wait:
        await asyncio.sleep(0.5)
        if ffmpeg_proc.poll() is not None:
            ffmpeg_completed = True
            break

    if ffmpeg_completed:
        logger.info("FFmpeg completed in %.1fs → VOD mode (stream=%s)", time.time() - wait_start, stream_id)
        await _monitor_hls(stream_id, ffmpeg_proc, playlist_path, duration)
    else:
        logger.info("FFmpeg still encoding after %ds → EVENT mode (stream=%s)", max_wait, stream_id)

    # Build redirect URL
    redirect_url = f"/segments/{stream_id}/playlist.m3u8"

    response_headers = {
        "Access-Control-Allow-Origin": "*",
        "X-Stream-Id": stream_id,
        "X-Content-Duration": str(float(duration)) if duration > 0 else "0",
    }
    if title:
        response_headers["X-Content-Title"] = quote(title, safe="")

    if is_browser and not force_raw:
        # Return HTML player page for browsers
        return _build_stream_player_page(
            title=title,
            stream_url=redirect_url,
            duration=duration,
            stream_id=stream_id,
        )
    else:
        # Return redirect for non-browser clients (VLC, mpv, etc.)
        return RedirectResponse(
            url=redirect_url,
            status_code=302,
            headers=response_headers,
        )


async def _monitor_hls(stream_id: str, proc: subprocess.Popen, playlist_path: str, duration: float):
    """Background: update stream status, add ENDLIST when FFmpeg completes.

    FFmpeg 8.1 HLS muxer no longer auto-appends ENDLIST for EVENT mode,
    so we add it manually after the process exits."""
    try:
        while True:
            await asyncio.sleep(1)
            if os.path.exists(playlist_path):
                with open(playlist_path) as f:
                    content = f.read()
                seg_count = content.count("seg_")
                if duration > 0:
                    seg_dur = 4  # default segment duration
                    progress = min(seg_count * seg_dur / duration * 100, 99)
                    eta = max(0, duration - seg_count * seg_dur)
                    await _set_stream_status(stream_id, "processing", progress, eta)

            if proc.poll() is not None:
                await _set_stream_status(stream_id, "completed", 100, 0)
                # FFmpeg 8.1 does not add ENDLIST to EVENT playlists on exit.
                # Append it manually so players get proper VOD duration/seeking.
                info = _active_hls.get(stream_id, {})
                if not info.get("_endlist_appended") and os.path.exists(playlist_path):
                    with open(playlist_path, "r+") as f:
                        content = f.read()
                        if "#EXT-X-ENDLIST" not in content:
                            if not content.endswith("\n"):
                                f.write("\n")
                            f.write("#EXT-X-ENDLIST\n")
                            logger.info("HLS stream %s: appended ENDLIST", stream_id)
                    if stream_id in _active_hls:
                        _active_hls[stream_id]["_endlist_appended"] = True
                if proc.returncode != 0:
                    logger.warning("HLS stream %s FFmpeg exit code %d", stream_id, proc.returncode)
                break
    except Exception as e:
        logger.error("HLS monitor error for %s: %s", stream_id, e)


# ============================================================
#  SEGMENT SERVING
# ============================================================

@router.get("/segments/{stream_id}/playlist.m3u8")
async def serve_hls_playlist(request: Request, stream_id: str):
    """
    Serve the live-updating HLS playlist with absolute URLs for maximum compatibility.
    """
    info = _active_hls.get(stream_id)
    if not info:
        raise HTTPException(status_code=404, detail="Stream not found")

    playlist_path = info["playlist_path"]
    if not os.path.exists(playlist_path):
        raise HTTPException(status_code=404, detail="Playlist not found")

    with open(playlist_path) as f:
        content = f.read()

    # Rewrite relative segment paths to absolute URLs for player compatibility
    # e.g. "seg_000.ts" → "http://host:port/segments/{stream_id}/seg_000.ts"
    base_url = str(request.base_url).rstrip("/")
    seg_base = f"{base_url}/segments/{stream_id}"
    
    # Replace relative segment URLs with absolute ones
    lines = content.split("\n")
    new_lines = []
    for line in lines:
        line_stripped = line.strip()
        # If line is a segment filename (starts with seg_ and ends with .ts)
        if line_stripped.startswith("seg_") and line_stripped.endswith(".ts") and not line_stripped.startswith("http"):
            line = line.replace(line_stripped, f"{seg_base}/{line_stripped}")
        new_lines.append(line)
    content = "\n".join(new_lines)

    return Response(
        content,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "max-age=2",
        },
    )


@router.get("/segments/{stream_id}/{filename}")
async def serve_segment(stream_id: str, filename: str):
    """Serve HLS .ts segment files."""
    info = _active_hls.get(stream_id)
    if not info:
        raise HTTPException(status_code=404, detail="Stream not found or expired")

    seg_path = os.path.join(info["tmp_dir"], filename)
    if not os.path.exists(seg_path):
        raise HTTPException(status_code=404, detail="Segment not found")

    mime = "video/mp2t" if filename.endswith(".ts") else "application/octet-stream"
    with open(seg_path, "rb") as f:
        data = f.read()

    return Response(
        content=data,
        media_type=mime,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "max-age=30",
        },
    )


# ============================================================
#  QUALITY ALIAS API
# ============================================================

@router.get("/api/quality-aliases")
async def get_quality_aliases():
    """List all custom quality aliases."""
    return JSONResponse({
        "aliases": dict(_quality_aliases),
        "standard_qualities": {k: str(v) for k, v in STD_QUALITY_MAP.items()},
    })


@router.post("/api/quality-aliases")
async def set_quality_alias(request: Request):
    """Set a quality alias. Body: {"alias": "1k", "target": "1080p"}"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    alias = body.get("alias", "").strip()
    target = body.get("target", "").strip()

    if not alias or not target:
        raise HTTPException(status_code=400, detail="需要提供 alias 和 target")
    if alias in STD_QUALITY_MAP:
        raise HTTPException(status_code=400, detail=f"'{alias}' 是标准清晰度名称，不能覆盖")
    if target not in STD_QUALITY_MAP:
        raise HTTPException(status_code=400, detail=f"目标 '{target}' 不是有效的标准清晰度")

    _quality_aliases[alias] = target
    _save_quality_aliases()
    logger.info("Quality alias set: %s → %s", alias, target)
    return JSONResponse({"ok": True, "alias": alias, "resolves_to": target})


@router.delete("/api/quality-aliases")
async def delete_quality_alias(request: Request):
    """Delete a quality alias. Body: {"alias": "1k"}"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    alias = body.get("alias", "").strip()
    if not alias:
        raise HTTPException(status_code=400, detail="需要提供 alias")

    removed = _quality_aliases.pop(alias, None)
    if removed:
        _save_quality_aliases()
        logger.info("Quality alias removed: %s", alias)
        return JSONResponse({"ok": True, "alias": alias, "removed": True})
    return JSONResponse({"ok": True, "alias": alias, "removed": False})


# ============================================================
#  TRANSCODING SETTINGS API
# ============================================================

@router.get("/api/transcode-settings")
async def get_transcode_settings():
    """Get current transcoding settings."""
    return JSONResponse({
        "blocked_qualities": _transcode_settings.get("blocked_qualities", []),
        "max_output_resolution": _transcode_settings.get("max_output_resolution", ""),
        "available_qualities": sorted(STANDARD_QUALITIES),
    })


@router.post("/api/transcode-settings")
async def set_transcode_settings(request: Request):
    """Update transcoding settings.
    Body: {
        "blocked_qualities": ["4k", "1080p60"],
        "max_output_resolution": "1080"
    }
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if "blocked_qualities" in body:
        blocked = body["blocked_qualities"]
        if not isinstance(blocked, list):
            raise HTTPException(status_code=400, detail="blocked_qualities must be a list")
        for q in blocked:
            if q not in STANDARD_QUALITIES:
                raise HTTPException(status_code=400, detail=f"'{q}' is not a valid quality name")
        _transcode_settings["blocked_qualities"] = blocked

    if "max_output_resolution" in body:
        val = str(body["max_output_resolution"]).strip()
        if val and not val.isdigit():
            raise HTTPException(status_code=400, detail="max_output_resolution must be a number (height in pixels) or empty")
        _transcode_settings["max_output_resolution"] = val

    _save_transcode_settings()
    logger.info("Transcode settings updated: %s", _transcode_settings)
    return JSONResponse({
        "ok": True,
        "blocked_qualities": _transcode_settings.get("blocked_qualities", []),
        "max_output_resolution": _transcode_settings.get("max_output_resolution", ""),
    })


# ============================================================
#  DEBUG PAGE
# ============================================================

def _build_debug_page(title: str, bvid: str, cid: int, duration: int, qx: str, qn: int, codecid: int, token: str) -> Response:
    """HTML debug page with hls.js player."""
    stream_url = f"/play?bvid={bvid}&token={token}&qx={qx}"
    if cid:
        stream_url += f"&cid={cid}"

    codec_badge = ""
    transcode_note = ""
    if codecid == CODECID_AVC:
        codec_badge = "<span class='badge badge-ok'>AVC/H.264 — 直接复制</span>"
        transcode_note = "源流已是 H.264，FFmpeg 直接 remux，零 CPU 负载"
    elif codecid == CODECID_HEVC:
        codec_badge = "<span class='badge badge-warn'>HEVC/H.265 — GPU 转码</span>"
        transcode_note = f"源流是 HEVC，通过 {_best_encoder.description if _best_encoder else 'N/A'} 实时转码为 H.264"
    elif codecid == CODECID_AV1:
        codec_badge = "<span class='badge badge-warn'>AV1 — GPU 转码</span>"
        transcode_note = f"源流是 AV1，通过 {_best_encoder.description if _best_encoder else 'N/A'} 实时转码为 H.264"

    html = f"""<!DOCTYPE html>
<html lang="zh">
<head><meta charset="UTF-8"><title>BPlayer Debug - {title}</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#0d1117;color:#c9d1d9;padding:16px}}
h2{{color:#58a6ff;margin:16px 0 8px}}
.player{{background:#161b22;border-radius:12px;padding:16px;margin:16px 0}}
.player video{{width:100%;max-height:60vh;border-radius:8px;background:#000}}
.info{{display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:13px}}
.info div{{background:#161b22;padding:10px 14px;border-radius:8px}}
.label{{color:#8b949e}} .value{{color:#c9d1d9;font-weight:500}}
.badge{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:500}}
.badge-ok{{background:#1b5e20;color:#81c784}}
.badge-warn{{background:#4a3a2a;color:#ff9800}}
.url-box{{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:12px;font-family:monospace;font-size:12px;word-break:break-all;color:#7ee787;margin:8px 0}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin:8px 0}}
th{{background:#21262d;color:#8b949e;padding:8px 12px;text-align:left;font-weight:500}}
td{{padding:8px 12px;border-bottom:1px solid #21262d}}
</style></head>
<body>
<h1 style="color:#fb7299">BPlayer Debug — HLS</h1>
<div class="player">
    <video id="video" controls autoplay muted preload="auto"></video>
    <p style="color:#8b949e;font-size:12px;margin-top:8px" id="status">初始化播放器...</p>
</div>
<script>
var video = document.getElementById('video');
var statusEl = document.getElementById('status');
if (Hls.isSupported()) {{
    var hls = new Hls();
    hls.loadSource('{stream_url}');
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, function() {{
        statusEl.textContent = 'HLS 加载成功，点击播放';
    }});
    hls.on(Hls.Events.ERROR, function(event, data) {{
        statusEl.textContent = 'HLS 错误: ' + data.type + ' - ' + data.details;
    }});
}} else if (video.canPlayType('application/vnd.apple.mpegurl')) {{
    video.src = '{stream_url}';
    statusEl.textContent = '原生 HLS 支持（Safari）';
}}
</script>
<div class="info">
    <div><span class="label">标题</span><br><span class="value">{title}</span></div>
    <div><span class="label">BV号</span><br><span class="value">{bvid}</span></div>
    <div><span class="label">时长</span><br><span class="value">{duration}s</span></div>
    <div><span class="label">清晰度</span><br><span class="value">{qx} (qn={qn})</span></div>
    <div><span class="label">输出</span><br><span class="value">HLS (M3U8 + fMP4)</span></div>
    <div><span class="label">源编码</span><br><span class="value">{codec_badge}</span></div>
    <div><span class="label">CID</span><br><span class="value">{cid}</span></div>
</div>
<h2>转码信息</h2>
<p style="color:#8b949e;font-size:13px;line-height:1.6">{transcode_note}</p>
<h2>M3U8 播放地址</h2>
<div class="url-box">{stream_url}</div>
</body></html>"""
    return Response(html, media_type="text/html")


# ============================================================
#  INFO / PAGES / QUALITIES / SEASON / EPISODE
# ============================================================

@router.get("/info")
async def video_info(
    bvid: str = Query(..., description="Bilibili BV号"),
    cid: Optional[int] = Query(None, description="分P cid"),
    page: Optional[int] = Query(None, description="分P编号(1-indexed)"),
    _token_valid: None = None,
):
    info = await get_video_info(bvid, cid=cid)
    if "error" in info:
        return JSONResponse(info, status_code=400)
    return JSONResponse(info)


@router.get("/pages")
async def video_pages(
    bvid: str = Query(..., description="Bilibili BV号"),
    _token_valid: None = None,
):
    pages_data = await get_video_pages(bvid)
    if "error" in pages_data:
        return JSONResponse(pages_data, status_code=400)
    return JSONResponse(pages_data)


@router.get("/qualities")
async def video_qualities(
    bvid: str = Query(..., description="Bilibili BV号"),
    cid: Optional[int] = Query(None, description="分P cid"),
    _token_valid: None = None,
):
    play_data = await get_play_url_comprehensive(bvid=bvid, cid=cid, qx="1080p")
    if "error" in play_data:
        return JSONResponse(play_data, status_code=400)
    all_qns = play_data.get("all_available_qualities", [])
    from utils.codec_adapter import QUALITY_LABELS
    qualities = [{"qn": qn, "label": QUALITY_LABELS.get(qn, f"未知 ({qn})")} for qn in all_qns]
    return JSONResponse({"bvid": bvid, "cid": play_data.get("cid"), "qualities": qualities})


@router.get("/season")
async def season_info(
    season_id: str = Query(..., description="剧集 season_id"),
    _token_valid: None = None,
):
    data = await get_season_info(season_id)
    if "error" in data:
        return JSONResponse(data, status_code=400)
    return JSONResponse(data)


@router.get("/episode")
async def episode_info(
    ep_id: str = Query(..., description="剧集 ep_id"),
    _token_valid: None = None,
):
    clean_ep_id = ep_id[2:] if str(ep_id).lower().startswith("ep") else ep_id
    data = await get_episode_info(clean_ep_id)
    if "error" in data:
        return JSONResponse(data, status_code=400)
    return JSONResponse(data)


# ============================================================
#  VIDEO SUMMARY / STREAM STATUS / TOKEN INFO
# ============================================================

@router.get("/video-summary")
async def video_summary(
    request: Request,
    bvid: Optional[str] = Query(None, description="Bilibili BV号"),
    ep_id: Optional[str] = Query(None, description="番剧 ep_id"),
    _token_valid: None = None,
):
    token = request.query_params.get("token", "")
    if token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token")
    if not bvid and not ep_id:
        raise HTTPException(status_code=400, detail="需要提供 bvid 或 ep_id 参数")

    resolved_bvid = bvid
    if ep_id and not bvid:
        clean_ep_id = ep_id[2:] if str(ep_id).lower().startswith("ep") else ep_id
        ep_data = await get_episode_info(clean_ep_id)
        if "error" in ep_data:
            raise HTTPException(status_code=400, detail=ep_data["error"])
        current = ep_data.get("current", {})
        if not current:
            raise HTTPException(status_code=400, detail="未找到该剧集")
        resolved_bvid = current.get("bvid", "")
        if not resolved_bvid and ep_data.get("episodes"):
            resolved_bvid = ep_data["episodes"][0].get("bvid", "")
    if not resolved_bvid:
        raise HTTPException(status_code=400, detail="无法解析视频 ID")

    info = await get_video_info(resolved_bvid)
    if "error" in info:
        raise HTTPException(status_code=400, detail=info["error"])
    play_data = await get_play_url_comprehensive(bvid=resolved_bvid, qx="1080p")
    from utils.codec_adapter import QUALITY_LABELS
    all_qns = play_data.get("all_available_qualities", [])
    qualities = [{"qn": qn, "label": QUALITY_LABELS.get(qn, f"未知 ({qn})")} for qn in all_qns]
    return JSONResponse({
        "bvid": resolved_bvid,
        "title": info.get("title", ""),
        "cover": info.get("cover", ""),
        "duration": info.get("duration", 0),
        "owner": info.get("owner", {}),
        "pages": info.get("pages", []),
        "qualities": qualities,
    })


@router.get("/api/video-info")
async def api_video_info(
    request: Request,
    bvid: Optional[str] = Query(None),
    ep_id: Optional[str] = Query(None),
    ss_id: Optional[str] = Query(None),
):
    """
    Get video info with available qualities and estimated file sizes.
    Accepts bvid, ep_id, or ss_id.
    """
    token = request.query_params.get("token", "")
    if token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token")

    # Resolve ID
    resolved_bvid = bvid
    if not resolved_bvid and ep_id:
        clean_ep_id = str(ep_id)
        if clean_ep_id.lower().startswith("ep"):
            clean_ep_id = clean_ep_id[2:]
        ep_data = await get_episode_info(clean_ep_id)
        if "error" not in ep_data:
            current = ep_data.get("current", {})
            resolved_bvid = current.get("bvid", "")
            if not resolved_bvid and ep_data.get("episodes"):
                resolved_bvid = ep_data["episodes"][0].get("bvid", "")

    if not resolved_bvid and ss_id:
        clean_ss_id = str(ss_id)
        if clean_ss_id.lower().startswith("ss"):
            clean_ss_id = clean_ss_id[2:]
        try:
            season_data = await get_season_info(clean_ss_id)
            if "error" not in season_data:
                episodes = season_data.get("episodes", [])
                if episodes:
                    resolved_bvid = episodes[0].get("bvid", "")
                else:
                    raise HTTPException(status_code=400, detail="该剧集没有分集数据")
            else:
                raise HTTPException(status_code=400, detail=season_data.get("error", "获取剧集失败"))
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("SS resolution failed for %s: %s", clean_ss_id, e)
            raise HTTPException(status_code=400, detail=f"SS号查询失败: {e}")

    if not resolved_bvid:
        hint = ""
        if bvid: hint = f" (BV: {bvid})"
        elif ep_id: hint = f" (EP: {ep_id})"
        elif ss_id: hint = f" (SS: {ss_id})"
        raise HTTPException(status_code=400, detail=f"无法解析{hint}，请检查输入")

    # Get video info
    info = await get_video_info(resolved_bvid)
    if "error" in info:
        raise HTTPException(status_code=400, detail=info["error"])

    title = info.get("title", "")
    cover = info.get("cover", "")
    duration = info.get("duration", 0)

    # Get all quality options with bandwidth
    # Call B站 API with fnval=4048 to get all DASH streams
    import httpx
    from bilibili.wbi import sign_params
    from bilibili.auth import get_current_cookies, load_cookies
    cookies = get_current_cookies() or load_cookies()
    cid = info.get("cid", info.get("pages", [{}])[0].get("cid", 0)) if info.get("pages") else 0

    # Fetch DASH playurl with maximum fnval to get all streams
    params = await sign_params({"bvid": resolved_bvid, "cid": cid, "qn": 120, "fnval": 4048, "fnver": 0, "fourk": 1})
    qualities_info = []
    try:
        async with httpx.AsyncClient(timeout=10, cookies=cookies) as client:
            r = await client.get("https://api.bilibili.com/x/player/wbi/playurl", params=params,
                headers={"User-Agent": BILIBILI_UA, "Referer": BILIBILI_REFERER})
            d = r.json()
            dash = d.get("data", {}).get("dash", {})
            video_list = dash.get("video", [])
            accept_quality = d.get("data", {}).get("accept_quality", [])
            accept_description = d.get("data", {}).get("accept_description", [])

            # Map quality number to description
            qn_desc = {}
            for i, qn_val in enumerate(accept_quality):
                if i < len(accept_description):
                    qn_desc[qn_val] = accept_description[i]

            # Group by quality (height) and pick best bandwidth for each
            seen_heights = set()
            seen_qxs = set()
            for v in video_list:
                height = v.get("height", 0)
                if height in seen_heights:
                    continue
                seen_heights.add(height)
                bw = v.get("bandwidth", 0)
                codecid = v.get("codecid", 0)
                qn_val = v.get("id", 0)
                codec_names = {7: "AVC/H.264", 12: "HEVC/H.265", 13: "AV1"}

                # Map height to standard qx using ranges
                if height >= 2000:
                    qx = "4k"
                elif height >= 1080:
                    qx = "1080p"
                elif height >= 720:
                    qx = "720p"
                elif height >= 480:
                    qx = "480p"
                else:
                    qx = "360p"

                if qx in seen_qxs:
                    continue
                seen_qxs.add(qx)

                est_mb = round(bw * duration / 8 / 1024 / 1024, 1) if bw and duration else 0
                est_total_mb = round(est_mb * 1.05, 1)

                qualities_info.append({
                    "qx": qx,
                    "label": qn_desc.get(qn_val, f"{height}p"),
                    "height": height,
                    "codecid": codecid,
                    "codec_name": codec_names.get(codecid, f"未知({codecid})"),
                    "estimated_size_mb": est_total_mb,
                    "qn": qn_val,  # B站实际质量编号，用于请求
                })

            # Sort by height descending
            qualities_info.sort(key=lambda q: q.get("height", 0), reverse=True)
    except Exception:
        pass

    return JSONResponse({
        "bvid": resolved_bvid,
        "title": title,
        "cover": cover,
        "duration": duration,
        "qualities": qualities_info,
    })


@router.get("/api/stream-status/{stream_id}")
async def stream_status(stream_id: str):
    async with _status_lock:
        status = _stream_status.get(stream_id)
    if not status:
        return JSONResponse({"status": "not_found", "stream_id": stream_id}, status_code=404)
    return JSONResponse({
        "stream_id": stream_id,
        "status": status["status"],
        "progress_pct": status["progress_pct"],
        "eta_seconds": status["eta_seconds"],
    })


# ============================================================
#  PRELOAD & DASH CACHE API
# ============================================================

# Track background preload tasks
_preload_tasks: Dict[str, dict] = {}


@router.post("/api/preload")
async def preload_video(request: Request):
    """
    Start preloading a video's DASH streams in background.
    Returns task_id immediately. Poll /api/preload-status/{task_id} for progress.
    """
    token = request.query_params.get("token", "")
    if token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    bvid = body.get("bvid", "").strip()
    qn_param = body.get("qn")
    if qn_param is not None:
        qn_reverse = {120: "4k", 112: "1080p60", 64: "1080p", 32: "720p", 16: "480p", 8: "360p"}
        qx = qn_reverse.get(int(qn_param), "1080p")
    else:
        qx = body.get("qx", "1080p").strip()

    if not bvid:
        raise HTTPException(status_code=400, detail="需要提供 bvid")

    from config import QUALITY_MAP as QM
    qn = QM.get(qx, 64)

    # Get DASH URLs
    play_data = await get_play_url_comprehensive(bvid=bvid, qx=qx)
    if "error" in play_data:
        raise HTTPException(status_code=400, detail=play_data["error"])

    video_url = play_data.get("video_url", "")
    audio_url = play_data.get("audio_url", "")
    source_codecid = play_data.get("codecid", 0)
    actual_qn = play_data.get("actual_qn", qn)
    title = play_data.get("title", bvid)
    duration = play_data.get("duration", 0)

    if not video_url:
        raise HTTPException(status_code=400, detail="未获取到视频地址")

    # Check if already cached
    cached = get_cached_paths(bvid, source_codecid, qn)
    if cached:
        return JSONResponse({
            "ok": True,
            "cached": True,
            "bvid": bvid,
            "title": title,
            "duration": duration,
            "size_mb": round(cached.get("size_bytes", 0) / 1024 / 1024, 1),
            "message": "已缓存，可直接播放",
        })

    cookies = get_current_cookies() or load_cookies()
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items()) if cookies else ""

    # Start background download
    task_id = download_dash_background(
        bvid=bvid, codecid=source_codecid, qn=actual_qn,
        video_url=video_url, audio_url=audio_url,
        cookie_str=cookie_str, title=title, duration=duration,
    )

    # Register as pipeline task for persistent progress tracking
    qn_rev = {120:"4k",112:"1080p60",64:"1080p",32:"720p",16:"480p",8:"360p"}
    _set_progress(task_id, status="downloading", bvid=bvid,
                  qx=qn_rev.get(actual_qn, "1080p"), title=title,
                  stage="download", progress_pct=0, created_at=time.time(),
                  detail="预加载中...",
                  download_video_pct=0, download_video_speed="")

    return JSONResponse({
        "ok": True,
        "task_id": task_id,
        "bvid": bvid,
        "title": title,
        "duration": duration,
        "message": "后台下载已启动",
    })


@router.get("/api/preload-status/{task_id}")
async def preload_status(task_id: str):
    """Get progress of a background preload task."""
    progress = get_download_progress(task_id)
    # Sync to pipeline task system
    if progress.get("status") == "complete":
        _set_progress(task_id, status="done", stage="done", progress_pct=100,
                      detail=f"预加载完成 {progress.get('total_size',0)/1048576:.0f}MB")
    elif progress.get("status") == "error":
        _set_progress(task_id, status="error", detail=progress.get("error", "预加载失败"))
    else:
        pct = progress.get("progress_pct", 0)
        detail = f"预加载 {pct}%"
        if progress.get("downloaded") and progress.get("speed"):
            detail = f"预加载 {progress.get('downloaded')} {progress.get('speed')}"
        _set_progress(task_id, status="downloading", stage="download",
                      progress_pct=pct, detail=detail)
    return JSONResponse(progress)


# ============================================================
#  PUSH-TO-ALIWH1 PIPELINE (preload → transcode → upload)
# ============================================================

@router.post("/api/push-to-aliwh1")
async def push_to_aliwh1(request: Request):
    """
    One-click: preload DASH → GPU transcode HLS → upload to aliWH1.
    Returns task_id. Poll /api/push-status/{task_id} for progress.
    """
    token = request.query_params.get("token", "")
    if token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    bvid = body.get("bvid", "").strip()
    qn_param = body.get("qn")
    
    if not bvid:
        raise HTTPException(status_code=400, detail="需要提供 bvid")

    # Get DASH URLs - use qn directly if provided, otherwise fallback to qx
    if qn_param is not None:
        qn_target = int(qn_param)
        qn_reverse = {120: "4k", 112: "1080p60", 64: "1080p", 32: "720p", 16: "480p", 8: "360p"}
        qx_label = qn_reverse.get(qn_target, "1080p")
        play_data = await get_play_url_comprehensive(bvid=bvid, qx=qx_label, qn_override=qn_target)
    else:
        qx_label = body.get("qx", "1080p").strip()
        play_data = await get_play_url_comprehensive(bvid=bvid, qx=qx_label)
    if "error" in play_data:
        raise HTTPException(status_code=400, detail=play_data["error"])

    video_url = play_data.get("video_url", "")
    audio_url = play_data.get("audio_url", "")
    source_codecid = play_data.get("codecid", 0)
    title = play_data.get("title", bvid)
    duration = play_data.get("duration", 0)

    if not video_url:
        raise HTTPException(status_code=400, detail="未获取到视频地址")

    cookies = get_current_cookies() or load_cookies()
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items()) if cookies else ""

    # Fetch video info for cover
    cover = ""
    try:
        info = await get_video_info(bvid)
        cover = info.get("cover", "")
    except Exception:
        pass

    task_id = run_pipeline(
        bvid=bvid, qx=qx_label, video_url=video_url, audio_url=audio_url,
        codecid=source_codecid, qn=play_data.get("actual_qn", 64),
        duration=duration, title=title, cookie_str=cookie_str, cover=cover,
    )

    return JSONResponse({
        "ok": True, "task_id": task_id, "bvid": bvid,
        "title": title, "duration": duration, "qx": qx_label,
        "message": "Pipeline started: download -> transcode -> upload",
    })


@router.get("/api/push-status/{task_id}")
async def push_status(task_id: str):
    """Get progress of a push-to-aliwh1 pipeline task."""
    progress = get_pipeline_status(task_id)
    return JSONResponse(progress)


@router.post("/api/push-from-cache")
async def push_from_cache(request: Request):
    """Push using cached files only (skip download). Body: {"bvid":"...","qn":120}"""
    token = request.query_params.get("token", "")
    if token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    bvid = body.get("bvid", "").strip()
    qn_param = body.get("qn")
    if not bvid or not qn_param:
        raise HTTPException(status_code=400, detail="需要 bvid 和 qn")
    qn = int(qn_param)
    qn_rev = {120:"4k",112:"1080p60",64:"1080p",32:"720p",16:"480p",8:"360p"}
    qx_label = qn_rev.get(qn, "1080p")
    play_data = await get_play_url_comprehensive(bvid=bvid, qx=qx_label, qn_override=qn)
    codecid = play_data.get("codecid", 7)
    cached = get_cached_paths(bvid, codecid, qn)
    if not cached:
        raise HTTPException(status_code=400, detail="本地缓存未找到，请先预加载")
    # Get video info for cover
    cover = ""
    try:
        info = await get_video_info(bvid)
        cover = info.get("cover", "")
    except Exception:
        pass
    task_id = run_pipeline(
        bvid=bvid, qx=qx_label, video_url="", audio_url="",
        codecid=codecid, qn=qn, duration=play_data.get("duration",0),
        title=play_data.get("title",bvid),
        local_video=cached["video_path"],
        local_audio=cached.get("audio_path",""), cover=cover,
    )
    return JSONResponse({"ok":True,"task_id":task_id,"bvid":bvid,"qx":qx_label,
                         "message":"从缓存推送: transcode -> upload"})


@router.get("/api/tasks")
async def list_tasks(request: Request):
    """List all pipeline tasks (active + completed), survives browser close."""
    token = request.query_params.get("token", "")
    if token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token")
    tasks = get_all_tasks()
    return JSONResponse({"tasks": tasks})


@router.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str, request: Request):
    """Delete a task from history."""
    token = request.query_params.get("token", "")
    if token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token")
    try:
        db = _load_task_db()
        db.pop(task_id, None)
        _save_task_db(db)
        return JSONResponse({"ok": True})
    except Exception:
        return JSONResponse({"ok": False}, status_code=500)


@router.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request):
    """Cancel a running task and delete it."""
    token = request.query_params.get("token", "")
    if token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token")
    # Mark as cancelled
    _set_progress(task_id, status="cancelled", detail="已取消")
    db = _load_task_db()
    db.pop(task_id, None)
    _save_task_db(db)
    return JSONResponse({"ok": True})


@router.get("/api/aliwh1-list")
async def aliwh1_list(request: Request):
    """List videos currently on aliWH1 with metadata."""
    token = request.query_params.get("token", "")
    if token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token")
    videos = list_aliwh1_videos()
    return JSONResponse({"videos": videos})


@router.delete("/api/aliwh1-delete/{bvid}")
async def aliwh1_delete(bvid: str, request: Request):
    """Delete a video from aliWH1."""
    token = request.query_params.get("token", "")
    if token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token")
    ok = delete_aliwh1_video(bvid)
    if ok:
        return JSONResponse({"ok": True, "deleted": bvid})
    return JSONResponse({"ok": False, "error": "删除失败"}, status_code=500)


@router.get("/api/cache-stats")
async def cache_stats(request: Request):
    """Return DASH cache statistics (for Web panel display).
    Enhanced: fetches bilibili video info (title, cover, duration) for each cached bvid.
    """
    token = request.query_params.get("token", "")
    if token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token")

    stats = get_cache_stats()

    # Enrich entries with bilibili video info
    enriched_entries = []
    for entry in stats.get("entries", []):
        bvid = entry.get("bvid", "")
        if bvid:
            try:
                info = await get_video_info(bvid)
                if "error" not in info:
                    entry["title"] = info.get("title", "")
                    entry["cover_url"] = info.get("cover", "")
                    entry["duration"] = info.get("duration", 0)
                else:
                    entry["title"] = bvid
                    entry["cover_url"] = ""
                    entry["duration"] = 0
            except Exception:
                entry["title"] = bvid
                entry["cover_url"] = ""
                entry["duration"] = 0
        else:
            entry["title"] = ""
            entry["cover_url"] = ""
            entry["duration"] = 0

        # Map qn to label
        qn = entry.get("qn", 0)
        entry["qn_label"] = QUALITY_LABELS.get(qn, f"未知 ({qn})")
        enriched_entries.append(entry)

    stats["entries"] = enriched_entries
    return JSONResponse(stats)


@router.delete("/api/cache/{cache_key}")
async def delete_cache_endpoint(cache_key: str, request: Request):
    """Delete a specific cache entry by key."""
    token = request.query_params.get("token", "")
    if token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token")

    ok = delete_cache_entry(cache_key)
    if ok:
        return JSONResponse({"ok": True, "deleted": cache_key})
    else:
        return JSONResponse({"ok": False, "not_found": cache_key}, status_code=404)


@router.get("/api/aria2-settings")
async def get_aria2_settings():
    """Return current aria2 connections setting."""
    from config import ARIA2_CONNECTIONS
    return JSONResponse({"connections": ARIA2_CONNECTIONS})


@router.post("/api/aria2-settings")
async def set_aria2_settings(request: Request):
    """Update aria2 connections setting. Body: {"connections": 32}"""
    token = request.query_params.get("token", "")
    if token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    connections = body.get("connections")
    if not isinstance(connections, int) or connections < 1 or connections > 64:
        raise HTTPException(status_code=400, detail="connections must be an integer between 1 and 64")

    from config import set_dynamic_setting
    await set_dynamic_setting("aria2_connections", str(connections))
    return JSONResponse({"ok": True, "connections": connections})


@router.get("/api/token-info")
async def token_info():
    full = API_TOKEN
    masked = full[:4] + "****" + full[-4:] if len(full) > 8 else "****"
    return JSONResponse({"token": full, "masked": masked})


# ============================================================
#  HEALTH
# ============================================================

@router.get("/health")
async def health():
    cookies = get_current_cookies() or load_cookies()
    cookie_info = {"logged_in": bool(cookies), "expires_in": "unknown"}
    if cookies:
        cookie_info["expires_in"] = _cookie_expiry_estimate()

    system_info: dict = {
        "active_streams": len(_active_hls),
        "encoder": _best_encoder.description if _best_encoder else "none",
    }

    try:
        import psutil
        system_info["cpu_percent"] = round(psutil.cpu_percent(interval=0.1), 1)
        system_info["memory_percent"] = round(psutil.virtual_memory().percent, 1)
        system_info["disk_percent"] = round(psutil.disk_usage("/").percent, 1)
    except ImportError:
        try:
            with open("/proc/meminfo") as f:
                mem = f.read()
            total = int([l for l in mem.split("\n") if "MemTotal" in l][0].split()[1])
            available = int([l for l in mem.split("\n") if "MemAvailable" in l][0].split()[1])
            system_info["memory_percent"] = round((total - available) / total * 100, 1)
            with open("/proc/stat") as f:
                cpu_line = f.readline()
            cpu_parts = [int(x) for x in cpu_line.split()[1:]]
            cpu_idle = cpu_parts[3]
            cpu_total = sum(cpu_parts)
            system_info["cpu_percent"] = round((1 - cpu_idle / cpu_total) * 100, 1)
        except Exception:
            system_info["cpu_percent"] = -1
            system_info["memory_percent"] = -1
            system_info["disk_percent"] = -1

    try:
        with open("/proc/uptime") as f:
            system_info["uptime_seconds"] = int(float(f.readline().split()[0]))
    except Exception:
        system_info["uptime_seconds"] = -1

    return JSONResponse({
        "status": "ok",
        "bilibili_logged_in": bool(cookies),
        "service": "vrchat-bplayer-proxy",
        "cookie": cookie_info,
        "system": system_info,
    })


def _build_stream_player_page(title: str, stream_url: str, duration: float, stream_id: str) -> Response:
    """Build HTML player page with hls.js for browser playback.

    Features:
    - Real total duration from B站 API (not accumulated during playback)
    - Custom progress bar with click-to-seek
    - VOD mode (ENDLIST) support for precise seeking
    """
    duration_str = f"{int(duration // 60)}:{int(duration % 60):02d}" if duration > 0 else "未知"
    duration_js = str(int(duration)) if duration > 0 else "0"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: #0d1117; color: #c9d1d9; min-height: 100vh;
            display: flex; flex-direction: column; align-items: center;
        }}
        .player-container {{
            width: 100%; max-width: 1200px; padding: 20px;
        }}
        .video-wrapper {{
            background: #161b22; border-radius: 12px 12px 0 0; overflow: hidden;
            box-shadow: 0 8px 32px rgba(0,0,0,0.4);
            position: relative;
        }}
        video {{ width: 100%; max-height: 70vh; background: #000; display: block; }}
        .overlay {{
            position: absolute; top: 0; left: 0; right: 0; bottom: 0;
            display: flex; flex-direction: column; align-items: center; justify-content: center;
            background: rgba(0,0,0,0.5); cursor: pointer; z-index: 10;
        }}
        .overlay.hidden {{ display: none; }}
        .play-btn {{
            width: 80px; height: 80px; border-radius: 50%; background: rgba(251,114,153,0.9);
            display: flex; align-items: center; justify-content: center;
            font-size: 36px; color: white; transition: transform 0.2s;
        }}
        .play-btn:hover {{ transform: scale(1.1); }}
        .overlay-text {{ color: #fff; font-size: 14px; margin-top: 12px; opacity: 0.8; }}
        .info {{
            padding: 20px; background: #161b22; border-radius: 0 0 12px 12px;
        }}
        .title {{ font-size: 18px; font-weight: 600; color: #f0f6fc; margin-bottom: 8px; }}
        .meta {{ font-size: 14px; color: #8b949e; display: flex; gap: 16px; flex-wrap: wrap; }}
        .meta span {{ display: flex; align-items: center; gap: 4px; }}
        .status {{
            margin-top: 12px; padding: 12px; background: #0d1117;
            border-radius: 8px; font-size: 13px; color: #8b949e;
        }}
        .status.error {{ color: #f85149; }}
        .status.success {{ color: #3fb950; }}
        .copy-btn {{
            margin-top: 12px; padding: 8px 16px; background: #21262d;
            border: 1px solid #30363d; border-radius: 6px; color: #c9d1d9;
            cursor: pointer; font-size: 13px; transition: all 0.2s;
        }}
        .copy-btn:hover {{ background: #30363d; border-color: #8b949e; }}
        /* --- Custom seekable progress bar --- */
        .seek-bar-container {{ margin-top: 16px; cursor: pointer; user-select: none; }}
        .seek-bar-track {{
            height: 8px; background: #0d1117; border-radius: 4px; overflow: hidden;
            position: relative;
        }}
        .seek-bar-buffered {{
            position: absolute; top: 0; left: 0; height: 100%;
            background: #30363d; border-radius: 4px; transition: width 0.3s ease;
        }}
        .seek-bar-played {{
            position: absolute; top: 0; left: 0; height: 100%;
            background: linear-gradient(90deg, #fb7299, #ff8db3);
            border-radius: 4px; transition: width 0.1s linear;
        }}
        .seek-bar-hover {{
            position: absolute; top: 0; left: 0; height: 100%;
            background: rgba(255,255,255,0.15); border-radius: 4px; display: none;
        }}
        .seek-bar-thumb {{
            position: absolute; top: -5px; width: 18px; height: 18px;
            background: #fb7299; border-radius: 50%; border: 2px solid #fff;
            transform: translateX(-9px); display: none; z-index: 2;
        }}
        .seek-bar-container:hover .seek-bar-thumb {{ display: block; }}
        .seek-bar-container:hover .seek-bar-hover {{ display: block; }}
        .seek-bar-container.dragging .seek-bar-thumb {{ display: block; width: 22px; height: 22px; top: -7px; }}
        .seek-bar-labels {{
            display: flex; justify-content: space-between; font-size: 12px;
            color: #8b949e; margin-top: 4px;
        }}
    </style>
</head>
<body>
    <div class="player-container">
        <div class="video-wrapper">
            <video id="video" controls playsinline></video>
            <div class="overlay" id="overlay">
                <div class="play-btn" id="playBtn">▶</div>
                <div class="overlay-text">点击播放</div>
            </div>
        </div>
        <div class="info">
            <div class="title">{title}</div>
            <div class="meta">
                <span>⏱ 总时长: {duration_str}</span>
                <span id="loaded_info">缓冲: 计算中...</span>
                <span id="vod_status" style="color:#8b949e">◉ 加载中</span>
                <span>🆔 {stream_id}</span>
            </div>
            <div class="seek-bar-container" id="seekBarContainer">
                <div class="seek-bar-track" id="seekBarTrack">
                    <div class="seek-bar-buffered" id="seekBarBuffered"></div>
                    <div class="seek-bar-played" id="seekBarPlayed"></div>
                    <div class="seek-bar-hover" id="seekBarHover"></div>
                    <div class="seek-bar-thumb" id="seekBarThumb"></div>
                </div>
                <div class="seek-bar-labels">
                    <span id="currentTime">0:00</span>
                    <span id="totalTime">{duration_str}</span>
                </div>
            </div>
            <div class="status" id="status">正在初始化播放器...</div>
            <button class="copy-btn" onclick="copyUrl()">📋 复制播放地址</button>
        </div>
    </div>

    <script>
        const video = document.getElementById('video');
        const status = document.getElementById('status');
        const overlay = document.getElementById('overlay');
        const streamUrl = '{stream_url}';
        const realDuration = {duration_js};  // B站 API 真实总时长(秒)
        const loadedInfo = document.getElementById('loaded_info');
        const vodStatus = document.getElementById('vod_status');
        const totalTimeEl = document.getElementById('totalTime');
        const currentTimeEl = document.getElementById('currentTime');

        // Seek bar elements
        const seekBarContainer = document.getElementById('seekBarContainer');
        const seekBarTrack = document.getElementById('seekBarTrack');
        const seekBarBuffered = document.getElementById('seekBarBuffered');
        const seekBarPlayed = document.getElementById('seekBarPlayed');
        const seekBarHover = document.getElementById('seekBarHover');
        const seekBarThumb = document.getElementById('seekBarThumb');

        let isDragging = false;
        let knownDuration = realDuration;  // May be updated from HLS manifest

        function formatTime(s) {{
            s = Math.floor(s);
            var m = Math.floor(s / 60);
            var sec = s % 60;
            return m + ':' + (sec < 10 ? '0' : '') + sec;
        }}

        // Update buffered + played positions on seek bar
        function updateSeekBar() {{
            if (knownDuration <= 0) return;
            var buffered = video.buffered.length > 0 ? video.buffered.end(video.buffered.length - 1) : 0;
            var played = video.currentTime || 0;

            seekBarBuffered.style.width = Math.min(buffered / knownDuration * 100, 100) + '%';
            seekBarPlayed.style.width = Math.min(played / knownDuration * 100, 100) + '%';
            seekBarThumb.style.left = Math.min(played / knownDuration * 100, 100) + '%';
            currentTimeEl.textContent = formatTime(played);
            loadedInfo.textContent = '缓冲: ' + formatTime(buffered) + ' / ' + formatTime(knownDuration);
            totalTimeEl.textContent = formatTime(knownDuration);
        }}

        // Click-to-seek on seek bar
        function getSeekPercent(e) {{
            var rect = seekBarTrack.getBoundingClientRect();
            var x = e.clientX - rect.left;
            return Math.max(0, Math.min(1, x / rect.width));
        }}

        seekBarContainer.addEventListener('mousedown', function(e) {{
            isDragging = true;
            seekBarContainer.classList.add('dragging');
            var pct = getSeekPercent(e);
            var seekTime = pct * knownDuration;
            video.currentTime = seekTime;
            updateSeekBar();
        }});

        seekBarContainer.addEventListener('mousemove', function(e) {{
            var pct = getSeekPercent(e);
            seekBarHover.style.width = (pct * 100) + '%';
            if (isDragging) {{
                var seekTime = pct * knownDuration;
                video.currentTime = seekTime;
                updateSeekBar();
            }}
        }});

        seekBarContainer.addEventListener('mouseleave', function() {{
            seekBarHover.style.width = '0%';
            if (isDragging) {{
                isDragging = false;
                seekBarContainer.classList.remove('dragging');
            }}
        }});

        document.addEventListener('mouseup', function() {{
            if (isDragging) {{
                isDragging = false;
                seekBarContainer.classList.remove('dragging');
            }}
        }});

        // Touch support for mobile
        seekBarContainer.addEventListener('touchstart', function(e) {{
            isDragging = true;
            seekBarContainer.classList.add('dragging');
            var pct = getSeekPercent(e.touches[0]);
            var seekTime = pct * knownDuration;
            video.currentTime = seekTime;
            updateSeekBar();
            e.preventDefault();
        }});

        seekBarContainer.addEventListener('touchmove', function(e) {{
            if (isDragging) {{
                var pct = getSeekPercent(e.touches[0]);
                var seekTime = pct * knownDuration;
                video.currentTime = seekTime;
                updateSeekBar();
            }}
            e.preventDefault();
        }});

        seekBarContainer.addEventListener('touchend', function() {{
            isDragging = false;
            seekBarContainer.classList.remove('dragging');
        }});

        // Update seek bar on playback
        video.addEventListener('timeupdate', updateSeekBar);
        video.addEventListener('progress', updateSeekBar);
        video.addEventListener('durationchange', function() {{
            if (video.duration && isFinite(video.duration) && video.duration > 0) {{
                knownDuration = video.duration;
                totalTimeEl.textContent = formatTime(knownDuration);
            }}
        }});

        // Send seeking events to backend for analytics
        video.addEventListener('seeking', function() {{
            var pct = knownDuration > 0 ? (video.currentTime / knownDuration * 100) : 0;
            console.log('Seeking to: ' + formatTime(video.currentTime) + ' (' + Math.round(pct) + '%)');
        }});

        if (Hls.isSupported()) {{
            const hls = new Hls({{
                maxBufferLength: 30,
                maxMaxBufferLength: 60,
                enableWorker: true,
                lowLatencyMode: false,
                startLevel: 0,
                // Allow seeking even beyond loaded data
                maxFragLookUpTolerance: 0.5,
            }});

            hls.loadSource(streamUrl);
            hls.attachMedia(video);

            hls.on(Hls.Events.MANIFEST_PARSED, function(event, data) {{
                // Use manifest duration if available (VOD mode with ENDLIST)
                if (data.levels && data.levels[0] && data.levels[0].details) {{
                    var manifestDuration = data.levels[0].details.totalduration;
                    if (manifestDuration && manifestDuration > 0) {{
                        knownDuration = manifestDuration;
                    }}
                }}
                status.textContent = '✅ 流加载成功，点击播放按钮开始';
                status.className = 'status success';
            }});

            hls.on(Hls.Events.BUFFER_APPENDED, function() {{
                updateSeekBar();
            }});

            hls.on(Hls.Events.LEVEL_LOADED, function(event, data) {{
                if (data.details.live === false || data.details.endlist) {{
                    status.textContent = '✅ VOD 模式 — 完整视频可拖动进度条精准跳转';
                    status.className = 'status success';
                    vodStatus.innerHTML = '<span style="color:#3fb950">● VOD</span>';
                    // Update duration from manifest
                    if (data.details.totalduration && data.details.totalduration > 0) {{
                        knownDuration = data.details.totalduration;
                        totalTimeEl.textContent = formatTime(knownDuration);
                    }}
                }}
            }});

            hls.on(Hls.Events.FRAG_BUFFERED, function() {{
                updateSeekBar();
            }});

            hls.on(Hls.Events.ERROR, function(event, data) {{
                if (data.fatal) {{
                    switch (data.type) {{
                        case Hls.ErrorTypes.NETWORK_ERROR:
                            status.textContent = '⚠️ 网络错误，正在重连...';
                            status.className = 'status error';
                            hls.startLoad();
                            break;
                        case Hls.ErrorTypes.MEDIA_ERROR:
                            status.textContent = '⚠️ 媒体错误，正在恢复...';
                            status.className = 'status error';
                            hls.recoverMediaError();
                            break;
                        default:
                            status.textContent = '⚠️ 致命错误，无法恢复';
                            status.className = 'status error';
                            break;
                    }}
                }}
            }});

            video.addEventListener('play', function() {{
                overlay.classList.add('hidden');
                status.textContent = '▶️ 播放中';
            }});

            // Click overlay to start playback with audio
            overlay.addEventListener('click', function() {{
                overlay.classList.add('hidden');
                video.muted = false;
                video.play().catch(function(e) {{
                    status.textContent = '⚠️ 请手动点击播放: ' + e.message;
                }});
            }});
        }} else if (video.canPlayType('application/vnd.apple.mpegurl')) {{
            video.src = streamUrl;
            status.textContent = '✅ 原生 HLS 支持（Safari）';
            status.className = 'status success';
        }} else {{
            status.textContent = '❌ 浏览器不支持 HLS 播放';
            status.className = 'status error';
        }}

        function copyUrl() {{
            navigator.clipboard.writeText(window.location.href).then(function() {{
                alert('已复制播放地址！');
            }});
        }}

        // Initial update
        updateSeekBar();
        setInterval(updateSeekBar, 500);
    </script>
</body>
</html>"""

    return Response(html, media_type="text/html")
