"""
Bilibili video URL resolution (DASH).
Simplified for VRChat HLS proxy: AVC priority, no HDR/8K/Dolby.
"""

from typing import Optional, List

import httpx

from config import (
    BILIBILI_UA,
    BILIBILI_REFERER,
    QUALITY_MAP,
    QUALITY_FALLBACK_ORDER,
    DEFAULT_QN,
    DEFAULT_QX,
)
from bilibili.wbi import sign_params
from bilibili.auth import get_current_cookies, load_cookies

# Codec constants
CODECID_AVC = 7
CODECID_HEVC = 12
CODECID_AV1 = 13

# Max resolution per quality name (for strict AVC stream filtering)
QUALITY_MAX_HEIGHT: dict[str, int] = {
    "4k": 4320,
    "1080p60": 1080,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
}


async def get_video_info(bvid: str, cid: Optional[int] = None) -> dict:
    """Get basic video info (title, duration, pages)."""
    cookies = get_current_cookies() or load_cookies()

    async with httpx.AsyncClient(timeout=15, cookies=cookies) as client:
        resp = await client.get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
            headers={
                "User-Agent": BILIBILI_UA,
                "Referer": BILIBILI_REFERER,
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            return {"error": data.get("message", "获取视频信息失败")}

        info = data.get("data", {})
        if not info:
            return {"error": "未找到视频信息"}

        result = {
            "bvid": info["bvid"],
            "aid": info.get("aid", 0),
            "title": info["title"],
            "duration": info["duration"],
            "cover": info["pic"],
            "owner": {
                "name": info["owner"]["name"],
                "mid": info["owner"]["mid"],
            },
            "stat": {
                "view": info["stat"]["view"],
                "danmaku": info["stat"]["danmaku"],
            },
            "pages": [],
            "videos": info.get("videos", 1),
        }

        pages = info.get("pages", [])
        if pages:
            result["pages"] = []
            for p in pages:
                page_info = {
                    "cid": p["cid"],
                    "page": p["page"],
                    "part": p.get("part", ""),
                    "duration": p.get("duration", 0),
                }
                result["pages"].append(page_info)

        if cid is not None:
            for p in pages:
                if p["cid"] == cid:
                    result["current_cid"] = cid
                    result["current_page"] = p["page"]
                    result["current_part"] = p.get("part", "")
                    result["current_duration"] = p.get("duration", info["duration"])
                    break

        return result


async def get_video_pages(bvid: str) -> dict:
    """Get list of pages (cid, title, duration, page number)."""
    cookies = get_current_cookies() or load_cookies()

    async with httpx.AsyncClient(timeout=15, cookies=cookies) as client:
        resp = await client.get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
            headers={
                "User-Agent": BILIBILI_UA,
                "Referer": BILIBILI_REFERER,
            },
        )
        data = resp.json()
        if data["code"] != 0:
            return {"error": data.get("message", "获取视频信息失败")}

        info = data["data"]
        pages = info.get("pages", [])
        result = {
            "bvid": bvid,
            "title": info["title"],
            "videos": len(pages),
            "pages": [],
        }
        for p in pages:
            result["pages"].append({
                "cid": p["cid"],
                "page": p["page"],
                "part": p.get("part", ""),
                "duration": p.get("duration", 0),
            })
        return result


async def get_season_info(season_id: str) -> dict:
    """Get season info and episode list."""
    cookies = get_current_cookies() or load_cookies()

    async with httpx.AsyncClient(timeout=15, cookies=cookies) as client:
        resp = await client.get(
            "https://api.bilibili.com/pgc/view/web/season",
            params={"season_id": season_id},
            headers={
                "User-Agent": BILIBILI_UA,
                "Referer": BILIBILI_REFERER,
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            return {"error": data.get("message", "获取剧集信息失败")}

        info = data.get("result") or data.get("data")
        if not info:
            return {"error": "剧集数据为空"}
        result = {
            "season_id": info.get("season_id", season_id),
            "title": info.get("title", ""),
            "season_title": info.get("season_title", ""),
            "total": info.get("total", 0),
            "episodes": [],
        }

        episodes = info.get("episodes", [])
        for ep in episodes:
            result["episodes"].append({
                "aid": ep.get("aid", 0),
                "bvid": ep.get("bvid", ""),
                "cid": ep.get("cid", 0),
                "title": ep.get("title", ""),
                "long_title": ep.get("long_title", ""),
                "duration": ep.get("duration", 0),
                "ep_id": ep.get("ep_id", 0),
                "index": ep.get("index", ""),
            })

        return result


async def get_episode_info(ep_id: str) -> dict:
    """Get episode info for a specific episode ID, resolves to aid + cid."""
    import re
    cookies = get_current_cookies() or load_cookies()

    async with httpx.AsyncClient(timeout=15, cookies=cookies) as client:
        resp = await client.get(
            "https://api.bilibili.com/pgc/view/web/season",
            params={"ep_id": ep_id},
            headers={
                "User-Agent": BILIBILI_UA,
                "Referer": BILIBILI_REFERER,
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            return {"error": data.get("message", "获取剧集信息失败")}

        info = data.get("result") or data.get("data", {})
        if not info or not info.get("episodes"):
            try:
                page_resp = await client.get(
                    f"https://www.bilibili.com/bangumi/play/ep{ep_id}",
                    headers={
                        "User-Agent": BILIBILI_UA,
                        "Referer": BILIBILI_REFERER,
                    },
                )
                html = page_resp.text
                bvid_m = re.search(r'"bvid"\s*:\s*"([^"]+)"', html)
                cid_m = re.search(r'"cid"\s*:\s*(\d+)', html)
                aid_m = re.search(r'"aid"\s*:\s*(\d+)', html)
                if bvid_m and cid_m:
                    return {
                        "current": {
                            "bvid": bvid_m.group(1),
                            "cid": int(cid_m.group(1)),
                            "aid": int(aid_m.group(1)) if aid_m else 0,
                            "ep_id": ep_id,
                        },
                        "episodes": [{
                            "bvid": bvid_m.group(1),
                            "cid": int(cid_m.group(1)),
                            "aid": int(aid_m.group(1)) if aid_m else 0,
                            "ep_id": ep_id,
                        }],
                    }
            except Exception:
                pass
            return {"error": "未找到剧集信息 (ep_id=" + str(ep_id) + ")"}

        result = {
            "season_id": info.get("season_id", 0),
            "title": info.get("title", ""),
            "episodes": [],
        }

        episodes = info.get("episodes", [])
        for ep in episodes:
            ep_data = {
                "aid": ep.get("aid", 0),
                "bvid": ep.get("bvid", ""),
                "cid": ep.get("cid", 0),
                "title": ep.get("title", ""),
                "long_title": ep.get("long_title", ""),
                "duration": ep.get("duration", 0),
                "ep_id": ep.get("ep_id", 0),
                "index": ep.get("index", ""),
            }
            result["episodes"].append(ep_data)

            if str(ep.get("ep_id", 0)) == str(ep_id):
                result["current"] = ep_data

        return result


async def get_play_url(bvid: str, qx: str = "1080p") -> dict:
    """
    Get DASH video + audio URLs for given BVID with desired quality.
    Returns {video_url, audio_url, quality, codecid, ...}
    Always prefers AVC (H.264) streams for maximum compatibility.
    """
    cookies = get_current_cookies() or load_cookies()
    qn = QUALITY_MAP.get(qx, DEFAULT_QN)

    # fnval: 16=DASH, 64=4K, 2048=AV1
    fnval = 16 | 64 | 2048

    params = await sign_params({
        "bvid": bvid,
        "qn": qn,
        "fnval": fnval,
        "fnver": 0,
        "fourk": 1,
    })

    async with httpx.AsyncClient(timeout=15, cookies=cookies) as client:
        resp = await client.get(
            "https://api.bilibili.com/x/player/wbi/playurl",
            params=params,
            headers={
                "User-Agent": BILIBILI_UA,
                "Referer": BILIBILI_REFERER,
            },
        )
        data = resp.json()
        if data["code"] != 0:
            return {"error": data.get("message", "获取播放地址失败")}

        result = data["data"]
        dash = result.get("dash", {})
        video_url = ""
        audio_url = ""
        selected_codecid = 0

        if dash:
            video_list = dash.get("video", [])
            audio_list = dash.get("audio", [])

            # Pick highest quality video stream, prefer AVC (codecid=7)
            max_height = QUALITY_MAX_HEIGHT.get(qx, 4320)
            if video_list:
                avc_streams = [v for v in video_list if v.get("codecid") == CODECID_AVC and v.get("height", 9999) <= max_height]
                if avc_streams:
                    chosen = avc_streams[0]
                else:
                    chosen = video_list[0]
                video_url = chosen.get("baseUrl", chosen.get("base_url", ""))
                selected_codecid = chosen.get("codecid", 0)

            # Audio: pick highest bandwidth
            if audio_list:
                audio_sorted = sorted(audio_list, key=lambda x: x.get("bandwidth", 0), reverse=True)
                audio_url = audio_sorted[0].get("baseUrl", audio_sorted[0].get("base_url", ""))

        # Fallback to single MP4 if no DASH
        if not video_url:
            durl = result.get("durl", [])
            if durl:
                video_url = durl[0].get("url", "")

        current_qn = result.get("quality", 0)

        return {
            "video_url": video_url,
            "audio_url": audio_url,
            "codecid": selected_codecid,
            "quality": current_qn,
            "requested_qx": qx,
            "requested_qn": qn,
        }


async def get_play_url_comprehensive(
    bvid: str,
    cid: Optional[int] = None,
    qx: str = "1080p",
    qn_override: Optional[int] = None,
) -> dict:
    """
    Get DASH URLs with quality fallback.
    If qn_override is given, uses that qn directly without qx mapping.
    Falls back through quality levels if a codec/quality is unavailable.
    Always prefers AVC (H.264).
    """
    cookies = get_current_cookies() or load_cookies()

    # Resolve cid if not provided
    resolved_cid = cid
    title = ""
    duration = 0

    if resolved_cid is None:
        async with httpx.AsyncClient(timeout=15, cookies=cookies) as client:
            resp = await client.get(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": bvid},
                headers={
                    "User-Agent": BILIBILI_UA,
                    "Referer": BILIBILI_REFERER,
                },
            )
            view_data = resp.json()
            if view_data["code"] != 0:
                return {"error": view_data.get("message", "获取视频信息失败")}
            vinfo = view_data["data"]
            title = vinfo.get("title", "")
            pages = vinfo.get("pages", [])
            if pages:
                resolved_cid = pages[0]["cid"]
                duration = pages[0].get("duration", vinfo.get("duration", 0))
            else:
                return {"error": "视频没有分P信息"}

    # Build quality fallback list from requested quality down
    target_qn = qn_override if qn_override is not None else QUALITY_MAP.get(qx, DEFAULT_QN)
    fallback_qualities: List[int] = []

    try:
        start_idx = QUALITY_FALLBACK_ORDER.index(target_qn)
    except ValueError:
        start_idx = QUALITY_FALLBACK_ORDER.index(DEFAULT_QN)

    for i in range(start_idx, len(QUALITY_FALLBACK_ORDER)):
        fallback_qualities.append(QUALITY_FALLBACK_ORDER[i])

    # Try each quality until one succeeds
    all_available: List[int] = []
    best_result = None

    for qn in fallback_qualities:
        fnval = 16 | 64 | 2048

        params = await sign_params({
            "bvid": bvid,
            "cid": resolved_cid,
            "qn": qn,
            "fnval": fnval,
            "fnver": 0,
            "fourk": 1,
        })

        try:
            async with httpx.AsyncClient(timeout=15, cookies=cookies) as client:
                resp = await client.get(
                    "https://api.bilibili.com/x/player/wbi/playurl",
                    params=params,
                    headers={
                        "User-Agent": BILIBILI_UA,
                        "Referer": BILIBILI_REFERER,
                    },
                )
                play_data = resp.json()
                if play_data["code"] != 0:
                    continue

                result = play_data["data"]
                dash = result.get("dash", {})
                video_url = ""
                audio_url = ""
                selected_codecid = 0

                if dash:
                    video_list = dash.get("video", [])
                    audio_list = dash.get("audio", [])

                    if video_list:
                        max_height = QUALITY_MAX_HEIGHT.get(qx, 4320)
                        avc_streams = [v for v in video_list if v.get("codecid") == CODECID_AVC and v.get("height", 9999) <= max_height]
                        if avc_streams:
                            chosen = avc_streams[0]
                        else:
                            # No AVC within height cap — take lowest height available
                            sorted_by_height = sorted(video_list, key=lambda v: v.get("height", 9999))
                            chosen = sorted_by_height[0]
                        video_url = chosen.get("baseUrl", chosen.get("base_url", ""))
                        selected_codecid = chosen.get("codecid", 0)

                    if audio_list:
                        audio_sorted = sorted(audio_list, key=lambda x: x.get("bandwidth", 0), reverse=True)
                        audio_url = audio_sorted[0].get("baseUrl", audio_sorted[0].get("base_url", ""))

                if not video_url:
                    durl = result.get("durl", [])
                    if durl:
                        video_url = durl[0].get("url", "")

                current_qn = result.get("quality", qn)
                accept_quality = result.get("accept_quality", [])

                if accept_quality and not all_available:
                    all_available = accept_quality

                if video_url:
                    best_result = {
                        "video_url": video_url,
                        "audio_url": audio_url,
                        "codecid": selected_codecid,
                        "actual_qn": current_qn,
                        "requested_qx": qx,
                        "all_available_qualities": all_available if all_available else accept_quality,
                        "cid": resolved_cid,
                        "duration": duration,
                        "title": title,
                    }
                    # Resolve title/duration if missing (e.g., from direct cid call)
                    if not title:
                        try:
                            async with httpx.AsyncClient(timeout=15, cookies=cookies) as client2:
                                vresp = await client2.get(
                                    "https://api.bilibili.com/x/web-interface/view",
                                    params={"bvid": bvid},
                                    headers={"User-Agent": BILIBILI_UA, "Referer": BILIBILI_REFERER},
                                )
                                vdata = vresp.json()
                                if vdata["code"] == 0:
                                    best_result["title"] = vdata["data"].get("title", "")
                                    best_result["duration"] = vdata["data"].get("duration", 0)
                                    for p in vdata["data"].get("pages", []):
                                        if p.get("cid") == resolved_cid:
                                            best_result["duration"] = p.get("duration", best_result["duration"])
                                            break
                        except Exception:
                            pass
                    break
        except Exception:
            continue

    if not best_result:
        return {"error": "所有清晰度均不可用，请检查账号权限或视频状态"}

    return best_result
