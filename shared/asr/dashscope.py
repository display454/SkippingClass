"""阿里云百炼（DashScope）的非实时语音识别 provider。

走的是 OpenAI 兼容的 chat/completions，但音频放在 messages 的
input_audio 里（不是 /audio/transcriptions），所以需要单独实现。

- 模型：qwen3-asr-flash（另有 paraformer 系列走异步任务接口，需公网 URL）
- 音频：URL 或 base64 data URI，base64 后不超过 10MB
- 返回：纯文本，没有时间戳 -> 自动按字数估算时间轴
"""
from __future__ import annotations

import base64
from typing import Any

import requests

from .base import (ASRError, BaseProvider, ProviderSpec,
                   TranscribeResult, build_result)

DEFAULT_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# 官方说 base64 后 10MB 以内，按原始音频留足余量
MAX_RAW_BYTES = 7 * 1024 * 1024


class DashScopeProvider(BaseProvider):
    spec = ProviderSpec(
        id="dashscope",
        name="阿里云百炼 · Qwen-ASR",
        vendor="阿里云",
        summary="国内直连快，中英混合稳定，返回纯文本无时间戳",
        supports_timestamps=False,
        max_segment_seconds=600,
        max_segment_bytes=MAX_RAW_BYTES,
        concurrency=2,
    )

    def __init__(self, cfg: dict[str, Any]):
        super().__init__(cfg)
        base = (cfg.get("base_url") or DEFAULT_BASE).strip().rstrip("/")
        self.base_url = base or DEFAULT_BASE
        self.model = (cfg.get("model") or "qwen3-asr-flash").strip()

    def validate(self) -> None:
        self._need("api_key", "API Key")

    def transcribe(self, audio_bytes: bytes, duration: float,
                   opts: dict[str, Any] | None = None) -> TranscribeResult:
        opts = opts or {}
        self.validate()

        if len(audio_bytes) > MAX_RAW_BYTES:
            raise ASRError(
                f"单段音频 {len(audio_bytes) / 1024 / 1024:.1f}MB 超过百炼限制，"
                f"请把分段长度调小到 {self.spec.max_segment_seconds // 60} 分钟以内"
            )

        data_uri = "data:audio/mpeg;base64," + base64.b64encode(audio_bytes).decode("ascii")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [{"type": "input_audio", "input_audio": {"data": data_uri}}],
            }],
            "stream": False,
        }

        asr_opts: dict[str, Any] = {}
        lang = (opts.get("language") or self.cfg.get("language") or "").strip()
        if lang:
            asr_opts["language"] = lang.split("-")[0]
        if self.cfg.get("enable_itn", True):
            asr_opts["enable_itn"] = True
        if asr_opts:
            payload["asr_options"] = asr_opts

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {(self.cfg.get('api_key') or '').strip()}",
            "Content-Type": "application/json",
        }

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=(20, 600))
        except requests.RequestException as exc:
            raise ASRError(f"百炼识别请求失败（网络异常）：{exc}") from exc

        if resp.status_code != 200:
            raise ASRError(self._error_text(resp), code=str(resp.status_code))

        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            raise ASRError(f"百炼返回格式异常：{exc}") from exc

        text = (content or "").strip()
        return build_result(text, [], duration, data, estimated=True)

    @staticmethod
    def _error_text(resp: requests.Response) -> str:
        try:
            data = resp.json()
            msg = data.get("message") or data.get("error") or data
            if isinstance(msg, dict):
                msg = msg.get("message") or str(msg)
        except ValueError:
            msg = (resp.text or "")[:300]

        hint = ""
        if resp.status_code == 401:
            hint = "（API Key 无效，注意百炼的 Key 与火山、OpenAI 不通用）"
        elif resp.status_code == 404:
            hint = "（Base URL 可能有误，新版控制台需要填带 WorkspaceId 的地址）"
        elif resp.status_code == 403:
            hint = "（该模型未开通，请到百炼控制台开通 qwen3-asr-flash）"
        return f"百炼识别失败：{msg}{hint}"
