"""
Multi-threaded DASH stream downloader using aria2c.
Provides 10-20x speedup over single-connection HTTP download by splitting
the file into 16 parallel connections.

Also manages a local cache with 12-hour TTL for preloaded videos.
"""

import os
import subprocess
import json
import time
import logging
import hashlib
import shutil
from typing import Optional

from config import ARIA2_CONNECTIONS

logger = logging.getLogger("bilibili-proxy.downloader")

ARIA2C_PATH = shutil.which("aria2c") or "aria2c"

# Cache directory for downloaded DASH streams
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "dash_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# Cache metadata file
CACHE_INDEX = os.path.join(os.path.dirname(CACHE_DIR), "dash_cache_index.json")

CACHE_TTL_SECONDS = 12 * 3600  # 12 hours


def _load_cache_index() -> dict:
    """Load the cache index from disk."""
    if os.path.exists(CACHE_INDEX):
        try:
            with open(CACHE_INDEX) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def _save_cache_index(index: dict):
    """Save the cache index to disk."""
    with open(CACHE_INDEX, "w") as f:
        json.dump(index, f, indent=2)


def _cache_key(bvid: str, codecid: int, qn: int) -> str:
    """Generate a unique cache key for a specific video+quality."""
    raw = f"{bvid}:{codecid}:{qn}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _entry_dir(cache_key: str) -> str:
    """Get the cache directory for a specific entry."""
    return os.path.join(CACHE_DIR, cache_key)


def get_cached_paths(bvid: str, codecid: int, qn: int) -> Optional[dict]:
    """
    Check if a video is cached and still valid.
    Returns dict with video_path, audio_path or None if not cached / expired.
    """
    key = _cache_key(bvid, codecid, qn)
    index = _load_cache_index()

    entry = index.get(key)
    if not entry:
        return None

    # Check TTL
    if time.time() - entry.get("downloaded_at", 0) > CACHE_TTL_SECONDS:
        # Expired — clean up
        _purge_entry(key, index)
        return None

    # Verify files exist
    entry_dir = _entry_dir(key)
    video_path = os.path.join(entry_dir, "video.m4s")
    audio_path = os.path.join(entry_dir, "audio.m4s")

    if not os.path.exists(video_path):
        _purge_entry(key, index)
        return None

    return {
        "video_path": video_path,
        "audio_path": audio_path,
        "size_bytes": entry.get("total_size", 0),
        "downloaded_at": entry.get("downloaded_at", 0),
    }


def download_dash(
    bvid: str,
    codecid: int,
    qn: int,
    video_url: str,
    audio_url: str,
    cookie_str: str = "",
    connections: Optional[int] = None,
) -> dict:
    """
    Download DASH video + audio streams using aria2c with multiple connections.

    Args:
        bvid: Bilibili BV号
        codecid: Source codec ID
        qn: Quality number
        video_url: DASH video URL
        audio_url: DASH audio URL
        cookie_str: B站 cookie string for auth
        connections: Number of parallel connections per file (default from config ARIA2_CONNECTIONS)

    Returns:
        dict with video_path, audio_path, size_bytes, elapsed_seconds
    """
    if connections is None:
        connections = ARIA2_CONNECTIONS
    key = _cache_key(bvid, codecid, qn)
    entry_dir = _entry_dir(key)
    os.makedirs(entry_dir, exist_ok=True)

    # Build aria2c command: --split controls chunks, --max-connection-per-server capped at 16
    start_time = time.time()
    actual_connections = connections or ARIA2_CONNECTIONS
    max_conn_per_server = min(actual_connections, 16)  # aria2c hard limit

    video_path = os.path.join(entry_dir, "video.m4s")
    audio_path = os.path.join(entry_dir, "audio.m4s")

    # Build header list
    header_args = []
    header_args += ["--header", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"]
    header_args += ["--header", "Referer: https://www.bilibili.com"]
    if cookie_str:
        header_args += ["--header", f"Cookie: {cookie_str}"]

    # Download video
    video_cmd = [
        ARIA2C_PATH,
        "--split", str(actual_connections),
        "--max-connection-per-server", str(max_conn_per_server),
        "--min-split-size", "1M",
        "--max-concurrent-downloads", "2",
        "--dir", entry_dir,
        "--out", "video.m4s",
        "--console-log-level", "error",
        "--summary-interval", "0",
    ] + header_args + [video_url]

    logger.info("aria2c downloading video (key=%s, connections=%d)...", key, connections)
    try:
        result = subprocess.run(
            video_cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 min max
        )
    except subprocess.TimeoutExpired:
        logger.error("aria2c video download timed out (key=%s)", key)
        shutil.rmtree(entry_dir, ignore_errors=True)
        return {"error": "视频下载超时"}

    if result.returncode != 0 or not os.path.exists(video_path):
        stderr_tail = result.stderr[-500:] if result.stderr else "no stderr"
        logger.error("aria2c video download failed (key=%s): %s", key, stderr_tail)
        shutil.rmtree(entry_dir, ignore_errors=True)
        return {"error": f"视频下载失败: aria2c exit={result.returncode}"}

    video_size = os.path.getsize(video_path)
    logger.info("Video downloaded: %.1f MB in %.1fs (key=%s)",
                  video_size / 1024 / 1024, time.time() - start_time, key)

    # Download audio if available
    audio_size = 0
    if audio_url:
        audio_cmd = [
            ARIA2C_PATH,
            "--split", str(max(4, actual_connections // 2)),
            "--max-connection-per-server", str(max(4, max_conn_per_server // 2)),
            "--min-split-size", "1M",
            "--dir", entry_dir,
            "--out", "audio.m4s",
            "--console-log-level", "error",
            "--summary-interval", "0",
        ] + header_args + [audio_url]

        try:
            result = subprocess.run(
                audio_cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            logger.warning("aria2c audio download timed out (key=%s)", key)
            # Audio is optional — continue without it
            if os.path.exists(audio_path):
                audio_size = os.path.getsize(audio_path)

        if result.returncode == 0 and os.path.exists(audio_path):
            audio_size = os.path.getsize(audio_path)
            logger.info("Audio downloaded: %.1f MB (key=%s)", audio_size / 1024 / 1024, key)

    elapsed = time.time() - start_time
    total_size = video_size + audio_size

    # Save to cache index
    index = _load_cache_index()
    index[key] = {
        "bvid": bvid,
        "codecid": codecid,
        "qn": qn,
        "downloaded_at": time.time(),
        "total_size": total_size,
        "video_size": video_size,
        "audio_size": audio_size,
    }
    _save_cache_index(index)

    logger.info("Download complete: %.1f MB in %.1fs (%.1f MB/s, key=%s)",
                  total_size / 1024 / 1024, elapsed,
                  (total_size / 1024 / 1024 / max(elapsed, 0.1)), key)

    return {
        "video_path": video_path,
        "audio_path": audio_path,
        "size_bytes": total_size,
        "elapsed_seconds": elapsed,
        "key": key,
    }


def _purge_entry(key: str, index: dict):
    """Remove a cache entry from disk and index."""
    entry_dir = _entry_dir(key)
    if os.path.exists(entry_dir):
        shutil.rmtree(entry_dir, ignore_errors=True)
        logger.info("Purged cache entry: %s", key)
    index.pop(key, None)


def cleanup_expired_cache():
    """Remove all expired cache entries. Called on startup and periodically."""
    index = _load_cache_index()
    now = time.time()
    expired_keys = [
        key for key, entry in index.items()
        if now - entry.get("downloaded_at", 0) > CACHE_TTL_SECONDS
    ]

    for key in expired_keys:
        _purge_entry(key, index)

    if expired_keys:
        _save_cache_index(index)
        logger.info("Cleaned up %d expired cache entries", len(expired_keys))

    # Also cleanup any orphaned directories
    if os.path.exists(CACHE_DIR):
        valid_keys = set(index.keys())
        for entry_name in os.listdir(CACHE_DIR):
            entry_path = os.path.join(CACHE_DIR, entry_name)
            if os.path.isdir(entry_path) and entry_name not in valid_keys:
                shutil.rmtree(entry_path, ignore_errors=True)
                logger.info("Removed orphaned cache dir: %s", entry_name)


def delete_cache_entry(cache_key: str) -> bool:
    """Delete a specific cache entry by key. Returns True if deleted."""
    index = _load_cache_index()
    if cache_key in index:
        _purge_entry(cache_key, index)
        _save_cache_index(index)
        logger.info("Deleted cache entry: %s", cache_key)
        return True
    return False


def get_cache_stats() -> dict:
    """Return cache statistics for Web panel display."""
    index = _load_cache_index()
    now = time.time()
    entries = []

    total_size = 0
    for key, entry in index.items():
        age_seconds = now - entry.get("downloaded_at", 0)
        remaining = max(0, CACHE_TTL_SECONDS - age_seconds)
        total_size += entry.get("total_size", 0)
        entries.append({
            "key": key,
            "bvid": entry.get("bvid", ""),
            "codecid": entry.get("codecid", 0),
            "qn": entry.get("qn", 0),
            "size_mb": round(entry.get("total_size", 0) / 1024 / 1024, 1),
            "downloaded_at": entry.get("downloaded_at", 0),
            "age_minutes": round(age_seconds / 60, 1),
            "remaining_minutes": round(remaining / 60, 1),
            "expired": age_seconds > CACHE_TTL_SECONDS,
        })

    return {
        "total_entries": len(entries),
        "total_size_mb": round(total_size / 1024 / 1024, 1),
        "cache_dir": CACHE_DIR,
        "ttl_hours": CACHE_TTL_SECONDS / 3600,
        "entries": sorted(entries, key=lambda e: e["downloaded_at"] if "downloaded_at" in e else 0, reverse=True),
    }


def download_dash_sync(
    bvid: str,
    codecid: int,
    qn: int,
    video_url: str,
    audio_url: str,
    cookie_str: str = "",
) -> dict:
    """
    Synchronous wrapper around download_dash. Checks cache first.
    Returns dict with paths or error.
    """
    # Check cache first
    cached = get_cached_paths(bvid, codecid, qn)
    if cached:
        logger.info("Cache HIT for bvid=%s codecid=%d qn=%d", bvid, codecid, qn)
        return {**cached, "from_cache": True}

    logger.info("Cache MISS for bvid=%s codecid=%d qn=%d — downloading...", bvid, codecid, qn)
    result = download_dash(bvid, codecid, qn, video_url, audio_url, cookie_str)
    if "error" in result:
        return result
    return {**result, "from_cache": False}


# ============================================================
#  ASYNC DOWNLOAD WITH PROGRESS TRACKING
# ============================================================

import threading
import re

# In-memory progress tracking for background download tasks
_download_progress: dict[str, dict] = {}
_progress_lock = threading.Lock()


def _set_progress(task_id: str, **kwargs):
    with _progress_lock:
        if task_id not in _download_progress:
            _download_progress[task_id] = {}
        _download_progress[task_id].update(kwargs)
        _download_progress[task_id]["updated_at"] = time.time()


def get_download_progress(task_id: str) -> dict:
    """Return current progress for a background download task."""
    with _progress_lock:
        return dict(_download_progress.get(task_id, {"status": "not_found"}))


def cleanup_old_progress(max_age: float = 3600):
    """Remove progress entries older than max_age seconds."""
    with _progress_lock:
        now = time.time()
        stale = [tid for tid, p in _download_progress.items()
                  if now - p.get("updated_at", 0) > max_age]
        for tid in stale:
            del _download_progress[tid]


def _run_aria2c_with_progress(task_id: str, cmd: list, label: str, total_label: str):
    """Run aria2c and parse console output for progress."""
    import subprocess as sp

    proc = sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.PIPE, text=True, bufsize=1)
    total_size = 0

    # Parse aria2c console output: [DL:1.2MiB][#1 SIZE:50MiB/99MiB(50%) CN:8 SPD:12MiB/s ETA:5s]
    progress_re = re.compile(r'SIZE:(\d+[KMGT]?i?B)/(\d+[KMGT]?i?B)\((\d+)%\).*?SPD:(\S+).*?ETA:(\S+)')

    # aria2c outputs progress to stderr with console-log-level=notice
    for line in proc.stderr:
        line = line.strip()
        if not line:
            continue

        match = progress_re.search(line)
        if match:
            size_str = match.group(1)
            total_str = match.group(2)
            pct = int(match.group(3))
            speed_str = match.group(4)
            eta_str = match.group(5)

            _set_progress(task_id,
                status="downloading",
                label=label,
                progress_pct=pct,
                downloaded=size_str,
                total=total_str,
                speed=speed_str,
                eta=eta_str,
                total_label=total_label,
            )

    proc.wait()

    if proc.returncode == 0:
        _set_progress(task_id, status=f"{label}_complete", progress_pct=100,
                       label=label, total_label=total_label)
    else:
        _set_progress(task_id, status="error",
                       label=label,
                       error=f"aria2c exit={proc.returncode}",
                       stderr_tail=line[-300:] if 'line' in dir() else "")


def download_dash_background(
    bvid: str, codecid: int, qn: int,
    video_url: str, audio_url: str,
    cookie_str: str = "",
    title: str = "", duration: float = 0,
) -> str:
    """
    Start multi-threaded download in background, return task_id for polling.

    Progress is tracked via get_download_progress(task_id).
    """
    import uuid
    task_id = uuid.uuid4().hex[:12]

    key = _cache_key(bvid, codecid, qn)
    entry_dir = _entry_dir(key)
    os.makedirs(entry_dir, exist_ok=True)

    actual_connections = ARIA2_CONNECTIONS
    max_conn_per_server = min(actual_connections, 16)

    header_args = []
    header_args += ["--header", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"]
    header_args += ["--header", "Referer: https://www.bilibili.com"]
    if cookie_str:
        header_args += ["--header", f"Cookie: {cookie_str}"]

    _set_progress(task_id,
        status="starting",
        bvid=bvid,
        title=title,
        duration=duration,
        codecid=codecid,
        qn=qn,
        cache_key=key,
        video_progress=0,
        audio_progress=0,
    )

    def _download_both():
        # Download video
        video_path = os.path.join(entry_dir, "video.m4s")
        video_cmd = [
            ARIA2C_PATH,
            "--split", str(actual_connections),
            "--max-connection-per-server", str(max_conn_per_server),
            "--min-split-size", "1M",
            "--dir", entry_dir,
            "--out", "video.m4s",
            "--console-log-level", "notice",
            "--show-console-readout", "true",
            "--summary-interval", "0",
        ] + header_args + [video_url]

        _set_progress(task_id, status="downloading_video")
        _run_aria2c_with_progress(task_id, video_cmd, "视频", "视频+音频")

        video_ok = os.path.exists(video_path) and os.path.getsize(video_path) > 0
        video_size = os.path.getsize(video_path) if video_ok else 0

        # Download audio
        audio_ok = False
        audio_size = 0
        if audio_url:
            audio_path = os.path.join(entry_dir, "audio.m4s")
            audio_cmd = [
                ARIA2C_PATH,
                "--split", str(max(4, actual_connections // 2)),
                "--max-connection-per-server", str(max(4, max_conn_per_server // 2)),
                "--min-split-size", "1M",
                "--dir", entry_dir,
                "--out", "audio.m4s",
                "--console-log-level", "notice",
                "--show-console-readout", "true",
                "--summary-interval", "0",
            ] + header_args + [audio_url]

            _set_progress(task_id, status="downloading_audio")
            _run_aria2c_with_progress(task_id, audio_cmd, "音频", "视频+音频")

            audio_ok = os.path.exists(audio_path) and os.path.getsize(audio_path) > 0
            audio_size = os.path.getsize(audio_path) if audio_ok else 0

        if video_ok:
            total_size = video_size + audio_size
            # Save to cache index
            index = _load_cache_index()
            index[key] = {
                "bvid": bvid,
                "codecid": codecid,
                "qn": qn,
                "downloaded_at": time.time(),
                "total_size": total_size,
                "video_size": video_size,
                "audio_size": audio_size,
            }
            _save_cache_index(index)
            _set_progress(task_id, status="complete", progress_pct=100,
                          total_size=total_size, cache_key=key)
        else:
            shutil.rmtree(entry_dir, ignore_errors=True)
            _set_progress(task_id, status="error", error="视频下载失败")

    t = threading.Thread(target=_download_both, daemon=True)
    t.start()

    return task_id

