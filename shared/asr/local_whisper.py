"""本地 faster-whisper provider：不联网、不要密钥、不花钱。

代价是首次要下载模型（几十 MB 到 1.5GB），且速度取决于 CPU/GPU。
模型实例会缓存复用，避免每段都重新加载。

常见耗时参考（CPU int8，中文 1 小时音频）：
tiny 约 2 分钟 / base 约 5 分钟 / small 约 12 分钟 / medium 约 30 分钟
"""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .base import (ASRError, BaseProvider, ProviderSpec, Segment,
                   TranscribeResult)

MODEL_SIZES = ["tiny", "base", "small", "medium", "large-v3", "distil-large-v3"]

_cache: dict[tuple[str, str, str], Any] = {}
_cache_lock = threading.Lock()


class LocalWhisperProvider(BaseProvider):
    spec = ProviderSpec(
        id="local",
        name="本地 · faster-whisper",
        vendor="本地运行",
        summary="完全离线、零成本，带时间戳。首次需下载模型，CPU 上速度较慢",
        supports_timestamps=True,
        max_segment_seconds=1800,
        max_segment_bytes=200 * 1024 * 1024,
        concurrency=1,          # 本地 CPU 并发只会互相抢算力
        needs_local_model=True,
    )

    def __init__(self, cfg: dict[str, Any]):
        super().__init__(cfg)
        self.model_size = (cfg.get("model") or "small").strip()
        self.device = (cfg.get("device") or "cpu").strip()
        self.compute_type = (cfg.get("compute_type") or "int8").strip()

    def validate(self) -> None:
        try:
            import faster_whisper  # noqa: F401
        except ImportError as exc:
            raise ASRError(
                "未安装 faster-whisper，请运行：pip install faster-whisper"
            ) from exc

    def _get_model(self):
        key = (self.model_size, self.device, self.compute_type)
        with _cache_lock:
            model = _cache.get(key)
            if model is not None:
                return model

            # 国内直连 huggingface 常常超时，默认走镜像（用户已设置则不覆盖）
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

            from faster_whisper import WhisperModel
            try:
                model = WhisperModel(
                    self.model_size, device=self.device,
                    compute_type=self.compute_type,
                )
            except Exception as exc:  # noqa: BLE001
                raise ASRError(
                    f"加载本地模型失败：{exc}。首次使用需联网下载模型，"
                    f"若一直失败可换更小的模型（tiny / base）"
                ) from exc
            _cache[key] = model
            return model

    def transcribe(self, audio_bytes: bytes, duration: float,
                   opts: dict[str, Any] | None = None) -> TranscribeResult:
        opts = opts or {}
        self.validate()
        model = self._get_model()

        lang = (opts.get("language") or self.cfg.get("language") or "").strip()
        lang_code = lang.split("-")[0] if lang else None

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as fh:
                fh.write(audio_bytes)
                tmp_path = fh.name

            try:
                seg_iter, info = model.transcribe(
                    tmp_path,
                    language=lang_code,
                    beam_size=5,
                    vad_filter=bool(self.cfg.get("vad", True)),
                    condition_on_previous_text=False,
                )
            except Exception as exc:  # noqa: BLE001
                raise ASRError(f"本地识别失败：{exc}") from exc

            segments: list[Segment] = []
            for s in seg_iter:
                text = (s.text or "").strip()
                if not text:
                    continue
                segments.append(Segment(
                    text=text,
                    start=float(s.start or 0),
                    end=float(s.end or 0),
                ))
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

        detected = getattr(info, "duration", None) or duration
        full_text = "".join(s.text for s in segments)
        return TranscribeResult(
            text=full_text,
            segments=segments,
            duration=float(detected),
            raw={"language": getattr(info, "language", None)},
            estimated_time=False,
        )
