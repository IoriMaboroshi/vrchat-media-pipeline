"""
Pipeline: preload DASH → GPU transcode HLS → upload to aliWH1.
Real-time progress per stage, persistent task status, multi-task support.
"""
import os
import subprocess
import threading
import time
import uuid
import shutil
import json
import re
import hashlib
import logging
import tempfile
from typing import Optional

from config import BILIBILI_UA, BILIBILI_REFERER, FFMPEG_PATH, QUALITY_MAP, ARIA2_CONNECTIONS
from utils.downloader import _cache_key, _entry_dir

logger = logging.getLogger("bilibili-proxy.pipeline")

ARIA2C_PATH = shutil.which("aria2c") or "aria2c"
RCLONE_PATH = shutil.which("rclone") or "rclone"
LOCAL_HLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "hls_output")
PUBLIC_PLAY_URL = "http://8.148.64.28:14515"
PUBLIC_TOKEN = "thechwinlyu"
ALIWH1_REMOTE = "aliwh1-pub"  # Public IP for faster upload
MANIFEST_PATH = f"{ALIWH1_REMOTE}:/var/www/hls/manifest.json"
TASK_DB_FILE = os.path.join(os.path.dirname(LOCAL_HLS_DIR), "pipeline_tasks.json")

os.makedirs(LOCAL_HLS_DIR, exist_ok=True)

# Progress tracking (in-memory + persistent)
_pipeline_progress: dict[str, dict] = {}
_progress_lock = threading.Lock()


def _load_task_db() -> dict:
    if os.path.exists(TASK_DB_FILE):
        try:
            with open(TASK_DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_task_db(db: dict):
    with open(TASK_DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def _set_progress(task_id: str, **kwargs):
    with _progress_lock:
        if task_id not in _pipeline_progress:
            _pipeline_progress[task_id] = {}
        _pipeline_progress[task_id].update(kwargs)
        _pipeline_progress[task_id]["task_id"] = task_id
        _pipeline_progress[task_id]["updated_at"] = time.time()
        # Persist to disk
        try:
            db = _load_task_db()
            db[task_id] = dict(_pipeline_progress[task_id])
            _save_task_db(db)
        except Exception:
            pass


def get_pipeline_status(task_id: str) -> dict:
    with _progress_lock:
        return dict(_pipeline_progress.get(task_id, {"status": "not_found"}))


def get_all_tasks() -> list:
    """Return all known tasks (active + completed) sorted by time."""
    db = _load_task_db()
    tasks = []
    for tid, info in db.items():
        info["task_id"] = tid  # Ensure task_id is in the dict
        tasks.append(info)
    tasks.sort(key=lambda t: t.get("created_at", 0), reverse=True)
    # Merge with in-memory (in-memory may have fresher data)
    for tid, info in _pipeline_progress.items():
        # Also sync preload progress
        if info.get("status") == "downloading" and info.get("stage") == "download":
            try:
                from utils.downloader import get_download_progress
                dp = get_download_progress(tid)
                if dp.get("status") in ("complete", "error"):
                    info["status"] = "done" if dp["status"] == "complete" else "error"
                    info["detail"] = f"预加载完成" if dp["status"] == "complete" else dp.get("error", "预加载失败")
                    info["progress_pct"] = 100 if dp["status"] == "complete" else 0
                else:
                    info["progress_pct"] = dp.get("progress_pct", 0)
                    if dp.get("downloaded") and dp.get("speed"):
                        info["detail"] = f"预加载 {dp['downloaded']} {dp['speed']}"
                    else:
                        info["detail"] = f"预加载 {dp.get('progress_pct',0)}%"
            except Exception:
                pass
        found = False
        for t in tasks:
            if t.get("task_id") == tid:
                t.update(info)
                found = True
                break
        if not found:
            info["task_id"] = tid
            tasks.insert(0, dict(info))
    return tasks


# ============ MANIFEST MANAGEMENT ============

def _load_manifest() -> dict:
    try:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp_path = f.name
        subprocess.run([RCLONE_PATH, "copyto", MANIFEST_PATH, tmp_path, "--ignore-errors"],
                       capture_output=True, timeout=30)
        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
            with open(tmp_path, encoding="utf-8") as f:
                data = json.load(f)
            os.unlink(tmp_path)
            return data
        os.unlink(tmp_path)
    except Exception:
        pass
    return {"videos": []}


def _save_manifest(manifest: dict):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False)
        tmp_path = f.name
    subprocess.run([RCLONE_PATH, "copyto", tmp_path, MANIFEST_PATH], check=True, timeout=30, capture_output=True)
    os.unlink(tmp_path)


def update_manifest(bvid: str, title: str, cover: str, duration: float, qx: str, segments: int):
    manifest = _load_manifest()
    now = time.time()
    videos = [v for v in manifest.get("videos", []) if v.get("bvid") != bvid]
    videos.append({"bvid": bvid, "title": title, "cover": cover, "duration": duration,
                   "qx": qx, "segments": segments, "pushed_at": now, "expires_at": now + 24 * 3600})
    videos.sort(key=lambda v: v.get("pushed_at", 0), reverse=True)
    manifest["videos"] = videos
    _save_manifest(manifest)


def list_aliwh1_videos() -> list:
    return _load_manifest().get("videos", [])


def delete_aliwh1_video(bvid: str) -> bool:
    try:
        subprocess.run([RCLONE_PATH, "delete", f"{ALIWH1_REMOTE}:/var/www/hls/{bvid}"], check=True, timeout=30, capture_output=True)
        manifest = _load_manifest()
        manifest["videos"] = [v for v in manifest.get("videos", []) if v.get("bvid") != bvid]
        _save_manifest(manifest)
        return True
    except Exception as e:
        logger.error("Failed to delete %s: %s", bvid, e)
        return False


# ============ PIPELINE RUNNER ============

def run_pipeline(bvid: str, qx: str, video_url: str, audio_url: str,
                 codecid: int, qn: int, duration: float, title: str,
                 cookie_str: str = "", cover: str = "",
                 local_video: str = None, local_audio: str = None) -> str:
    task_id = uuid.uuid4().hex[:8]
    _set_progress(task_id, status="starting", bvid=bvid, qx=qx, title=title,
                  stage="download", progress_pct=0, created_at=time.time(),
                  detail="准备中...")

    def _run():
        try:
            # ======= STAGE 1: DOWNLOAD (skip if cached) =======
            _set_progress(task_id, status="downloading", stage="download", progress_pct=0,
                          detail="正在下载视频流...",
                          download_video_pct=0, download_video_speed="", download_video_eta="")

            key = _cache_key(bvid, codecid, qn)
            work_dir = _entry_dir(key)

            if local_video and os.path.exists(local_video):
                video_path = local_video
                audio_path = local_audio if local_audio and os.path.exists(local_audio) else ""
                _set_progress(task_id, status="downloading", stage="download", progress_pct=50,
                              detail=f"使用本地缓存 {os.path.getsize(video_path)/1048576:.0f}MB，跳过下载")
            else:
                os.makedirs(work_dir, exist_ok=True)
                video_path = os.path.join(work_dir, "video.m4s")
                audio_path = os.path.join(work_dir, "audio.m4s")

                # Register cache entry immediately
                from utils.downloader import _load_cache_index, _save_cache_index
                cache_index = _load_cache_index()
                cache_index[key] = {"bvid": bvid, "codecid": codecid, "qn": qn,
                                   "downloaded_at": time.time(), "total_size": 0}
                _save_cache_index(cache_index)

                # Download video with progress polling
                conn = min(ARIA2_CONNECTIONS, 16)
                headers = ["--header", f"User-Agent: {BILIBILI_UA}", "--header", f"Referer: {BILIBILI_REFERER}"]
                if cookie_str:
                    headers += ["--header", f"Cookie: {cookie_str}"]

                video_cmd = [
                    ARIA2C_PATH, "--split", str(conn), "--max-connection-per-server", str(conn),
                    "--min-split-size", "1M", "--dir", work_dir, "--out", "video.m4s",
                    "--console-log-level", "error", "--summary-interval", "0",
                ] + headers + [video_url]

                proc = subprocess.Popen(video_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                start_time = time.time()
                while proc.poll() is None:
                    time.sleep(1)
                    if os.path.exists(video_path):
                        sz = os.path.getsize(video_path)
                        sz_mb = sz / 1024 / 1024
                        elapsed = time.time() - start_time
                        speed_mb = sz_mb / max(elapsed, 0.1)
                        _set_progress(task_id, status="downloading", stage="download",
                                      progress_pct=int(min(sz_mb / 100, 1) * 45) if sz_mb < 100 else min(10 + int(sz_mb / 500 * 35), 45),
                                      detail=f"视频 {sz_mb:.0f}MB {speed_mb:.1f}MB/s",
                                      download_video_pct=min(int(sz_mb / 500 * 100), 99),
                                      download_video_speed=f"{speed_mb:.1f}MB/s")
                proc.wait()
                if proc.returncode != 0:
                    if not os.path.exists(video_path) or os.path.getsize(video_path) < 1024 * 1024:
                        raise RuntimeError("视频下载失败")
                    logger.warning("aria2c exit=%d but file exists (%d MB), continuing",
                                   proc.returncode, os.path.getsize(video_path) // 1048576)
                _set_progress(task_id, status="downloading", stage="download", progress_pct=48,
                              detail=f"视频已下载 {os.path.getsize(video_path)/1048576:.0f}MB")

                # Download audio
                if audio_url:
                    _set_progress(task_id, status="downloading", stage="download", progress_pct=49,
                                  detail="下载音频流...")
                    try:
                        audio_cmd = [
                            ARIA2C_PATH, "--split", str(max(4, conn // 2)),
                            "--max-connection-per-server", str(max(4, conn // 2)),
                            "--min-split-size", "1M", "--dir", work_dir, "--out", "audio.m4s",
                            "--console-log-level", "error", "--summary-interval", "0",
                        ] + headers + [audio_url]
                        subprocess.run(audio_cmd, check=True, timeout=300, capture_output=True)
                    except Exception:
                        logger.warning("Audio download failed, continuing without audio")

                # Update cache index with real sizes
                ci_v = os.path.getsize(video_path) / 1024 / 1024
                ci_a = os.path.getsize(audio_path) / 1024 / 1024 if os.path.exists(audio_path) else 0
                try:
                    ci = _load_cache_index()
                    ci[key] = {"bvid": bvid, "codecid": codecid, "qn": qn,
                              "downloaded_at": time.time(), "total_size": int((ci_v + ci_a) * 1024 * 1024),
                              "video_size": int(ci_v * 1024 * 1024),
                              "audio_size": int(ci_a * 1024 * 1024)}
                    _save_cache_index(ci)
                except Exception:
                    pass

            # Compute sizes (works for both cache and download paths)
            v_size = os.path.getsize(video_path) / 1024 / 1024 if os.path.exists(video_path) else 0
            a_size = os.path.getsize(audio_path) / 1024 / 1024 if os.path.exists(audio_path) else 0
            _set_progress(task_id, status="downloading", stage="download", progress_pct=50,
                          detail=f"下载完成: 视频{v_size:.0f}MB 音频{a_size:.0f}MB")

            # ======= STAGE 2: TRANSCODE =======
            _set_progress(task_id, status="transcoding", stage="transcode", progress_pct=50,
                          detail="GPU转码中...", transcode_time="0:00")

            output_dir = os.path.join(LOCAL_HLS_DIR, bvid)
            if os.path.exists(output_dir):
                shutil.rmtree(output_dir)
            os.makedirs(output_dir, exist_ok=True)
            playlist_path = os.path.join(output_dir, "playlist.m3u8")

            ff_cmd = [FFMPEG_PATH, "-y", "-hide_banner",
                      "-fflags", "+genpts+igndts+discardcorrupt",
                      "-analyzeduration", "5M", "-probesize", "5M"]
            ff_cmd += ["-i", video_path]
            if os.path.exists(audio_path):
                ff_cmd += ["-i", audio_path, "-map", "0:v:0", "-map", "1:a:0",
                           "-c:a", "aac", "-b:a", "128k", "-af", "aresample=async=1:first_pts=0"]
            else:
                ff_cmd += ["-map", "0:v:0"]
            if codecid == 7:
                ff_cmd += ["-c:v", "copy"]
            else:
                ff_cmd += ["-c:v", "h264_amf", "-quality", "quality", "-rc", "vbr_peak", "-b:v", "8M"]
            ff_cmd += [
                "-max_interleave_delta", "5000000", "-avoid_negative_ts", "make_zero",
                "-f", "hls", "-hls_init_time", "2", "-hls_time", "4",
                "-hls_list_size", "0", "-hls_playlist_type", "vod",
                "-hls_flags", "independent_segments",
                "-hls_segment_filename", os.path.join(output_dir, "seg_%03d.ts"),
                playlist_path,
            ]

            transcode_start = time.time()
            proc = subprocess.Popen(ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
            time_re = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
            speed_re = re.compile(r"speed=\s*(\S+)x")
            for line in proc.stderr:
                m = time_re.search(line)
                if m and duration > 0:
                    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
                    current = h * 3600 + mi * 60 + s
                    speed_str = ""
                    sm = speed_re.search(line)
                    if sm:
                        speed_str = f" {sm.group(1)}x"
                    pct = min(50 + int(current / duration * 45), 94)
                    _set_progress(task_id, status="transcoding", stage="transcode", progress_pct=pct,
                                  detail=f"转码 {int(current)}s/{int(duration)}s{speed_str}",
                                  transcode_time=f"{int(current//60)}:{int(current%60):02d}")
            proc.wait()
            if proc.returncode != 0:
                raise RuntimeError(f"FFmpeg failed (exit {proc.returncode})")

            elapsed = time.time() - transcode_start
            seg_count = len([f for f in os.listdir(output_dir) if f.endswith(".ts")])
            total_size = sum(os.path.getsize(os.path.join(output_dir, f)) for f in os.listdir(output_dir)) / 1024 / 1024

            # Post-process playlist
            seg_base = f"{PUBLIC_PLAY_URL}/hls/{bvid}"
            with open(playlist_path, "r", encoding="utf-8") as f:
                pl_lines = f.readlines()
            with open(playlist_path, "w", encoding="utf-8") as f:
                for line in pl_lines:
                    stripped = line.strip()
                    if stripped.startswith("seg_") and stripped.endswith(".ts") and not stripped.startswith("http"):
                        line = f"{seg_base}/{stripped}?token={PUBLIC_TOKEN}\n"
                    f.write(line)

            _set_progress(task_id, status="transcoding", stage="transcode", progress_pct=95,
                          detail=f"转码完成 {seg_count}段 {total_size:.0f}MB {elapsed:.1f}s {duration/elapsed:.1f}x")

            # ======= STAGE 3: UPLOAD =======
            _set_progress(task_id, status="uploading", stage="upload", progress_pct=96,
                          detail=f"上传 {total_size:.0f}MB 0%", upload_pct=0, upload_speed="",
                          total_size_mb=total_size)

            remote_path = f"{ALIWH1_REMOTE}:/var/www/hls/{bvid}"
            upload_log = os.path.join(tempfile.gettempdir(), f"rclone_{task_id}.log")
            rclone_cmd = [
                RCLONE_PATH, "copy", output_dir, remote_path,
                "--transfers", "8", "--no-traverse",
                "--progress", "--stats-one-line-date", "--stats", "3s",
            ]
            # Redirect stderr to log file (line-buffered for real-time progress)
            with open(upload_log, "w") as log_f:
                proc = subprocess.Popen(rclone_cmd, stdout=subprocess.DEVNULL, stderr=log_f)

            # Poll log file for progress
            rclone_re = re.compile(r"(\d+\.?\d*)\s*(\S?i?B)\s*/\s*[\d.]+\s*\S+i?B?,\s*(\d+)%")
            last_pos = 0
            while proc.poll() is None:
                time.sleep(2)
                try:
                    with open(upload_log, "r") as f:
                        f.seek(last_pos)
                        new_data = f.read()
                        last_pos = f.tell()
                    for line in new_data.split("\n"):
                        m = rclone_re.search(line)
                        if m:
                            pct = int(m.group(3))
                            _set_progress(task_id, status="uploading", stage="upload",
                                          progress_pct=96 + int(pct * 0.04),
                                          detail=f"上传 {pct}%",
                                          upload_pct=pct)
                except Exception:
                    pass

            try:
                os.unlink(upload_log)
            except Exception:
                pass

            # ======= DONE =======
            play_url = f"{PUBLIC_PLAY_URL}/hls/{bvid}/playlist.m3u8?token={PUBLIC_TOKEN}"
            update_manifest(bvid, title, cover, duration, qx, seg_count)
            _set_progress(task_id, status="done", stage="done", progress_pct=100,
                          detail="推送完成！", play_url=play_url, title=title,
                          duration=duration, qx=qx, segments=seg_count, total_size_mb=round(total_size, 1))

        except subprocess.TimeoutExpired:
            logger.exception("Pipeline timeout for %s", bvid)
            _set_progress(task_id, status="error", detail="操作超时，文件过大或网络太慢", progress_pct=0)
        except RuntimeError as e:
            msg = str(e)
            if "下载失败" in msg:
                msg = "下载失败：B站链接可能过期，重新推送即可"
            elif "rclone" in msg.lower() or "upload" in msg.lower():
                msg = "上传失败：检查网络连接，重新推送即可"
            _set_progress(task_id, status="error", detail=msg, progress_pct=0)
        except Exception as e:
            logger.exception("Pipeline failed for %s: %s", bvid, e)
            _set_progress(task_id, status="error", detail=f"未知错误: {str(e)[:80]}", progress_pct=0)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return task_id
