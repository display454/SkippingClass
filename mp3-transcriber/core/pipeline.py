"""任务流水线：从本地文件到 md/docx 的完整链路。

流程：
1. 按当前引擎的硬限制（时长 / 体积）自动算分段长度；
2. 转码切片，交给 provider 并发识别，进度按「已完成段数」推进；
3. 合并时给每段时间戳加上该段在原文件中的偏移量。

所有厂商差异都被 shared.asr 的 provider 吸收，这里只负责调度。
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from shared.storage import write_json
from shared.local_security import redact_message
from pathlib import Path
from typing import Any

from shared import audio_utils as audio
from shared.asr import (ASRError, Segment, TranscribeResult, build,
                        spec_of)

from . import config, render

JOBS_PATH = config.DATA_DIR / "jobs.json"

ENGINE_FALLBACK = {
    "volc": "火山引擎 · 豆包",
    "openai": "OpenAI 兼容",
    "dashscope": "阿里云百炼",
    "local": "本地 faster-whisper",
}

STATUS_LABEL = {
    "queued": "排队中",
    "preparing": "音频预处理",
    "transcribing": "识别中",
    "rendering": "生成文档",
    "done": "已完成",
    "failed": "失败",
    "canceled": "已取消",
}


def engine_label(provider_id: str, pcfg: dict[str, Any]) -> str:
    """给用户看的引擎名，带上具体模型，方便回看时知道用的是什么。"""
    if provider_id == "volc":
        from shared.asr.volc import RESOURCE_LABEL
        return RESOURCE_LABEL.get(pcfg.get("resource_id", ""), ENGINE_FALLBACK["volc"])
    if provider_id == "openai":
        return f"OpenAI 兼容 · {pcfg.get('model') or 'whisper-1'}"
    if provider_id == "dashscope":
        return f"百炼 · {pcfg.get('model') or 'qwen3-asr-flash'}"
    if provider_id == "local":
        return f"本地 whisper · {pcfg.get('model') or 'small'}"
    return ENGINE_FALLBACK.get(provider_id, provider_id)


@dataclass
class Job:
    id: str
    filename: str
    src_path: str
    size: int
    status: str = "queued"
    stage: str = "等待处理"
    progress: float = 0.0
    duration: float = 0.0
    segments_total: int = 0
    segments_done: int = 0
    outputs: dict[str, str] = field(default_factory=dict)
    error: str = ""
    engine: str = ""
    provider: str = ""
    estimated_time: bool = False
    created: float = field(default_factory=time.time)
    finished_at: float = 0.0
    canceled: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("src_path", None)
        d.pop("canceled", None)
        d["status_label"] = STATUS_LABEL.get(self.status, self.status)
        d["size_text"] = human_size(self.size)
        d["duration_text"] = audio.format_duration(self.duration)
        d["outputs"] = sorted(
            self.outputs.keys(),
            key=lambda k: {"md": 0, "docx": 1, "txt": 2}.get(k, 9),
        )
        d["elapsed"] = round((self.finished_at or time.time()) - self.created, 1)
        return d


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


class JobStore:
    def __init__(self, workers: int = 2):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="job")
        self._load_jobs()

    # ---------------- 持久化 ----------------

    def _persist(self) -> None:
        """任务表写盘，重启后不丢。文件很小，直接覆盖写。"""
        try:
            data = [asdict(j) for j in self._jobs.values()]
            JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
            write_json(JOBS_PATH, data)
        except (OSError, TypeError):
            pass

    def _load_jobs(self) -> None:
        if not JOBS_PATH.exists():
            self._recover_from_outputs()
            return

        try:
            raw = json.loads(JOBS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return

        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                job = Job(**{
                    k: v for k, v in item.items()
                    if k in Job.__dataclass_fields__
                })
            except (TypeError, ValueError):
                continue

            # 上次服务在跑任务时被关掉，这些任务已经不可能恢复了
            if job.status not in ("done", "failed", "canceled"):
                job.status = "failed"
                job.stage = "服务已重启"
                job.error = "服务重启导致任务中断，可点「重试」重新识别"
                job.finished_at = job.finished_at or time.time()

            self._jobs[job.id] = job

    def _recover_from_outputs(self) -> None:
        """首次升级时，把磁盘上已有的产出恢复成任务记录，避免历史结果「消失」。"""
        if not config.OUTPUT_DIR.exists():
            return

        for folder in config.OUTPUT_DIR.iterdir():
            if not folder.is_dir():
                continue

            outputs: dict[str, str] = {}
            for f in folder.iterdir():
                ext = f.suffix.lstrip(".").lower()
                if ext in ("md", "docx", "txt") and f.is_file():
                    outputs[ext] = str(f)

            if not outputs:
                continue

            source_name = ""
            duration = 0.0
            md_file = outputs.get("md")
            if md_file:
                try:
                    head = Path(md_file).read_text(encoding="utf-8", errors="replace")[:1200]
                    m = re.search(r'^source:\s*"?(.+?)"?\s*$', head, re.M)
                    if m:
                        source_name = m.group(1)
                    m2 = re.search(r'^duration:\s*"?(\d+):(\d+)(?::(\d+))?"?', head, re.M)
                    if m2:
                        parts = [int(x) for x in m2.groups() if x is not None]
                        duration = (parts[0] * 3600 + parts[1] * 60 + parts[2]
                                    if len(parts) == 3 else parts[0] * 60 + parts[1])
                except OSError:
                    pass

            if not source_name:
                any_file = next(iter(outputs.values()))
                source_name = Path(any_file).stem

            mtime = min(Path(p).stat().st_mtime for p in outputs.values())
            job = Job(
                id=folder.name,
                filename=source_name,
                src_path="",
                size=0,
                status="done",
                stage="已恢复（历史结果）",
                progress=100.0,
                duration=duration,
                outputs=outputs,
                engine="历史记录",
                created=mtime,
                finished_at=mtime,
            )
            self._jobs[job.id] = job

        if self._jobs:
            self._persist()

    # ---------------- 任务管理 ----------------

    def create(self, filename: str, src_path: str, size: int) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], filename=filename,
                  src_path=src_path, size=size)
        with self._lock:
            self._jobs[job.id] = job
            self._persist()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)

    def update(self, job_id: str, **kwargs: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            for k, v in kwargs.items():
                if hasattr(job, k):
                    setattr(job, k, v)
            self._persist()

    def remove(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)
            self._persist()

    def submit(self, job_id: str, cfg: dict[str, Any]) -> None:
        self._pool.submit(self._run_guarded, job_id, cfg)

    # ---------------- 执行 ----------------

    def _run_guarded(self, job_id: str, cfg: dict[str, Any]) -> None:
        try:
            self._run(job_id, cfg)
        except ASRError as exc:
            self.update(job_id, status="failed", stage="识别失败",
                        error=redact_message(exc, config.load()), finished_at=time.time())
        except audio.AudioError as exc:
            self.update(job_id, status="failed", stage="音频处理失败",
                        error=redact_message(exc, config.load()), finished_at=time.time())
        except Exception as exc:  # noqa: BLE001
            self.update(job_id, status="failed", stage="未知错误",
                        error=f"{type(exc).__name__}: {redact_message(exc, config.load())}", finished_at=time.time())

    def _run(self, job_id: str, cfg: dict[str, Any]) -> None:
        job = self.get(job_id)
        if not job:
            return

        provider_id = cfg.get("provider", "volc")
        pcfg = cfg["providers"].get(provider_id, {})
        spec = spec_of(provider_id)
        label = engine_label(provider_id, pcfg)
        self.update(job_id, engine=label, provider=provider_id,
                    progress=2, stage="读取音频信息")

        provider = None
        if not cfg.get("mock"):
            provider = build(provider_id, pcfg)
            provider.validate()     # 提前校验，避免白白跑完转码才失败

        # 1. 探测
        info = audio.probe(job.src_path)
        duration = info.get("duration") or 0.0
        if duration <= 0:
            raise audio.AudioError("无法读取音频时长，文件可能损坏或不是有效的音频")
        self.update(job_id, duration=duration, progress=6)

        # 2. 按引擎能力算分段
        seg_seconds = audio.effective_segment_seconds(
            spec, int(cfg.get("segment_minutes", 20)) * 60
        )
        plan = audio.plan_segments(duration, seg_seconds)
        self.update(job_id, segments_total=len(plan), progress=10,
                    stage=f"转码切分（{len(plan)} 段 × {seg_seconds // 60} 分钟）")

        work_dir = config.WORK_DIR / job_id
        seg_files = audio.extract_segments(job.src_path, work_dir, duration, seg_seconds)

        if self._is_canceled(job_id):
            self._mark_canceled(job_id)
            return

        # 3. 识别
        self.update(job_id, progress=14, stage="提交识别任务")
        if cfg.get("mock"):
            results = self._mock_transcribe(job_id, seg_files)
        else:
            results = self._transcribe(job_id, provider, spec, seg_files, cfg)

        if self._is_canceled(job_id):
            self._mark_canceled(job_id)
            return

        # 4. 合并
        self.update(job_id, progress=88, stage="合并结果")
        segments = self._merge(seg_files, results)
        estimated = any(getattr(r, "estimated_time", False) for r in results if r)

        note_parts = []
        if cfg.get("mock"):
            note_parts.append("⚠️ 当前为演示模式，以下内容为模拟生成的示例文本，并非真实识别结果。")
        if estimated:
            note_parts.append("ℹ️ 该引擎不返回逐句时间戳，时间轴按字数比例估算，仅供大致定位。")
        note = "\n>\n> ".join(note_parts)

        # 5. 渲染
        self.update(job_id, progress=92, stage="生成文档")
        paragraphs = render.merge_paragraphs(segments)
        out_dir = config.OUTPUT_DIR / job_id / ("rev_" + uuid.uuid4().hex[:12])
        outputs = render.build_all(
            job_id=job_id,
            source_name=job.filename,
            out_dir=out_dir,
            duration=duration,
            paragraphs=paragraphs,
            segments=segments,
            formats=cfg.get("outputs") or ["md"],
            engine=label,
            include_timeline=cfg.get("include_timeline", True),
            note=note,
        )

        if self._is_canceled(job_id):
            self._mark_canceled(job_id)
            audio.cleanup([s["path"] for s in seg_files])
            return
        self.update(job_id, status="done", stage="已完成", progress=100,
                    outputs=outputs, estimated_time=estimated,
                    finished_at=time.time())

        audio.cleanup([s["path"] for s in seg_files])
        try:
            work_dir.rmdir()
        except OSError:
            pass

    # ---------------- 识别 ----------------

    def _transcribe(self, job_id: str, provider, spec,
                    seg_files: list[dict], cfg: dict[str, Any]) -> list[TranscribeResult]:
        total = len(seg_files)
        results: list[TranscribeResult | None] = [None] * total
        workers = max(1, min(spec.concurrency or 1, total))

        opts = {"language": cfg.get("language", "")}

        def work(idx: int, seg: dict) -> tuple[int, TranscribeResult]:
            data = Path(seg["path"]).read_bytes()
            return idx, provider.transcribe(data, seg["length"], opts)

        done = 0
        self.update(job_id, progress=18, stage=f"识别中（0/{total}）")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(work, s["index"], s): s["index"] for s in seg_files}
            try:
                for fut in as_completed(futures):
                    idx, res = fut.result()
                    results[idx] = res
                    done += 1
                    self.update(job_id, segments_done=done,
                                progress=18 + 66 * done / total,
                                stage=f"识别中（{done}/{total}）")
            except Exception:
                for f in futures:
                    f.cancel()
                raise

        return [r or TranscribeResult() for r in results]

    def _merge(self, seg_files: list[dict],
               results: list[TranscribeResult]) -> list[Segment]:
        merged: list[Segment] = []
        for seg, res in zip(seg_files, results):
            if res is None:
                continue
            offset = seg["offset"]
            if res.segments:
                for s in res.segments:
                    merged.append(Segment(
                        text=s.text, start=s.start + offset,
                        end=s.end + offset, speaker=s.speaker,
                    ))
            elif res.text.strip():
                merged.append(Segment(
                    text=res.text.strip(), start=offset,
                    end=offset + seg["length"],
                ))
        merged.sort(key=lambda s: s.start)
        return merged

    # ---------------- 演示模式 ----------------

    def _mock_transcribe(self, job_id: str,
                         seg_files: list[dict]) -> list[TranscribeResult]:
        samples = [
            "这是一段用于验证处理流程的示例文本，它并不会真的调用语音识别接口。",
            "整条链路包含音频转码、分段切片、任务提交、结果合并以及文档渲染。",
            "你在设置里填好 API Key 之后，关闭演示模式，就能得到真实的识别结果。",
            "时间轴会按照分句给出起始时间，方便你回到原始音频里核对内容。",
            "导出的 Markdown 和 Word 都会包含文字稿与时间轴两个部分。",
        ]
        total = len(seg_files)
        results: list[TranscribeResult] = []
        for i, seg in enumerate(seg_files):
            length = seg["length"]
            count = max(1, int(length // 10))
            segs: list[Segment] = []
            t = 0.0
            for k in range(count):
                if t >= length:
                    break
                dur = min(6.0, length - t)
                segs.append(Segment(text=samples[(i * 3 + k) % len(samples)],
                                    start=t, end=t + dur))
                t += dur + 0.6
            results.append(TranscribeResult(
                text="".join(s.text for s in segs), segments=segs, duration=length,
            ))
            time.sleep(0.4)
            self.update(job_id, segments_done=i + 1,
                        progress=14 + 72 * (i + 1) / total,
                        stage=f"演示识别（{i + 1}/{total}）")
        return results

    # ---------------- 小工具 ----------------

    def _is_canceled(self, job_id: str) -> bool:
        job = self.get(job_id)
        return bool(job and job.canceled)

    def _mark_canceled(self, job_id: str) -> None:
        self.update(job_id, status="canceled", stage="已取消",
                    finished_at=time.time())


store = JobStore()
