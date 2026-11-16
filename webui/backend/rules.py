"""JSON rule spec, URL scoping and the postprocess_js sandbox."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import shutil
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

SANDBOX = Path(__file__).with_name("js_sandbox.js")


class FieldRule(BaseModel):
    selector: str = Field(min_length=1)
    type: Literal["string", "number", "array_string", "array_number", "html"] = "string"
    postprocess_js: str | None = None


class Limits(BaseModel):
    # Big-crawl defaults: 500 pages need a much larger byte budget, otherwise
    # the 8 MB brake used to stop the crawl after ~80 pages.
    max_pages: int = Field(default=500, ge=1, le=10000)
    max_bytes: int = Field(default=200_000_000, ge=1)
    max_domains: int = Field(default=10, ge=1, le=1000)
    concurrent_requests: int = Field(default=5, ge=1, le=50)
    download_delay: float = Field(default=0.5, ge=0.0, le=60.0)
    robots_txt_obey: bool = True


class Spec(BaseModel):
    mode: Literal["single", "spider"] = "single"
    preset: Literal["article", "text", "links", "custom"] | None = None
    scope: Literal["same_site", "discover_domains"] = "same_site"
    same_site_mode: Literal["host", "registered_domain"] = "host"
    start_urls: list[str] = Field(min_length=1)
    # Easy-mode presets need no fields at all; custom mode still requires them.
    fields: dict[str, FieldRule] = Field(default_factory=dict)
    limits: Limits = Field(default_factory=Limits)
    browser: bool = False
    container: str | None = None
    follow: bool = True
    # Persist the spider queue under crawl_data/<host>/ so the next run resumes.
    # Only spider mode can use it (single-page mode has no Spider instance).
    crawl_resume: bool = False
    domain_allowlist: list[str] = Field(default_factory=list)
    domain_blocklist: list[str] = Field(default_factory=list)

    @field_validator("start_urls")
    @classmethod
    def _check_urls(cls, urls: list[str]) -> list[str]:
        cleaned = [u.strip() for u in urls if u.strip()]
        if not cleaned:
            raise ValueError("start_urls 不能为空")
        for url in cleaned:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError(f"起始 URL 必须是 http/https 完整地址: {url}")
            if is_private_host(parsed.hostname):
                raise ValueError(f"不允许抓取本机/内网地址: {url}")
        return cleaned

    @model_validator(mode="after")
    def _check_fields(self) -> "Spec":
        if (not self.preset or self.preset == "custom") and not self.fields:
            raise ValueError("自定义模式必须提供 fields 字段规则")
        return self


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def registered_domain(host: str) -> str:
    try:
        from tld import get_fld

        return (get_fld(host, fail_silently=True) or host).lower()
    except Exception:
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host


def is_private_host(host: str) -> bool:
    host = host.strip().lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain", "ip6-localhost"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return bool(
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast
        )
    except ValueError:
        pass
    return host.endswith((".local", ".internal", ".localdomain"))


def _matches_domain(host: str, pattern: str) -> bool:
    pattern = pattern.lower().lstrip(".")
    return host == pattern or host.endswith("." + pattern)


def url_allowed(url: str, spec: Spec, start_host: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host or is_private_host(host):
        return False

    if spec.scope == "same_site":
        if spec.same_site_mode == "host":
            return host == start_host
        return registered_domain(host) == registered_domain(start_host)

    if spec.domain_blocklist and any(_matches_domain(host, p) for p in spec.domain_blocklist):
        return False
    if spec.domain_allowlist and not any(_matches_domain(host, p) for p in spec.domain_allowlist):
        return False
    return True


def allowed_domains_for(spec: Spec, start_host: str) -> set[str]:
    """Spider.allowed_domains is a class attribute enforced by the engine."""
    if spec.scope != "same_site":
        return set()
    if spec.same_site_mode == "host":
        return {start_host}
    return {registered_domain(start_host)}


def _to_number(value: str) -> float | int | None:
    match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
    if not match:
        return None
    number = float(match.group())
    return int(number) if number.is_integer() else number


def _values(node: Any, rule: FieldRule) -> list[Any]:
    selected = node.css(rule.selector)
    if rule.type == "html":
        raw: list[Any] = []
        for element in selected:
            raw.append(getattr(element, "html", None) or element.get())
        values = [str(v) for v in raw]
    else:
        values = [str(v) for v in selected.getall()]

    if rule.type in ("number", "array_number"):
        return [_to_number(v) for v in values]
    return values


def _collapse(rule: FieldRule, values: list[Any]) -> Any:
    if rule.type.startswith("array"):
        return values
    return values[0] if values else None


def extract_items(page: Any, spec: Spec) -> list[dict[str, Any]]:
    """Extract records from a Response or from per-container blocks."""
    if spec.container:
        blocks = page.css(spec.container)
        return [
            {name: _collapse(rule, _values(block, rule)) for name, rule in spec.fields.items()}
            for block in blocks
        ]

    columns = {name: _values(page, rule) for name, rule in spec.fields.items()}
    count = max((len(v) for v in columns.values()), default=0)
    return [
        {
            # Pass a list (not a bare value) so _collapse keeps the declared
            # type: string -> scalar, array_* -> one-element list.
            name: _collapse(rule, columns[name][i:i + 1])
            for name, rule in spec.fields.items()
        }
        for i in range(count)
    ]


def has_postprocess(spec: Spec) -> bool:
    return any(rule.postprocess_js for rule in spec.fields.values())


async def apply_postprocess(item: dict[str, Any], spec: Spec) -> tuple[dict[str, Any], str | None]:
    """Run every field's JS transform in one Node subprocess. Returns (item, warning)."""
    payload = {
        name: {"js": rule.postprocess_js, "value": item.get(name)}
        for name, rule in spec.fields.items()
        if rule.postprocess_js
    }
    if not payload:
        return item, None

    node = shutil.which("node")
    if not node:
        return item, "未找到 node，postprocess_js 已跳过"

    try:
        proc = await asyncio.create_subprocess_exec(
            node,
            str(SANDBOX),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
            timeout=1.5,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return item, "postprocess_js 执行超时，已回退原值"
    except OSError as exc:
        return item, f"postprocess_js 启动失败: {exc}"

    try:
        parsed = json.loads(stdout.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return item, f"postprocess_js 输出异常: {(stderr or b'')[:120]!r}"

    if not parsed.get("ok"):
        return item, f"postprocess_js 出错: {parsed.get('error')}"
    item.update(parsed.get("values", {}))
    return item, None
