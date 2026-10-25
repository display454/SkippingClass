"""语音识别 provider 注册表。"""
from __future__ import annotations

from typing import Any

from .base import (ASRError, BaseProvider, ProviderSpec, Segment,
                   TranscribeResult, estimate_segments, merge_segments,
                   split_sentences)
from .dashscope import DashScopeProvider
from .local_whisper import LocalWhisperProvider
from .openai_compat import OpenAICompatProvider
from .sensevoice import SenseVoiceProvider
from .volc import VolcProvider

_CLASSES = [VolcProvider, OpenAICompatProvider, DashScopeProvider,
            SenseVoiceProvider, LocalWhisperProvider]
PROVIDERS: dict[str, type[BaseProvider]] = {c.spec.id: c for c in _CLASSES}

DEFAULT_PROVIDER = "volc"


def catalog() -> list[dict[str, Any]]:
    """给前端展示的 provider 列表。"""
    return [
        {
            "id": c.spec.id,
            "name": c.spec.name,
            "vendor": c.spec.vendor,
            "summary": c.spec.summary,
            "supports_timestamps": c.spec.supports_timestamps,
            "max_segment_minutes": max(1, c.spec.max_segment_seconds // 60),
            "max_segment_mb": round(c.spec.max_segment_bytes / 1024 / 1024, 1),
            "needs_local_model": c.spec.needs_local_model,
        }
        for c in _CLASSES
    ]


def build(provider_id: str, cfg: dict[str, Any]) -> BaseProvider:
    cls = PROVIDERS.get(provider_id or DEFAULT_PROVIDER)
    if not cls:
        raise ASRError(f"未知的识别引擎：{provider_id}")
    return cls(cfg)


def spec_of(provider_id: str) -> ProviderSpec:
    cls = PROVIDERS.get(provider_id or DEFAULT_PROVIDER)
    if not cls:
        raise ASRError(f"未知的识别引擎：{provider_id}")
    return cls.spec


__all__ = [
    "ASRError", "BaseProvider", "ProviderSpec", "Segment", "TranscribeResult",
    "estimate_segments", "merge_segments", "split_sentences",
    "PROVIDERS", "DEFAULT_PROVIDER", "catalog", "build", "spec_of",
]
