"""Zero-config extractors for easy mode.

Scrapling 0.5.1 has no readability-style article extraction:
Response.markdown(main_content_only=True) only takes <body>, it does not remove
navigation/ads. So 'article' uses a small heuristic scorer over semantic
containers plus noise-tag removal, with markdownify for clean Markdown output.
"""
from __future__ import annotations

import re
from typing import Any

NOISE_TAGS = (
    "script", "style", "noscript", "svg", "iframe", "nav", "footer",
    "aside", "header", "form", "template", "button", "select",
)
PLAIN_IGNORE = ("script", "style")

CANDIDATE_SELECTORS = (
    "article", "main", "[role=main]", ".article-content", ".article-body",
    ".article_content", ".post-content", ".entry-content", ".news-content",
    ".story-body", "#article", "#content", ".content", ".post", "body",
)
SEMANTIC_BONUS = ("article", "main", "[role=main]")
SKIP_SCHEMES = ("javascript:", "mailto:", "tel:", "data:", "#")

PRESET_COLUMNS = {
    "article": ("url", "title", "content"),
    "text": ("url", "text"),
    "links": ("page_url", "link_text", "link_url"),
}


def _text(node: Any, ignore: tuple[str, ...] = NOISE_TAGS) -> str:
    try:
        raw = str(node.get_all_text(separator="\n", strip=True, ignore_tags=ignore))
        return re.sub(r"\n{3,}", "\n\n", raw).strip()
    except Exception:
        return ""


def _title(page: Any) -> str:
    for selector in ("h1::text", "title::text"):
        try:
            value = page.css(selector).get()
            if value and str(value).strip():
                return str(value).strip()
        except Exception:
            continue
    return ""


def _to_markdown(html: str) -> str:
    try:
        from markdownify import markdownify

        return re.sub(r"\n{3,}", "\n\n", markdownify(html)).strip()
    except Exception:
        return ""


def _best_content(page: Any) -> str:
    """Pick the block that looks most like an article and return clean content."""
    best_score = -1.0
    best_node: Any = None
    for selector in CANDIDATE_SELECTORS:
        try:
            nodes = list(page.css(selector))[:5]
        except Exception:
            continue
        bonus = 300 if selector in SEMANTIC_BONUS else 0
        for node in nodes:
            text = _text(node)
            if len(text) < 120:
                continue
            try:
                link_count = len(node.css("a[href]"))
            except Exception:
                link_count = 0
            try:
                para_count = len(node.css("p"))
            except Exception:
                para_count = 0
            score = len(text) + para_count * 60 - link_count * 80 + bonus
            if score > best_score:
                best_score, best_node = score, node

    plain = _text(best_node) if best_node is not None else ""
    if not plain:
        return ""
    try:
        markdown = _to_markdown(str(best_node.html_content))
    except Exception:
        markdown = ""
    return markdown if len(markdown) >= len(plain) * 0.4 else plain


def extract_preset(page: Any, url: str, preset: str) -> list[dict[str, Any]]:
    """Return ready-to-display records; no selectors or fields required."""
    if preset == "text":
        return [{"url": url, "text": _text(page, ignore=PLAIN_IGNORE)}]

    if preset == "links":
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        try:
            anchors = page.css("a[href]")
        except Exception:
            anchors = []
        for anchor in anchors:
            try:
                href = str(anchor.attrib.get("href") or "").strip()
            except Exception:
                continue
            if not href or href.lower().startswith(SKIP_SCHEMES):
                continue
            try:
                full = page.urljoin(href)
            except Exception:
                full = href
            if not full or full in seen:
                continue
            seen.add(full)
            label = _text(anchor, ignore=PLAIN_IGNORE)
            if not label:
                try:
                    label = str(anchor.text).strip()
                except Exception:
                    label = ""
            items.append({"page_url": url, "link_text": label, "link_url": full})
        return items

    if preset == "article":
        content = _best_content(page) or _text(page)
        return [{"url": url, "title": _title(page), "content": content}]

    return []
