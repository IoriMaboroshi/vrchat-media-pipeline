"""
Bandwidth-based admission control for auto quality degradation.
Tracks active FFmpeg streaming sessions and estimates total bandwidth usage.
When bandwidth exceeds the configured limit, degrades quality for new connections.
"""

import time
import uuid
from typing import Dict, Optional, Tuple

# Estimated Mbps per quality level
QUALITY_BANDWIDTH_MAP: Dict[int, float] = {
    127: 80.0,  # 8K
    126: 35.0,  # Dolby Vision
    125: 35.0,  # HDR
    120: 30.0,  # 4K
    122: 22.0,  # 1440P HDR
    114: 18.0,  # 1080P HDR
    112: 15.0,  # 1080P 60fps
    80: 10.0,   # 1080P 高码率
    64: 8.0,    # 1080P
    32: 4.0,    # 720P
    16: 1.5,    # 480P
    8: 0.7,     # 360P
}

HIRES_AUDIO_BANDWIDTH = 3.0  # Estimated extra Mbps for Hi-Res/lossless audio
STREAM_TTL = 60  # Remove streams inactive for 60 seconds

_active_streams: Dict[str, dict] = {}


def register_stream(stream_id: Optional[str] = None, qn: int = 64, hires: bool = False) -> str:
    """Register a new active stream. Returns the stream_id."""
    if stream_id is None:
        stream_id = uuid.uuid4().hex[:12]

    estimated_mbps = QUALITY_BANDWIDTH_MAP.get(qn, 8.0)
    if hires:
        estimated_mbps += HIRES_AUDIO_BANDWIDTH

    _active_streams[stream_id] = {
        "qn": qn,
        "hires": hires,
        "started": time.time(),
        "estimated_mbps": estimated_mbps,
    }
    return stream_id


def unregister_stream(stream_id: str) -> None:
    """Remove a stream from tracking."""
    _active_streams.pop(stream_id, None)


def get_total_bandwidth_mbps() -> float:
    """Calculate total estimated bandwidth of all active streams."""
    cleanup_expired_streams()
    return sum(s["estimated_mbps"] for s in _active_streams.values())


def get_active_count() -> int:
    """Return number of currently active streams."""
    cleanup_expired_streams()
    return len(_active_streams)


def cleanup_expired_streams(ttl: int = STREAM_TTL) -> None:
    """Remove streams that haven't been active recently."""
    now = time.time()
    expired = [sid for sid, s in _active_streams.items() if now - s["started"] > ttl]
    for sid in expired:
        _active_streams.pop(sid, None)


def should_degrade(
    target_qn: int,
    enable_hires: bool,
    max_bandwidth_mbps: float,
    quality_fallback_order: list,
    hdr_qualities: set,
    allow_4k: bool = True,
    allow_8k: bool = False,
) -> Tuple[int, bool]:
    """
    Determine if quality should be degraded based on current bandwidth usage.
    Returns (adjusted_qn, adjusted_hires).
    
    Degradation order:
    1. Disable Hi-Res audio
    2. Step down video quality following the fallback order
    """
    from config import QUALITY_BANDWIDTH_MAP as BW_MAP

    current_total = get_total_bandwidth_mbps()
    new_estimated = BW_MAP.get(target_qn, 8.0)
    if enable_hires:
        new_estimated += HIRES_AUDIO_BANDWIDTH

    # If within limits, no degradation needed
    if current_total + new_estimated <= max_bandwidth_mbps:
        return target_qn, enable_hires

    # Try disabling Hi-Res first
    if enable_hires:
        new_estimated_no_hires = BW_MAP.get(target_qn, 8.0)
        if current_total + new_estimated_no_hires <= max_bandwidth_mbps:
            return target_qn, False

    # If still over, step down quality
    try:
        start_idx = quality_fallback_order.index(target_qn)
    except ValueError:
        start_idx = 4  # Default to 1080p position

    for i in range(start_idx + 1, len(quality_fallback_order)):
        qn = quality_fallback_order[i]
        if qn == 127 and not allow_8k:
            continue
        if qn == 120 and not allow_4k:
            continue
        if qn in hdr_qualities:
            continue

        step_estimated = BW_MAP.get(qn, 8.0)
        # Without Hi-Res since we're degrading
        if current_total + step_estimated <= max_bandwidth_mbps:
            return qn, False

    # Fallback to lowest quality
    return quality_fallback_order[-1], False
