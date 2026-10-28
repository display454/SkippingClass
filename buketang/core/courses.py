"""课程分类：一门课下挂多堂课。

层次刻意只做两级 —— 课程 → 课堂。
学期、专业这些再往上加，对个人使用是负担：教务系统本来就按学期分了，
这里只要能把「机器学习」的 16 堂课收在一起、期末能一次看全，就够了。

「未分类」不是一个真实课程，由调用方（前端）用一个虚拟分组表示，
这样新建课堂不必强制选课程，降低第一次使用的心智成本。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from shared.storage import write_json
from typing import Any

from . import config

COURSES_PATH = config.DATA_DIR / "courses.json"

# 纸感配色：低饱和，用于侧栏的课程色点，不做大面积填充
COLORS = ["#8A6A45", "#4E6B58", "#7A5A6E", "#566079", "#8A6F35", "#5C6B48"]


class CourseStore:
    def __init__(self, path: Any = None):
        self._path = path or COURSES_PATH
        self._lock = threading.RLock()
        self._items: list[dict] = []
        self._load()

    # ---- 存储 ----

    def _load(self) -> None:
        p = self._path
        if not p.exists():
            return
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        items = raw.get("courses") if isinstance(raw, dict) else raw
        if isinstance(items, list):
            self._items = [c for c in items if isinstance(c, dict) and c.get("id")]

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        write_json(self._path, {"courses": self._items})

    def reload(self) -> None:
        """从磁盘重新读取（恢复备份后调用）。"""
        with self._lock:
            self._items = []
            self._load()

    # ---- 查询 ----

    def all(self) -> list[dict]:
        with self._lock:
            return sorted(self._items,
                          key=lambda c: (c.get("order", 0), c.get("created", 0)))

    def get(self, course_id: str) -> dict | None:
        with self._lock:
            return next((c for c in self._items if c["id"] == course_id), None)

    # ---- 写 ----

    def create(self, name: str, term: str = "", color: str = "") -> dict:
        name = (name or "").strip() or "未命名课程"
        with self._lock:
            course = {
                "id": uuid.uuid4().hex[:10],
                "name": name,
                "term": (term or "").strip(),
                "color": color or COLORS[len(self._items) % len(COLORS)],
                "order": len(self._items),
                "created": time.time(),
            }
            self._items.append(course)
            self._save()
            return dict(course)

    def update(self, course_id: str, **patch: Any) -> dict | None:
        with self._lock:
            course = self.get(course_id)
            if not course:
                return None
            for key in ("name", "term", "color"):
                if isinstance(patch.get(key), str):
                    value = patch[key].strip()
                    if key == "name" and not value:
                        continue
                    course[key] = value
            if "memory_enabled" in patch:
                course["memory_enabled"] = bool(patch["memory_enabled"])
            if isinstance(patch.get("memory_notes"), str):
                course["memory_notes"] = patch["memory_notes"][:4000]
            self._save()
            return dict(course)

    def set_summary(self, course_id: str, info: dict) -> dict | None:
        """记录最近一次「期末总复习」的产物位置，重启后还能找回来。"""
        with self._lock:
            course = self.get(course_id)
            if not course:
                return None
            course["summary"] = dict(info)
            self._save()
            return dict(course)

    def delete(self, course_id: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [c for c in self._items if c["id"] != course_id]
            if len(self._items) == before:
                return False
            self._save()
            return True


courses = CourseStore()
