"""恢复前校验备份，并将旧电脑的文件路径映射到当前数据目录。"""
from __future__ import annotations

import json
import re
from pathlib import Path


def prepare_restore(root: Path, destination: Path) -> None:
    def read_json(path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"备份数据损坏：{path.name}") from exc

    def identifier(value) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("备份中的课程或课堂 ID 无效")
        return value

    def rebase(value, anchor: str) -> str:
        if not value:
            return ""
        if not isinstance(value, str):
            raise ValueError("备份中的文件路径无效")
        # 同时支持 Windows、macOS、Linux 备份，不依赖当前操作系统的路径语法。
        normalized = "/" + value.replace("\\", "/").lstrip("/")
        marker = f"/{anchor}/"
        if marker not in normalized:
            raise ValueError("备份中的文件路径不属于对应的课程或课堂")
        relative = Path(anchor) / normalized.rsplit(marker, 1)[1]
        target = (destination / relative).resolve()
        try:
            target.relative_to((destination / anchor).resolve())
        except ValueError as exc:
            raise ValueError("备份中的文件路径越界") from exc
        return str(target)

    def paths(values, anchor: str) -> dict:
        if not isinstance(values, dict):
            raise ValueError("备份中的结果文件清单无效")
        return {key: rebase(value, anchor) for key, value in values.items()}

    def write_json(path: Path, value) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    manifest = read_json(root / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("app") != "buketang":
        raise ValueError("这不像一份「不听课」的备份包")
    if (root / "config.json").exists():
        settings = read_json(root / "config.json")
        if not isinstance(settings, dict) or any(key in settings and not isinstance(settings[key], dict) for key in ("llm", "guide")):
            raise ValueError("备份中的配置无效")
        if settings.get("asr") is not None and not isinstance(settings["asr"], dict):
            raise ValueError("备份中的语音配置无效")

    for path in (root / "lessons").glob("*/lesson.json"):
        lesson = read_json(path)
        if not isinstance(lesson, dict) or identifier(lesson.get("id")) != path.parent.name:
            raise ValueError("备份中的课堂数据无效")
        anchor = f"lessons/{lesson['id']}"
        for collection in ("materials", "audios"):
            items = lesson.get(collection, [])
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ValueError("备份中的素材清单无效")
            for item in items:
                for key in ("path", "pdf_path"):
                    if key in item:
                        item[key] = rebase(item[key], anchor)
        lesson["guides"] = paths(lesson.get("guides", {}), anchor)
        revision = lesson.get("current_revision")
        if revision and not re.fullmatch(r"rev_[a-f0-9]{12}", str(revision)):
            raise ValueError("备份中的结果版本无效")
        for previous in lesson.get("history", []):
            if not isinstance(previous, dict):
                raise ValueError("备份中的历史结果无效")
            previous["guides"] = paths(previous.get("guides", {}), anchor)
        write_json(path, lesson)

    course_file = root / "courses.json"
    if course_file.exists():
        data = read_json(course_file)
        courses = data.get("courses") if isinstance(data, dict) else data
        if not isinstance(courses, list):
            raise ValueError("备份中的课程清单无效")
        for course in courses:
            if not isinstance(course, dict):
                raise ValueError("备份中的课程数据无效")
            cid = identifier(course.get("id"))
            summary = course.get("summary")
            if summary:
                if not isinstance(summary, dict):
                    raise ValueError("备份中的总复习数据无效")
                summary["files"] = paths(summary.get("files", {}), f"courses/{cid}")
        write_json(course_file, data)
