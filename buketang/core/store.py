"""课堂数据模型与执行流水线。

一个「课堂」= 若干份讲义 + 若干段录音。配对关系天然成立：
1 对 1 就是一个讲义一个录音，一对多/多对多就是把它们都放进同一个课堂。

执行顺序：
解析讲义 -> 转录录音 -> 合并转写 -> LLM 分析 -> 按需渲染讲义截图 -> 生成双版本
"""
from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from shared import audio_utils as audio
from shared.asr import ASRError, Segment, build, spec_of

from . import compose, config, ingest
from .analyze import CourseContext, analyze
from .llm import LLMClient, LLMError

STATUS_LABEL = {
    "draft": "待分析",
    "parsing": "解析讲义",
    "transcribing": "转录录音",
    "analyzing": "分析中",
    "rendering": "生成指导",
    "done": "已完成",
    "failed": "失败",
    "canceled": "已取消",
}

RUNNING_STATUS = ("parsing", "transcribing", "analyzing", "rendering")


class RunCancelled(RuntimeError):
    """用户主动取消，不算失败，单独区分开。"""


# ---------------- 数据结构 ----------------

def new_lesson(name: str) -> dict[str, Any]:
    now = time.time()
    return {
        "id": uuid.uuid4().hex[:10],
        "name": name,
        "course_id": "",          # 空字符串 = 未分类
        "created": now,
        "updated": now,
        "status": "draft",
        "stage": "等待添加材料",
        "progress": 0.0,
        "materials": [],
        "audios": [],
        "transcript": [],
        "transcript_text": "",
        "analysis": None,
        "guides": {},
        "stale": False,           # 已生成结果之后又改了素材
        "error": "",
    }


def new_material(filename: str, stored: Path) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:8],
        "filename": filename,
        "path": str(stored),
        "pdf_path": "",
        "page_count": 0,
        "chars": 0,
        "pages": [],
        "can_shot": False,
        "blank_pages": [],
        "warnings": [],
        "status": "pending",
        "error": "",
    }


def new_audio(filename: str, stored: Path, size: int) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:8],
        "filename": filename,
        "path": str(stored),
        "size": size,
        "duration": 0.0,
        "segments": [],
        "status": "pending",
        "error": "",
    }


# ---------------- 存储 ----------------

class LessonStore:
    def __init__(self, workers: int = 1):
        self._lessons: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._cancel: set[str] = set()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lesson")
        self._load_all()

    def _dir(self, lesson_id: str) -> Path:
        return config.LESSON_DIR / lesson_id

    def _json_path(self, lesson_id: str) -> Path:
        return self._dir(lesson_id) / "lesson.json"

    def _load_all(self) -> None:
        if not config.LESSON_DIR.exists():
            return
        for folder in sorted(config.LESSON_DIR.iterdir(), reverse=True):
            f = folder / "lesson.json"
            if not f.exists():
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(data, dict) and data.get("id"):
                # 兼容没有课程字段的旧数据
                data.setdefault("course_id", "")
                data.setdefault("stale", False)
                if data.get("status") in ("parsing", "transcribing", "analyzing", "rendering"):
                    data["status"] = "draft"
                    data["stage"] = "上次执行被中断"
                    data["progress"] = 0
                self._lessons[data["id"]] = data

    def save(self, lesson: dict) -> None:
        with self._lock:
            lesson["updated"] = time.time()
            d = self._dir(lesson["id"])
            d.mkdir(parents=True, exist_ok=True)
            self._json_path(lesson["id"]).write_text(
                json.dumps(lesson, ensure_ascii=False), encoding="utf-8"
            )

    def update(self, lesson_id: str, **kwargs: Any) -> None:
        with self._lock:
            lesson = self._lessons.get(lesson_id)
            if not lesson:
                return
            lesson.update(kwargs)
            self.save(lesson)

    def get(self, lesson_id: str) -> dict | None:
        with self._lock:
            return self._lessons.get(lesson_id)

    def reload(self) -> None:
        """从磁盘重新读取。恢复备份、或数据目录被外部改动后调用。"""
        with self._lock:
            self._lessons.clear()
            self._cancel.clear()
            self._load_all()

    def all(self) -> list[dict]:
        with self._lock:
            return sorted(self._lessons.values(), key=lambda x: x.get("created", 0), reverse=True)

    def create(self, name: str) -> dict:
        lesson = new_lesson(name)
        with self._lock:
            self._lessons[lesson["id"]] = lesson
            self._dir(lesson["id"]).mkdir(parents=True, exist_ok=True)
            (self._dir(lesson["id"]) / "files").mkdir(exist_ok=True)
            self.save(lesson)
        return lesson

    def delete(self, lesson_id: str) -> None:
        with self._lock:
            self._lessons.pop(lesson_id, None)
        shutil.rmtree(self._dir(lesson_id), ignore_errors=True)

    def files_dir(self, lesson_id: str) -> Path:
        d = self._dir(lesson_id) / "files"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def outputs_dir(self, lesson_id: str) -> Path:
        d = self._dir(lesson_id) / "outputs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---------------- 元信息 ----------------

    def rename(self, lesson_id: str, name: str) -> dict | None:
        name = (name or "").strip()
        if not name:
            return None
        lesson = self.get(lesson_id)
        if not lesson:
            return None
        self.update(lesson_id, name=name)
        return lesson

    def set_course(self, lesson_id: str, course_id: str) -> bool:
        lesson = self.get(lesson_id)
        if not lesson:
            return False
        self.update(lesson_id, course_id=course_id or "")
        return True

    def clear_course(self, course_id: str) -> int:
        """课程被删除时，把挂在它下面的课堂退回「未分类」，不删任何数据。"""
        hit = 0
        with self._lock:
            for lesson in self._lessons.values():
                if lesson.get("course_id") == course_id:
                    lesson["course_id"] = ""
                    self.save(lesson)
                    hit += 1
        return hit

    def cancel(self, lesson_id: str) -> bool:
        """请求中断。真正的停止在各阶段检查点生效，不会硬杀线程。"""
        lesson = self.get(lesson_id)
        if not lesson or lesson["status"] not in RUNNING_STATUS:
            return False
        with self._lock:
            self._cancel.add(lesson_id)
        self.update(lesson_id, stage="正在停止…", progress=lesson.get("progress", 0))
        return True

    def _is_cancelled(self, lesson_id: str) -> bool:
        with self._lock:
            return lesson_id in self._cancel

    def _check_cancel(self, lesson_id: str) -> None:
        with self._lock:
            if lesson_id in self._cancel:
                self._cancel.discard(lesson_id)
                raise RunCancelled("执行已取消")

    # ---------------- 素材 ----------------

    def _mark_stale(self, lesson: dict) -> None:
        """结果生成后又动了素材，标记「结果已过期」，让用户知道要重跑。"""
        if lesson.get("status") == "done" and lesson.get("guides"):
            lesson["stale"] = True

    def _touch_after_change(self, lesson: dict, stage_if_new: str) -> None:
        """素材变动后的状态处理。

        已经生成过指导的，不要把状态打回「待分析」——结果还在，
        用户可能还想接着看；只标一个「有更新」，让他自己决定要不要重跑。
        """
        if lesson.get("guides") and lesson.get("status") == "done":
            lesson["stale"] = True
            lesson["stage"] = "材料有更新，建议重新分析"
            return
        lesson["status"] = "draft"
        lesson["stage"] = stage_if_new

    def add_material(self, lesson_id: str, filename: str, stored: Path) -> dict:
        lesson = self.get(lesson_id)
        if not lesson:
            raise KeyError("课堂不存在")
        mat = new_material(filename, stored)
        lesson["materials"].append(mat)
        self._touch_after_change(lesson, "讲义待解析")
        self.save(lesson)
        self._pool.submit(self._parse_material, lesson_id, mat["id"])
        return mat

    def _parse_material(self, lesson_id: str, material_id: str) -> None:
        lesson = self.get(lesson_id)
        if not lesson:
            return
        mat = next((m for m in lesson["materials"] if m["id"] == material_id), None)
        if not mat:
            return

        work = self._dir(lesson_id) / "ingest" / material_id
        try:
            result = ingest.ingest(mat["path"], work)
            mat.update({
                "pdf_path": result["pdf_path"] or "",
                "page_count": result["page_count"],
                "chars": result["chars"],
                "pages": result["pages"],
                "can_shot": result["can_shot"],
                "blank_pages": result["blank_pages"],
                "warnings": result["warnings"],
                "status": "parsed",
                "error": "",
            })
        except Exception as exc:  # noqa: BLE001
            mat["status"] = "failed"
            mat["error"] = f"{type(exc).__name__}: {exc}"

        with self._lock:
            lesson["updated"] = time.time()
            self.save(lesson)

    def add_audio(self, lesson_id: str, filename: str, stored: Path, size: int) -> dict:
        lesson = self.get(lesson_id)
        if not lesson:
            raise KeyError("课堂不存在")
        item = new_audio(filename, stored, size)
        try:
            item["duration"] = audio.probe(stored).get("duration", 0.0)
        except audio.AudioError:
            pass
        lesson["audios"].append(item)
        self._touch_after_change(lesson, "录音待转录")
        self.save(lesson)
        return item

    def remove_material(self, lesson_id: str, material_id: str) -> None:
        lesson = self.get(lesson_id)
        if not lesson:
            return
        gone = next((m for m in lesson["materials"] if m["id"] == material_id), None)
        lesson["materials"] = [m for m in lesson["materials"] if m["id"] != material_id]
        self._touch_after_change(lesson, lesson.get("stage") or "讲义待解析")
        self.save(lesson)
        if gone:
            for key in ("path", "pdf_path"):
                p = gone.get(key)
                if p:
                    Path(p).unlink(missing_ok=True)
            shutil.rmtree(self._dir(lesson_id) / "ingest" / material_id, ignore_errors=True)

    def remove_audio(self, lesson_id: str, audio_id: str) -> None:
        lesson = self.get(lesson_id)
        if not lesson:
            return
        gone = next((a for a in lesson["audios"] if a["id"] == audio_id), None)
        lesson["audios"] = [a for a in lesson["audios"] if a["id"] != audio_id]
        self._touch_after_change(lesson, lesson.get("stage") or "录音待转录")
        self.save(lesson)
        if gone and gone.get("path"):
            Path(gone["path"]).unlink(missing_ok=True)

    # ---------------- 执行 ----------------

    def run(self, lesson_id: str) -> None:
        self._pool.submit(self._run_guarded, lesson_id)

    def _run_guarded(self, lesson_id: str) -> None:
        try:
            self._run(lesson_id)
        except RunCancelled:
            self.update(lesson_id, status="canceled", stage="已取消", progress=0,
                        error="已取消这次执行，之前生成的结果没有受影响")
        except (LLMError, ASRError, ingest.IngestError, audio.AudioError) as exc:
            self.update(lesson_id, status="failed", stage="执行失败", error=str(exc))
        except Exception as exc:  # noqa: BLE001
            self.update(lesson_id, status="failed", stage="执行失败",
                        error=f"{type(exc).__name__}: {exc}")

    def _run(self, lesson_id: str) -> None:
        lesson = self.get(lesson_id)
        if not lesson:
            return

        cfg = config.load()
        self.update(lesson_id, status="parsing", stage="检查讲义", progress=2,
                    error="", guides={}, analysis=None)
        self._check_cancel(lesson_id)

        # 1. 讲义
        materials = lesson.get("materials") or []
        if not materials:
            raise ingest.IngestError("还没有导入讲义")
        if not lesson.get("audios"):
            raise ingest.IngestError("还没有导入课堂录音")

        for i, mat in enumerate(materials):
            if mat.get("status") == "parsed":
                continue
            self._check_cancel(lesson_id)
            self.update(lesson_id, stage=f"解析讲义 {i + 1}/{len(materials)}",
                        progress=2 + 8 * i / max(1, len(materials)))
            self._parse_material(lesson_id, mat["id"])

        lesson = self.get(lesson_id)
        failed_mats = [m for m in lesson["materials"] if m.get("status") != "parsed"]
        if len(failed_mats) == len(lesson["materials"]):
            detail = failed_mats[0].get("error", "") if failed_mats else ""
            raise ingest.IngestError(f"讲义解析全部失败：{detail}")

        # 2. 转录
        self._check_cancel(lesson_id)
        self.update(lesson_id, status="transcribing", stage="转录课堂录音", progress=12)
        transcript = self._transcribe_all(lesson_id, cfg)
        lesson = self.get(lesson_id)
        text = transcript_to_text(transcript)
        self.update(lesson_id, transcript=transcript, transcript_text=text, progress=58)

        # 3. 分析
        self._check_cancel(lesson_id)
        self.update(lesson_id, status="analyzing", stage="AI 正在通读材料", progress=62)
        client = LLMClient(cfg["llm"])
        client.validate()

        def on_progress(msg: str, pct: float) -> None:
            self.update(lesson_id, stage=msg, progress=62 + (pct - 30) * 0.3)

        analysis = analyze(lesson["materials"], text, client, on_progress)
        self.update(lesson_id, analysis=analysis, progress=88)

        # 4. 截图
        self._check_cancel(lesson_id)
        self.update(lesson_id, status="rendering", stage="截取讲义关键页", progress=90)
        shots = self._render_shots(lesson_id, lesson["materials"], text, analysis, cfg)

        # 5. 生成
        self.update(lesson_id, stage="生成复习指导", progress=95)
        out_dir = self.outputs_dir(lesson_id)
        guides = compose.build_guides(
            lesson_name=lesson["name"],
            analysis=analysis,
            out_dir=out_dir,
            shots=shots,
            options=cfg.get("guide", {}),
        )

        self.update(lesson_id, status="done", stage="已完成", progress=100,
                    guides=guides, transcript_text=text, stale=False)

    def _transcribe_all(self, lesson_id: str, cfg: dict) -> list[dict]:
        lesson = self.get(lesson_id)
        audios = lesson.get("audios") or []
        asr_cfg = config.asr_config(cfg)
        provider_id = asr_cfg.get("provider", "volc")
        pcfg = (asr_cfg.get("providers") or {}).get(provider_id, {})
        spec = spec_of(provider_id)
        provider = build(provider_id, pcfg)
        try:
            provider.validate()
        except ASRError as exc:
            raise ASRError(f"{exc}（语音引擎的设置在「语音转录工坊」里，两边共用同一份配置）") from exc

        merged: list[dict] = []
        offset_seconds = 0.0

        for idx, item in enumerate(audios):
            self._check_cancel(lesson_id)
            self.update(lesson_id, stage=f"转录 {idx + 1}/{len(audios)}：{item['filename']}")
            try:
                segs, duration = self._transcribe_one(
                    lesson_id, item, provider, spec, asr_cfg, offset_seconds,
                    idx, len(audios),
                )
            except ASRError as exc:
                item["status"] = "failed"
                item["error"] = str(exc)
                self.save(lesson)
                raise

            item["status"] = "done"
            item["duration"] = duration
            item["segments"] = len(segs)
            merged.extend(segs)
            offset_seconds += duration
            self.save(lesson)

        return merged

    def _transcribe_one(self, lesson_id: str, item: dict, provider, spec,
                        asr_cfg: dict, offset: float, index: int, total: int
                        ) -> tuple[list[dict], float]:
        info = audio.probe(item["path"])
        duration = float(info.get("duration") or 0)
        if duration <= 0:
            raise audio.AudioError(f"无法读取音频时长：{item['filename']}")

        seg_seconds = audio.effective_segment_seconds(
            spec, int(asr_cfg.get("segment_minutes", 20)) * 60
        )
        work = self._dir(lesson_id) / "transcribe" / item["id"]
        seg_files = audio.extract_segments(item["path"], work, duration, seg_seconds)

        opts = {"language": asr_cfg.get("language", "")}
        results: list = [None] * len(seg_files)
        done = 0
        base_pct = 12 + (44 * index / max(1, total))

        def work_one(i: int, seg: dict):
            data = Path(seg["path"]).read_bytes()
            return i, provider.transcribe(data, seg["length"], opts)

        cancelled = False
        with ThreadPoolExecutor(max_workers=max(1, spec.concurrency)) as pool:
            futures = {pool.submit(work_one, s["index"], s): s for s in seg_files}
            for fut in as_completed(futures):
                if self._is_cancelled(lesson_id):
                    cancelled = True
                    for f in futures:
                        f.cancel()
                    break
                i, res = fut.result()
                results[i] = res
                done += 1
                self.update(
                    lesson_id,
                    stage=f"转录中（{item['filename']} {done}/{len(seg_files)} 段）",
                    progress=base_pct + (44 / max(1, total)) * (done / max(1, len(seg_files))),
                )

        if cancelled:
            audio.cleanup([s["path"] for s in seg_files])
            shutil.rmtree(work, ignore_errors=True)
            self._cancel.discard(lesson_id)
            raise RunCancelled("执行已取消")

        out: list[dict] = []
        for seg, res in zip(seg_files, results):
            if not res:
                continue
            off = offset + seg["offset"]
            if res.segments:
                for s in res.segments:
                    out.append({"start": s.start + off, "end": s.end + off,
                                "text": s.text, "speaker": s.speaker})
            elif res.text.strip():
                out.append({"start": off, "end": off + seg["length"],
                            "text": res.text.strip(), "speaker": None})

        audio.cleanup([s["path"] for s in seg_files])
        shutil.rmtree(work, ignore_errors=True)
        out.sort(key=lambda x: x["start"])
        return out, duration

    def _render_shots(self, lesson_id: str, materials: list[dict], transcript_text: str,
                      analysis: dict, cfg: dict) -> dict[int, str]:
        options = cfg.get("guide", {})
        if not options.get("detailed_with_shots", True):
            return {}

        needed: set[int] = set()
        for kp in analysis.get("key_points") or []:
            needed |= set(kp.get("pages") or [])
        for ef in analysis.get("exam_focus") or []:
            needed |= set(ef.get("pages") or [])
        for pf in analysis.get("pitfalls") or []:
            needed |= set(pf.get("pages") or [])

        if not needed:
            return {}

        ctx = CourseContext(materials, transcript_text)
        out_root = self.outputs_dir(lesson_id) / "assets" / "shots"
        dpi = int(options.get("shot_dpi", 130))
        quality = int(options.get("shot_quality", 82))

        shots: dict[int, str] = {}
        for mi, mat in enumerate(materials):
            pdf_path = mat.get("pdf_path")
            if not pdf_path or not Path(pdf_path).exists():
                continue

            pages = [e["p"] for e in ctx.entries if e["g"] in needed and e["m"] == mi]
            if not pages:
                continue

            imgs = ingest.render_pages(pdf_path, pages, out_root / f"m{mi}",
                                       dpi=dpi, quality=quality)
            for page_num, img_path in imgs.items():
                for e in ctx.entries:
                    if e["m"] == mi and e["p"] == page_num:
                        shots[e["g"]] = img_path

        return shots


def transcript_to_text(segments: list[dict], max_lines: int = 4000) -> str:
    """把转写切成一行一句，带时间戳，方便模型引用 timestamp。"""
    lines: list[str] = []
    buf: list[str] = []
    start = None

    def flush() -> None:
        nonlocal buf, start
        if buf and start is not None:
            lines.append(f"[{clock(start)}] {''.join(buf)}")
        buf = []

    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if start is None:
            start = seg["start"]
        buf.append(text)
        # 攒够两句或超过 60 字就换行，避免一行太长
        if len(buf) >= 2 or len("".join(buf)) > 60:
            flush()
            start = None
    flush()

    return "\n".join(lines[:max_lines])


def clock(seconds: float) -> str:
    """统一成 HH:MM:SS，和提示词里要求的时间戳格式保持一致，方便模型直接引用。"""
    total = int(max(0, seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


store = LessonStore()
