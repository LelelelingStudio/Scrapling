"""Render scraped items to JSON / JSONL / CSV / XML."""
from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

FORMATS = ("json", "jsonl", "csv", "xml")
ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _xml_tag(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name))
    if not cleaned or not re.match(r"[A-Za-z_]", cleaned[0]):
        cleaned = "f_" + cleaned
    return cleaned


def render(items: list[dict[str, Any]], fmt: str) -> tuple[bytes, str]:
    if fmt not in FORMATS:
        raise ValueError(f"不支持的导出格式: {fmt}")

    if fmt == "json":
        return json.dumps(items, ensure_ascii=False, indent=2).encode("utf-8"), "application/json"

    if fmt == "jsonl":
        lines = [json.dumps(item, ensure_ascii=False) for item in items]
        body = "\n".join(lines) + ("\n" if lines else "")
        return body.encode("utf-8"), "application/x-ndjson"

    if fmt == "csv":
        keys: list[str] = []
        for item in items:
            for key in item:
                if key not in keys:
                    keys.append(key)
        if not keys:
            keys = ["(empty)"]
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        for item in items:
            writer.writerow({key: _cell(item.get(key)) for key in keys})
        return buffer.getvalue().encode("utf-8-sig"), "text/csv"

    root = ET.Element("items")
    for item in items:
        node = ET.SubElement(root, "item")
        for key, value in item.items():
            child = ET.SubElement(node, _xml_tag(key))
            child.text = _cell(value)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), "application/xml"


def save(task_id: str, items: list[dict[str, Any]], fmt: str) -> Path:
    content, _ = render(items, fmt)
    folder = OUTPUT / task_id
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"result.{fmt}"
    target.write_bytes(content)
    return target
