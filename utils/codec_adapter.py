"""
Codec adapter for H.264 HLS output.
Detects source codec from Bilibili DASH, selects best available hardware encoder,
generates optimized FFmpeg commands for fast-start HLS streaming.

Supports: NVIDIA NVENC, AMD AMF, Intel QSV, VideoToolbox (macOS), libx264 fallback.
"""

import logging
import subprocess
import os
import platform
from dataclasses import dataclass
from typing import Optional, Tuple

logger = logging.getLogger("bilibili-proxy.codec")

# Bilibili codecid constants
CODECID_AVC = 7
CODECID_HEVC = 12
CODECID_AV1 = 13

# Bilibili DASH quality qn constants
QN_360P = 8
QN_480P = 16
QN_720P = 32
QN_1080P = 64
QN_1080P60 = 112
QN_4K = 120

# Resolution map: qx → (width, height) - used for resolution caps
QUALITY_RESOLUTIONS: dict[str, Tuple[int, int]] = {
    "4k": (3840, 2160),
    "1080p60": (1920, 1080),
    "1080p": (1920, 1080),
    "720p": (1280, 720),
    "480p": (854, 480),
    "360p": (640, 360),
}

# Human-readable quality labels
QUALITY_LABELS: dict[int, str] = {
    QN_360P: "360P 流畅",
    QN_480P: "480P 清晰",
    QN_720P: "720P 高清",
    QN_1080P: "1080P 高清",
    QN_1080P60: "1080P 60fps",
    QN_4K: "4K 超清",
}

# Quality fallback order: best → worst
QUALITY_FALLBACK_ORDER: list[int] = [QN_4K, QN_1080P60, QN_1080P, QN_720P, QN_480P, QN_360P]

# qx → qn mapping
QUALITY_MAP: dict[str, int] = {
    "4k": QN_4K,
    "1080p60": QN_1080P60,
    "1080p": QN_1080P,
    "720p": QN_720P,
    "480p": QN_480P,
    "360p": QN_360P,
}

# Standard quality names (for validation)
STANDARD_QUALITIES: set[str] = set(QUALITY_MAP.keys())

# Source codec → display name
CODEC_LABELS: dict[int, str] = {
    CODECID_AVC: "AVC/H.264",
    CODECID_HEVC: "HEVC/H.265",
    CODECID_AV1: "AV1",
}


def get_qx_resolution(qx: str) -> Optional[int]:
    """Return height for a quality name, or None if unknown."""
    res = QUALITY_RESOLUTIONS.get(qx)
    return res[1] if res else None


def needs_downscale(source_qx: str, max_height: int) -> bool:
    """Check if source quality qx exceeds the max output resolution."""
    src_h = get_qx_resolution(source_qx)
    if src_h is None:
        return False
    return src_h > max_height


def get_downscale_qx(source_qx: str, max_height: int) -> Optional[str]:
    """Find the closest standard qx that fits within max_height."""
    src_h = get_qx_resolution(source_qx)
    if src_h is None or src_h <= max_height:
        return None
    for qx in ["1080p", "720p", "480p", "360p"]:
        h = get_qx_resolution(qx)
        if h is not None and h <= max_height:
            return qx
    return "360p"


# ---- Encoder info dataclass ----

@dataclass
class EncoderInfo:
    name: str
    encoder: str
    hwaccel: Optional[str]
    description: str


def _get_gpu_vendor() -> Optional[str]:
    """Detect the primary GPU vendor on this system. Returns 'nvidia', 'amd', 'intel', or None."""
    try:
        if platform.system() == "Windows":
            import subprocess as sp
            result = sp.run(
                ["powershell", "-Command",
                 "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"],
                capture_output=True, text=True, timeout=10,
            )
            names = result.stdout.lower()
            if "nvidia" in names or "nv" in names:
                return "nvidia"
            if "amd" in names or "radeon" in names:
                return "amd"
            if "intel" in names or "iris" in names or "uhd" in names:
                return "intel"
        elif platform.system() == "Linux":
            try:
                with open("/proc/driver/nvidia/version") as f:
                    if "NVIDIA" in f.read():
                        return "nvidia"
            except Exception:
                pass
            result = subprocess.run(
                ["lspci"], capture_output=True, text=True, timeout=5,
            )
            out = result.stdout.lower()
            if "amd" in out or "radeon" in out:
                return "amd"
            if "intel" in out:
                return "intel"
    except Exception:
        pass
    return None


def _ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _check_encoders() -> set[str]:
    try:
        result = subprocess.run(["ffmpeg", "-encoders"], capture_output=True, text=True, timeout=10)
        available = set()
        for line in result.stdout.split("\n"):
            if "h264" in line.lower() and line.strip().startswith("V"):
                parts = line.split()
                if len(parts) >= 2:
                    available.add(parts[1].strip())
        return available
    except Exception as e:
        logger.warning("Failed to probe ffmpeg encoders: %s", e)
        return {"libx264"}


def _validate_encoder_works(encoder_name: str) -> bool:
    """Quick test: encode a few frames to verify the encoder works on this hardware."""
    try:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "nullsrc=s=640x360:d=0.5",
            "-frames:v", "5",
            "-c:v", encoder_name,
            "-f", "null", "-",
        ]
        # VA-API requires special device initialization
        if encoder_name == "h264_vaapi":
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-vaapi_device", "/dev/dri/renderD128",
                "-f", "lavfi", "-i", "nullsrc=s=640x360:d=0.5",
                "-vf", "format=nv12,hwupload",
                "-frames:v", "5",
                "-c:v", "h264_vaapi",
                "-f", "null", "-",
            ]
        subprocess.run(cmd, capture_output=True, timeout=10, check=True)
        return True
    except Exception:
        return False


_VENDOR_ENCODER_MAP = {
    "nvidia": [
        {"check_string": "h264_nvenc", "name": "nvenc", "encoder": "h264_nvenc", "hwaccel": "cuda", "description": "NVIDIA NVENC"},
    ],
    "amd": [
        {"check_string": "h264_amf", "name": "amf", "encoder": "h264_amf", "hwaccel": None, "description": "AMD AMF"},
        {"check_string": "h264_vaapi", "name": "vaapi", "encoder": "h264_vaapi", "hwaccel": "vaapi", "description": "AMD VA-API"},
    ],
    "intel": [
        {"check_string": "h264_qsv", "name": "qsv", "encoder": "h264_qsv", "hwaccel": "qsv", "description": "Intel QuickSync"},
    ],
}


def detect_encoders() -> list[EncoderInfo]:
    """
    Probe ffmpeg + system hardware to find actually-usable H.264 encoders.
    Validates each encoder with a quick test before accepting it.
    Falls back to libx264 if no hardware encoder works.
    """
    if not _ffmpeg_available():
        logger.warning("ffmpeg not found on PATH")
        return []

    gpu_vendor = _get_gpu_vendor()
    logger.info("Detected GPU vendor: %s", gpu_vendor or "unknown")

    available_encoders = _check_encoders()
    logger.info("Available H.264 encoders (ffmpeg): %s", available_encoders)

    result = []

    # Try vendor-specific encoders, validate each one
    vendor_defs = _VENDOR_ENCODER_MAP.get(gpu_vendor, []) if gpu_vendor else []
    for edef in vendor_defs:
        if edef["check_string"] in available_encoders:
            if _validate_encoder_works(edef["encoder"]):
                result.append(EncoderInfo(
                    name=edef["name"],
                    encoder=edef["encoder"],
                    hwaccel=edef["hwaccel"],
                    description=edef["description"],
                ))
                logger.info("Validated encoder: %s", edef["description"])
            else:
                logger.warning("Encoder %s found but validation failed - skipping", edef["encoder"])

    # Software encoder as always-available fallback
    if "libx264" in available_encoders:
        result.append(EncoderInfo(
            name="software",
            encoder="libx264",
            hwaccel=None,
            description="libx264 (software)",
        ))

    logger.info("Usable encoders: %s", [e.name for e in result])
    return result


def select_encoder(encoders: list[EncoderInfo]) -> Optional[EncoderInfo]:
    """Return the best available encoder, or None if none found."""
    if not encoders:
        return None
    return encoders[0]


def source_codec_name(codecid: int) -> str:
    """Return human-readable name for a Bilibili codecid."""
    return CODEC_LABELS.get(codecid, f"未知({codecid})")


def needs_transcode(source_codecid: int) -> bool:
    """
    Whether the source needs transcoding to H.264.
    AVC can be copied directly; HEVC and AV1 need transcoding.
    """
    return source_codecid != CODECID_AVC


def get_encoder_params(encoder: EncoderInfo) -> list[str]:
    """
    Return encoder-specific FFmpeg parameters.
    Prioritizes speed for low-latency HLS streaming.
    """
    if encoder.name == "nvenc":
        return [
            "-preset", "p7",
            "-cq", "23",
            "-rc", "vbr",
            "-b:v", "0",
        ]
    elif encoder.name == "amf":
        return [
            "-usage", "transcoding",
        ]
    elif encoder.name == "qsv":
        return [
            "-global_quality", "23",
            "-preset", "veryfast",
        ]
    elif encoder.name == "videotoolbox":
        return [
            "-quality", "85",
            "-b:v", "5M",
        ]
    else:
        # libx264 software
        return [
            "-preset", "veryfast",
            "-crf", "23",
        ]


def build_hls_command(
    ffmpeg_path: str,
    video_url: str,
    audio_url: Optional[str],
    source_codecid: int,
    tmp_dir: str,
    encoder: EncoderInfo,
    seek_seconds: Optional[int] = None,
    segment_duration: int = 4,
    initial_segment_duration: int = 2,
    user_agent: str = "",
    referer: str = "",
    cookies: str = "",
    max_output_height: Optional[int] = None,
) -> list[str]:
    """
    Build optimized FFmpeg command for fast-start HLS output.

    If max_output_height is set and source exceeds it, adds scale filter
    to downscale (forces transcode for AVC sources when needed).

    AVC source:
      - No downscale → -c copy (zero CPU)
      - Downscale    → GPU transcode + scale filter
    HEVC/AV1 source:
      - Always transcode → + scale filter if downscale needed
    """
    cmd = [ffmpeg_path, "-y"]

    # ---- Performance & sync flags ----
    cmd += [
        "-fflags", "+genpts+igndts+discardcorrupt",
        "-analyzeduration", "5M",
        "-probesize", "5M",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
    ]

    # Decide if we must transcode
    must_transcode = needs_transcode(source_codecid)
    need_downscale = max_output_height is not None and max_output_height > 0

    # ---- HW decode (only when transcoding) ----
    if (must_transcode or need_downscale) and encoder.hwaccel:
        cmd += ["-hwaccel", encoder.hwaccel]

    # ---- Seek ----
    if seek_seconds and seek_seconds > 0:
        cmd += ["-ss", str(seek_seconds)]

    # ---- Input headers ----
    headers = ""
    if referer: headers += f"Referer: {referer}\r\n"
    if user_agent: headers += f"User-Agent: {user_agent}\r\n"
    if cookies: headers += f"Cookie: {cookies}\r\n"
    if headers: cmd += ["-headers", headers]

    cmd += ["-i", video_url]

    if audio_url:
        audio_headers = ""
        if referer: audio_headers += f"Referer: {referer}\r\n"
        if user_agent: audio_headers += f"User-Agent: {user_agent}\r\n"
        if cookies: audio_headers += f"Cookie: {cookies}\r\n"
        if audio_headers: cmd += ["-headers", audio_headers]
        cmd += ["-i", audio_url]

    # ---- Video processing ----
    do_transcode = must_transcode or need_downscale

    # ---- Audio ----
    if audio_url:
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
        cmd += ["-c:a", "aac", "-b:a", "128k", "-af", "asetpts=PTS-STARTPTS"]
    else:
        cmd += ["-map", "0:v:0"]

    # ---- Codec (after -map) ----
    if do_transcode:
        cmd += ["-c:v", encoder.encoder]
        cmd.extend(get_encoder_params(encoder))
        cmd += ["-pix_fmt", "yuv420p"]
        if need_downscale:
            cmd += ["-vf", f"setpts=PTS-STARTPTS,scale=-2:min({max_output_height},ih)"]
        else:
            cmd += ["-vf", "setpts=PTS-STARTPTS"]
    else:
        # AVC, no downscale → direct copy (zero CPU)
        cmd += ["-c:v", "copy"]

    # ---- HLS output ----
    playlist_path = os.path.join(tmp_dir, "playlist.m3u8")
    cmd += [
        "-max_interleave_delta", "5000000",
        "-avoid_negative_ts", "make_zero",
        "-f", "hls",
        "-hls_init_time", str(initial_segment_duration),
        "-hls_time", str(segment_duration),
        "-hls_list_size", "0",
        "-hls_playlist_type", "event",
        "-hls_flags", "independent_segments+delete_segments",
        "-hls_segment_filename", os.path.join(tmp_dir, "seg_%03d.ts"),
        playlist_path,
    ]

    return cmd


def format_hls_playlist(
    playlist_content: str,
    base_url: str,
) -> str:
    """
    Rewrite relative paths in a raw HLS playlist to absolute URLs.
    Ensures EVENT type for live-style progressive segment loading.
    """
    content = playlist_content
    content = content.replace('URI="init.mp4"', f'URI="{base_url}/init.mp4"')
    content = content.replace("seg_", f"{base_url}/seg_")

    if "#EXT-X-PLAYLIST-TYPE:" not in content:
        content = content.replace(
            "#EXT-X-TARGETDURATION:",
            "#EXT-X-PLAYLIST-TYPE:EVENT\n#EXT-X-TARGETDURATION:",
            1,
        )
    content = content.replace("#EXT-X-ENDLIST\n", "").replace("#EXT-X-ENDLIST", "")

    return content
