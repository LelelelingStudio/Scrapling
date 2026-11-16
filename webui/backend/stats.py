"""Normalize Scrapling's CrawlStats into the small payload the UI renders."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .rules import host_of


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "response_bytes": int(raw.get("response_bytes", 0) or 0),
        "requests": int(raw.get("requests_count", 0) or 0),
        "failed": int(raw.get("failed_requests_count", 0) or 0),
        "items": int(raw.get("items_scraped", 0) or 0),
        "per_domain": dict(raw.get("domains_response_bytes", {}) or {}),
        "status_count": dict(raw.get("response_status_count", {}) or {}),
        # CrawlStats.elapsed_seconds is end_time - start_time, which is negative
        # while the crawl is still running (end_time is 0 until it finishes).
        "elapsed": max(0.0, float(raw.get("elapsed_seconds", 0) or 0)),
    }


def empty() -> dict[str, Any]:
    return {
        "response_bytes": 0,
        "requests": 0,
        "failed": 0,
        "items": 0,
        "per_domain": {},
        "status_count": {},
        "elapsed": 0.0,
    }


def single(pages: list[tuple[str, int]], item_count: int, failed: int = 0) -> dict[str, Any]:
    """Aggregate single-page mode, which may fetch several start_urls."""
    per_domain: dict[str, int] = {}
    total = 0
    for url, byte_count in pages:
        domain = host_of(url) or urlparse(url).netloc or "unknown"
        per_domain[domain] = per_domain.get(domain, 0) + byte_count
        total += byte_count
    return {
        "response_bytes": total,
        "requests": len(pages),
        "failed": failed,
        "items": item_count,
        "per_domain": per_domain,
        "status_count": {},
        "elapsed": 0.0,
    }


def limits_reached(stats: dict[str, Any], limits: Any, check_domains: bool = True) -> str | None:
    if stats["requests"] >= limits.max_pages:
        return f"达到最大页数 {limits.max_pages}"
    if stats["response_bytes"] >= limits.max_bytes:
        return f"达到最大流量 {limits.max_bytes} 字节"
    if check_domains and len(stats["per_domain"]) >= limits.max_domains:
        return f"达到最大域名数 {limits.max_domains}"
    return None
