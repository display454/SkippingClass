"""Lossless transcript views. Filtering never mutates the ASR source."""
from __future__ import annotations

import hashlib
import json

CATEGORIES = {"lecture": "课程讲授", "question": "相关问答", "exam": "考试提醒",
              "correction": "纠错补充", "irrelevant": "无关闲聊", "uncertain": "待判断"}


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     default=str).encode()).hexdigest()


def normalize(segments: list[dict]) -> list[dict]:
    result = []
    for i, source in enumerate(segments):
        item = dict(source)
        item.setdefault("id", f"{item.get('audio_id', 'legacy')}-s{i:06d}")
        item.setdefault("estimated_time", False)
        item.setdefault("local_start", item.get("start", 0))
        item.setdefault("local_end", item.get("end", 0))
        result.append(item)
    return result


def clock(value) -> str:
    hours, remainder = divmod(max(0, int(value)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def rows(segments: list[dict], cleaned: dict | None = None, view: str = "raw") -> list[dict]:
    annotations = (cleaned or {}).get("annotations", {})
    result = []
    for segment in normalize(segments):
        item = {**segment, **annotations.get(segment["id"], {})}
        item.setdefault("category", "uncertain")
        item.setdefault("reason", "")
        item.setdefault("keep", True)
        if view == "removed" and item["keep"]:
            continue
        if view == "clean" and not item["keep"]:
            continue
        result.append(item)
    return result


def text(segments: list[dict], cleaned: dict | None = None, view: str = "raw",
         identifiers: bool = False) -> str:
    lines = []
    for item in rows(segments, cleaned, view):
        prefix = f"[{clock(item.get('start', 0))}]"
        if identifiers:
            prefix += f" [{item['id']}]"
        if item.get("estimated_time"):
            prefix += "（时间估算）"
        value = item.get("text", "") if view == "raw" else item.get("edited_text", item.get("text", ""))
        lines.append(f"{prefix} {value}")
    return "\n".join(lines)
