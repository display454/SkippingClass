"""OpenAI 及所有 OpenAI 兼容协议的语音转写 provider。

一套代码覆盖：OpenAI、硅基流动、Groq、DeepInfra、自建网关等，
只要对方实现了 OpenAI 的 /audio/transcriptions。

重要差异：
- whisper-1 支持 verbose_json，能拿到分句时间戳
- gpt-4o-transcribe / mini 只支持 json，没有任何时间戳
  这类模型走降级路径：先拿纯文本，再按字数比例估算时间轴
"""
from __future__ import annotations

import time
from typing import Any

import requests

from .base import (ASRError, BaseProvider, ProviderSpec, Segment,
                   TranscribeResult, build_result)

DEFAULT_BASE = "https://api.openai.com/v1"


def _normalize_base(url: str) -> str:
    base = (url or DEFAULT_BASE).strip().rstrip("/")
    if not base:
        base = DEFAULT_BASE
    if not base.endswith("/v1") and "/v1" not in base:
        base += "/v1"
    return base


class OpenAICompatProvider(BaseProvider):
    spec = ProviderSpec(
        id="openai",
        name="OpenAI 兼容",
        vendor="OpenAI / 硅基流动 / Groq 等",
        summary="一套协议覆盖多家厂商。whisper-1 带时间戳，gpt-4o-transcribe 更准但无时间戳",
        supports_timestamps=True,   # whisper-1；其它型号自动降级
        max_segment_seconds=1400,   # 官方硬限制 1500 秒，留一点余量
        max_segment_bytes=24 * 1024 * 1024,
        concurrency=2,
    )

    def __init__(self, cfg: dict[str, Any]):
        super().__init__(cfg)
        self.base_url = _normalize_base(cfg.get("base_url") or DEFAULT_BASE)
        self.model = (cfg.get("model") or "whisper-1").strip()

    def validate(self) -> None:
        self._need("api_key", "API Key")

    def _wants_verbose(self) -> bool:
        """只有 whisper 系模型能吐时间戳。"""
        if self.cfg.get("force_verbose"):
            return True
        m = self.model.lower()
        if "whisper" in m:
            return True
        if "gpt-4o" in m or "transcribe" in m or "paraformer" in m:
            return False
        return False

    def transcribe(self, audio_bytes: bytes, duration: float,
                   opts: dict[str, Any] | None = None) -> TranscribeResult:
        opts = opts or {}
        self.validate()

        verbose = self._wants_verbose()
        body, err, status = self._request(audio_bytes, opts, verbose)

        # 有些第三方服务虽然模型名像 whisper，却不支持 verbose_json，
        # 直接退回 json 再试一次，避免用户被一个参数卡住。
        if err and "verbose_json" in err.lower() and verbose:
            body, err, status = self._request(audio_bytes, opts, False)
            verbose = False

        if err:
            raise ASRError(self._friendly(err, status), code=str(status))

        text = (body.get("text") or "").strip()
        segments: list[Segment] = []

        if verbose:
            for seg in body.get("segments") or []:
                if not isinstance(seg, dict):
                    continue
                t = (seg.get("text") or "").strip()
                if not t:
                    continue
                segments.append(Segment(
                    text=t,
                    start=float(seg.get("start") or 0),
                    end=float(seg.get("end") or 0),
                ))

        duration = float(body.get("duration") or duration or 0)
        return build_result(text, segments, duration, body, estimated=not segments)

    def _request(self, audio_bytes: bytes, opts: dict, verbose: bool
                 ) -> tuple[dict, str, int]:
        url = f"{self.base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {(self.cfg.get('api_key') or '').strip()}"}

        data = {
            "model": self.model,
            "response_format": "verbose_json" if verbose else "json",
        }
        lang = (opts.get("language") or self.cfg.get("language") or "").strip()
        if lang:
            data["language"] = lang.split("-")[0]
        prompt = (opts.get("prompt") or self.cfg.get("prompt") or "").strip()
        if prompt:
            data["prompt"] = prompt[:900]

        last_err = ""
        last_status = 0
        for attempt in range(3):
            try:
                resp = requests.post(
                    url, headers=headers, data=data,
                    files={"file": ("audio.mp3", audio_bytes, "audio/mpeg")},
                    timeout=(20, 600),
                )
            except requests.RequestException as exc:
                last_err, last_status = f"网络异常：{exc}", 0
                time.sleep(1.5 * (attempt + 1))
                continue

            last_status = resp.status_code
            if resp.status_code == 200:
                try:
                    return resp.json(), "", 200
                except ValueError:
                    return {"text": resp.text}, "", 200

            last_err = self._extract_error(resp)
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(2.0 * (attempt + 1))
                continue
            break

        return {}, last_err or "请求失败", last_status

    @staticmethod
    def _extract_error(resp: requests.Response) -> str:
        try:
            data = resp.json()
            err = data.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or err)
            return str(err or data.get("message") or resp.text[:300])
        except ValueError:
            return (resp.text or f"HTTP {resp.status_code}")[:300]

    def _friendly(self, message: str, status: int) -> str:
        msg = message
        if status == 401:
            msg = "API Key 无效或已过期，请检查设置里的密钥"
        elif status == 404:
            msg = f"接口地址不存在，检查 Base URL（当前 {self.base_url}）"
        elif status == 429:
            msg = "请求过于频繁或额度不足，请稍后重试"
        elif "25" in message and ("mb" in message.lower() or "size" in message.lower()):
            msg = "音频超过 25MB 上限，请调小分段长度"
        return f"识别失败：{msg}"
