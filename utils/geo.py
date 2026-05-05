"""
IP geo-location lookup using multi-source fallback chain.
Sources: ip.sb → ip-api.com → ipip.net → httpbin.org
"""

import time
import asyncio
import json
import logging
from typing import Optional

import httpx

logger = logging.getLogger("bilibili-proxy.geo")

# Cache: {ip: (result_dict, expiry_timestamp)}
_geo_cache: dict = {}
_CACHE_TTL = 86400  # 24 hours
_FAIL_CACHE_TTL = 3600  # 1 hour for failures

# Unified result format: {"country": "中国", "region": "广东", "city": "深圳", "isp": "阿里云"}
# or {"error": "unknown"}


async def _query_ip_sb(ip: str, timeout: float = 3.0) -> Optional[dict]:
    """Query ip.sb geo API."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"https://api.ip.sb/geoip/{ip}",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "country": data.get("country", ""),
                    "region": data.get("region", ""),
                    "city": data.get("city", ""),
                    "isp": data.get("organization", data.get("isp", "")),
                }
    except Exception as e:
        logger.debug(f"ip.sb failed for {ip}: {e}")
    return None


async def _query_ip_api(ip: str, timeout: float = 3.0) -> Optional[dict]:
    """Query ip-api.com with fields."""
    endpoints = [
        f"http://ip-api.com/json/{ip}?lang=zh-CN&fields=country,city,regionName,isp",
        f"http://ip-api.cn/json/{ip}?lang=zh-CN&fields=country,city,regionName,isp",
    ]
    for url in endpoints:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "success":
                        return {
                            "country": data.get("country", ""),
                            "region": data.get("regionName", ""),
                            "city": data.get("city", ""),
                            "isp": data.get("isp", ""),
                        }
        except Exception as e:
            logger.debug(f"ip-api failed for {ip} ({url}): {e}")
    return None


async def _query_ipip(ip: str, timeout: float = 3.0) -> Optional[dict]:
    """Query ipip.net free API."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"https://freeapi.ipip.net/{ip}",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code == 200:
                data = resp.json()
                # ipip returns [country, region, city, isp_detail, ...]
                if isinstance(data, list) and len(data) >= 4:
                    return {
                        "country": data[0] or "",
                        "region": data[1] or "",
                        "city": data[2] or "",
                        "isp": data[3] or "",
                    }
                elif isinstance(data, dict):
                    # Sometimes returns dict format
                    return {
                        "country": data.get("country_name", "") or data.get("country", ""),
                        "region": data.get("region_name", "") or data.get("region", ""),
                        "city": data.get("city_name", "") or data.get("city", ""),
                        "isp": data.get("isp_domain", "") or data.get("isp", ""),
                    }
    except Exception as e:
        logger.debug(f"ipip.net failed for {ip}: {e}")
    return None


async def _query_httpbin(ip: str, timeout: float = 3.0) -> Optional[dict]:
    """Query httpbin.org for IP (returns own IP, but we use as fallback)."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"https://httpbin.org/ip",
            )
            if resp.status_code == 200:
                data = resp.json()
                origin = data.get("origin", "")
                # httpbin only tells us the origin IP, not geo data
                # We just verify the IP is valid
                if origin and ip in origin:
                    return {"country": "", "region": "", "city": "", "isp": ""}
    except Exception as e:
        logger.debug(f"httpbin.org failed for {ip}: {e}")
    return None


async def lookup_ip(ip: str) -> str:
    """
    Lookup IP geo-location using multi-source fallback chain.
    
    Fallback order: ip.sb → ip-api.com → ipip.net → httpbin.org
    Each source has 3 second timeout.
    
    Returns formatted string like "中国 广东 深圳" or "未知" on complete failure.
    """
    # Skip local/private IPs
    if ip in ("127.0.0.1", "localhost", "::1") or ip.startswith(("10.", "172.16.", "172.17.", "172.18.", "172.19.", "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.", "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.", "192.168.")):
        return "内网"

    # Check cache
    now = time.time()
    if ip in _geo_cache:
        cached_result, expiry = _geo_cache[ip]
        if now < expiry:
            return cached_result

    # Multi-source fallback chain
    fallback_chain = [
        ("ip.sb", _query_ip_sb),
        ("ip-api.com", _query_ip_api),
        ("ipip.net", _query_ipip),
        ("httpbin.org", _query_httpbin),
    ]

    result_str = "未知"
    for source_name, query_fn in fallback_chain:
        try:
            data = await query_fn(ip, timeout=3.0)
            if data and data.get("country"):
                parts = [
                    data.get("country", ""),
                    data.get("region", ""),
                    data.get("city", ""),
                ]
                result_str = " ".join(filter(None, parts))
                if result_str:
                    # Cache successful result
                    _geo_cache[ip] = (result_str, now + _CACHE_TTL)
                    return result_str
            elif data:
                # Source responded but no useful geo data
                logger.debug(f"{source_name} responded for {ip} but no geo data")
        except Exception as e:
            logger.debug(f"{source_name} exception for {ip}: {e}")

    # Cache "未知" to avoid repeated lookups
    _geo_cache[ip] = ("未知", now + _FAIL_CACHE_TTL)
    return "未知"
