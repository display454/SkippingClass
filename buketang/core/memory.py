"""Course-scoped evidence retrieval, rebuilt from current published sources."""
from __future__ import annotations

import re
from shared.transcript import fingerprint


def terms(value: str) -> set[str]:
    text = re.sub(r"\s+", "", value.lower())
    return set(re.findall(r"[a-z][a-z0-9_]+", text)) | {text[i:i + 2] for i in range(len(text) - 1)}


def index(course: dict, lessons: list[dict]) -> dict:
    sources = []
    ordered = sorted((l for l in lessons if l.get("course_id") == course["id"]),
                     key=lambda l: (l.get("sequence") or 0, l.get("created", 0), l["id"]))
    for position, lesson in enumerate(ordered):
        for point in (lesson.get("analysis") or {}).get("key_points", []):
            if not point.get("pages") and not point.get("timestamp"):
                continue
            sources.append({"lesson_id": lesson["id"], "lesson_name": lesson["name"],
                            "revision": lesson.get("current_revision", "legacy"),
                            "order": position, "title": point["title"],
                            "detail": point.get("detail", "")[:1600],
                            "kind": "correction" if re.search(r"更正|纠正|修正|说错", point.get("detail", "")) else "knowledge",
                            "formulas": point.get("formulas", []),
                            "pages": point.get("pages", []), "timestamp": point.get("timestamp", "")})
        for emphasis in (lesson.get("analysis") or {}).get("emphasis", []):
            if not emphasis.get("verified") or not emphasis.get("timestamp"):
                continue
            sources.append({"lesson_id": lesson["id"], "lesson_name": lesson["name"],
                            "revision": lesson.get("current_revision", "legacy"), "order": position,
                            "title": "教师强调", "detail": emphasis["quote"], "formulas": [], "pages": [],
                            "timestamp": emphasis["timestamp"], "kind": "exam" if re.search(r"考试|不考|作业", emphasis["quote"]) else "emphasis"})
    return {"snapshot": fingerprint([sources, course.get("memory_notes", "")]),
            "enabled": course.get("memory_enabled", True), "notes": course.get("memory_notes", ""),
            "lessons": [l["id"] for l in ordered], "entries": sources}


def retrieve(lesson: dict, course: dict | None, lessons: list[dict], query: str, budget: int = 3000) -> dict:
    if not course or not course.get("memory_enabled", True):
        return {"snapshot": "", "text": "", "sources": []}
    data = index(course, lessons)
    position = data["lessons"].index(lesson["id"]) if lesson["id"] in data["lessons"] else 0
    words = terms(query[:20000])
    candidates = [item for item in data["entries"] if item["lesson_id"] != lesson["id"]
                  and (item["order"] < position or lesson.get("include_later_memory", False))]
    candidates.sort(key=lambda item: (len(words & terms(item["title"] + item["detail"])), item["order"]), reverse=True)
    selected, blocks = [], []
    remaining = max(0, budget)
    notes = f"用户术语约定（非教师原话）：{data['notes'][:800]}" if data["notes"] else ""
    if notes:
        blocks.append(notes)
        remaining -= len(notes)
    for item in candidates[:8]:
        block = (f"[历史课堂 {item['lesson_id']}｜{item['lesson_name']}] {item['title']}\n"
                 f"{item['detail']}\n公式：{item['formulas']}\n出处页：{item['pages']}，录音：{item['timestamp']}")
        if len(block) > remaining:
            continue
        remaining -= len(block)
        blocks.append(block)
        selected.append(item)
    return {"snapshot": data["snapshot"], "text": "\n\n".join(blocks), "sources": selected}
