"""语音识别统一抽象。

设计要点：每个 provider 只负责「识别一段音频」，切片、并发、合并时间轴
全部交给上层的 pipeline。这样新增一家厂商只需要实现一个 transcribe()，
不用关心长音频怎么切、进度怎么报。

各家接口的硬限制不一样，由 ProviderSpec 声明，上层据此决定分段长度：
- 火山标准版：5 小时 / 512MB
- OpenAI：25 分钟 / 25MB（双重限制，时长往往先触发）
- 阿里百炼 qwen3-asr-flash：base64 后 10MB
- 本地 whisper：只受内存影响
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


class ASRError(RuntimeError):
    """识别失败。code / logid 会透传给用户排查。"""

    def __init__(self, message: str, code: str = "", logid: str = ""):
        super().__init__(message)
        self.code = code
        self.logid = logid

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.code:
            parts.append(f"状态码 {self.code}")
        if self.logid:
            parts.append(f"LogID {self.logid}")
        return " · ".join(parts)


@dataclass
class Segment:
    text: str
    start: float          # 秒
    end: float
    speaker: str | None = None


@dataclass
class TranscribeResult:
    text: str = ""
    segments: list[Segment] = field(default_factory=list)
    duration: float = 0.0
    raw: dict = field(default_factory=dict)
    estimated_time: bool = False   # 时间戳是否为按字数估算


@dataclass
class ProviderSpec:
    """给前端展示、给调度层决策用的元信息。"""
    id: str
    name: str
    vendor: str
    summary: str
    supports_timestamps: bool
    max_segment_seconds: int
    max_segment_bytes: int
    concurrency: int = 2
    needs_local_model: bool = False


# ---------------- 文本切分与时间估算 ----------------

_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])|(?<=[.!?])\s+|\n+")


def split_sentences(text: str) -> list[str]:
    """按句末标点切句，保留标点。"""
    if not text:
        return []
    raw = _SENT_SPLIT.split(text)
    out: list[str] = []
    for part in raw:
        if not part:
            continue
        piece = part.strip()
        # 过短的碎片并入上一句，避免全文被切成碎渣
        if out and len(piece) < 4:
            out[-1] += piece
        elif piece:
            out.append(piece)
    return out


def estimate_segments(text: str, duration: float) -> list[Segment]:
    """没有原生时间戳时，按字符数比例把时间摊到各句上。

    精度只够「大致定位」，所以结果会标记 estimated_time=True，
    渲染时在文档里注明「时间戳为估算值」。
    """
    sents = split_sentences(text)
    if not sents:
        return []

    total_chars = sum(max(1, len(s)) for s in sents)
    if duration <= 0:
        return [Segment(text=s, start=0.0, end=0.0) for s in sents]

    segments: list[Segment] = []
    cursor = 0.0
    for s in sents:
        share = duration * max(1, len(s)) / total_chars
        segments.append(Segment(text=s, start=cursor, end=min(duration, cursor + share)))
        cursor += share
    return segments


def merge_segments(segments: list[Segment]) -> str:
    """把分句拼回一段连续文本，避免句间出现多余空格。"""
    buf: list[str] = []
    for seg in segments:
        t = seg.text.strip()
        if not t:
            continue
        if buf and re.match(r"[A-Za-z0-9]", t[:1]) and re.search(r"[A-Za-z0-9,.;:]$", buf[-1]):
            buf.append(" ")
        buf.append(t)
    return "".join(buf)


# ---------------- Provider 基类 ----------------

class BaseProvider:
    spec: ProviderSpec

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg or {}

    def validate(self) -> None:
        """检查凭证是否齐全，缺失时抛 ASRError（信息要能指导用户）。"""

    def transcribe(self, audio_bytes: bytes, duration: float,
                   opts: dict[str, Any] | None = None) -> TranscribeResult:
        raise NotImplementedError

    # 便捷方法
    def _need(self, key: str, label: str) -> str:
        val = (self.cfg.get(key) or "").strip()
        if not val:
            raise ASRError(f"{self.spec.name} 尚未配置「{label}」，请在设置中填写")
        return val


def build_result(text: str, segments: list[Segment], duration: float,
                 raw: dict | None = None, estimated: bool = False) -> TranscribeResult:
    if not segments and text:
        segments = estimate_segments(text, duration)
        estimated = True
    return TranscribeResult(
        text=text or merge_segments(segments),
        segments=segments,
        duration=duration,
        raw=raw or {},
        estimated_time=estimated,
    )
