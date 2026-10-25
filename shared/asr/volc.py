"""火山引擎「豆包录音文件识别」provider。

两种调用形态：
- standard：submit 提交 -> query 轮询（异步，官方支持 5h/512MB）
- flash：recognize/flash 一次返回（同步，2h/100MB）

业务状态不在 HTTP 码里，而在响应头 X-Api-Status-Code：
20000000 成功 / 20000001 处理中 / 20000002 排队 / 其它为终态错误。
"""
from __future__ import annotations

import base64
import time
import uuid
from typing import Any

import requests

from .base import ASRError, BaseProvider, ProviderSpec, Segment, TranscribeResult

SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
FLASH_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"

OK = "20000000"
PENDING = {"20000001", "20000002"}

RESOURCE_LABEL = {
    "volc.seedasr.auc": "豆包录音文件识别模型 2.0",
    "volc.bigasr.auc": "豆包录音文件识别模型 1.0",
    "volc.seedasr.auc_turbo": "豆包录音文件识别模型 2.0（极速版）",
    "volc.bigasr.auc_turbo": "豆包录音文件识别模型 1.0（极速版）",
}


class VolcProvider(BaseProvider):
    spec = ProviderSpec(
        id="volc",
        name="火山引擎 · 豆包",
        vendor="字节跳动",
        summary="中英混说与方言表现好，标准版支持 5 小时长音频，可分离说话人",
        supports_timestamps=True,
        max_segment_seconds=1800,
        max_segment_bytes=20 * 1024 * 1024,
        concurrency=3,
    )

    def __init__(self, cfg: dict[str, Any]):
        super().__init__(cfg)
        self.mode = cfg.get("mode", "standard")
        self.resource_id = cfg.get("resource_id") or "volc.seedasr.auc"
        self.timeout = int(cfg.get("timeout", 120))

    # ---- 鉴权 ----

    def validate(self) -> None:
        if self.cfg.get("auth_mode", "api_key") == "api_key":
            self._need("api_key", "API Key")
        else:
            self._need("app_id", "APP ID")
            self._need("access_key", "Access Token")

    def _headers(self, request_id: str) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": request_id,
            "X-Api-Sequence": "-1",
        }
        if self.cfg.get("auth_mode", "api_key") == "api_key":
            h["X-Api-Key"] = (self.cfg.get("api_key") or "").strip()
        else:
            h["X-Api-App-Key"] = (self.cfg.get("app_id") or "").strip()
            h["X-Api-Access-Key"] = (self.cfg.get("access_key") or "").strip()
        return {k: v for k, v in h.items() if v}

    # ---- 请求体 ----

    def _body(self, audio_b64: str, opts: dict[str, Any]) -> dict:
        audio = {
            "data": audio_b64,
            "format": "mp3",
            "rate": 16000,
            "bits": 16,
            "channel": 1,
        }
        lang = (opts.get("language") or self.cfg.get("language") or "").strip()
        if lang:
            audio["language"] = lang

        req = {
            "model_name": "bigmodel",
            "enable_itn": bool(self.cfg.get("enable_itn", True)),
            "enable_punc": bool(self.cfg.get("enable_punc", True)),
            "enable_ddc": bool(self.cfg.get("enable_ddc", False)),
            "show_utterances": True,   # 必须开，否则没有分句与时间戳
        }
        if self.cfg.get("enable_speaker_info"):
            req["enable_speaker_info"] = True

        return {"user": {"uid": "transcript-studio"}, "audio": audio, "request": req}

    @staticmethod
    def _parse(resp: requests.Response) -> tuple[str, str, str, dict]:
        status = resp.headers.get("X-Api-Status-Code") or ""
        message = resp.headers.get("X-Api-Message") or ""
        logid = resp.headers.get("X-Tt-Logid") or ""
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": (resp.text or "")[:800]}
        if not status and isinstance(body, dict):
            status = str(body.get("code") or "")
            message = message or str(body.get("message") or "")
        return status, message, logid, body

    # ---- 主流程 ----

    def transcribe(self, audio_bytes: bytes, duration: float,
                   opts: dict[str, Any] | None = None) -> TranscribeResult:
        opts = opts or {}
        self.validate()
        b64 = base64.b64encode(audio_bytes).decode("ascii")

        if self.mode == "flash":
            body = self._call_flash(b64, opts)
        else:
            body = self._call_standard(b64, opts)

        return self._extract(body, duration)

    def _call_flash(self, audio_b64: str, opts: dict) -> dict:
        task_id = str(uuid.uuid4())
        try:
            resp = requests.post(
                FLASH_URL, headers=self._headers(task_id),
                json=self._body(audio_b64, opts), timeout=(15, 300),
            )
        except requests.RequestException as exc:
            raise ASRError(f"极速版识别请求失败（网络异常）：{exc}") from exc

        status, message, logid, body = self._parse(resp)
        if status != OK:
            raise ASRError(
                f"极速版识别失败：{message or '接口返回异常'}（HTTP {resp.status_code}）",
                code=status, logid=logid,
            )
        return body

    def _call_standard(self, audio_b64: str, opts: dict) -> dict:
        task_id = str(uuid.uuid4())
        try:
            resp = requests.post(
                SUBMIT_URL, headers=self._headers(task_id),
                json=self._body(audio_b64, opts), timeout=(15, self.timeout),
            )
        except requests.RequestException as exc:
            raise ASRError(f"提交任务失败（网络异常）：{exc}") from exc

        status, message, logid, _ = self._parse(resp)
        if status != OK:
            raise ASRError(
                f"提交任务被拒绝：{message or '接口返回异常'}（HTTP {resp.status_code}）",
                code=status, logid=logid,
            )
        task_id = resp.headers.get("X-Api-Request-Id") or task_id

        # 轮询
        deadline = time.time() + 3600
        interval = 2.0
        while time.time() < deadline:
            try:
                r = requests.post(
                    QUERY_URL, headers=self._headers(task_id), json={},
                    timeout=(15, self.timeout),
                )
            except requests.RequestException:
                time.sleep(interval)
                interval = min(interval * 1.4, 8.0)
                continue

            st, msg, lg, body = self._parse(r)
            if st == OK:
                return body
            if st in PENDING:
                time.sleep(interval)
                interval = min(interval * 1.35, 8.0)
                continue
            raise ASRError(f"识别任务失败：{msg or '未知原因'}", code=st, logid=lg)

        raise ASRError("等待识别结果超时（超过 1 小时）")

    @staticmethod
    def _extract(body: dict, duration: float) -> TranscribeResult:
        result = body.get("result") if isinstance(body, dict) else None
        if isinstance(result, list):
            result = result[0] if result else {}
        if not isinstance(result, dict):
            result = {}

        text = (result.get("text") or "").strip()
        segments: list[Segment] = []

        for u in result.get("utterances") or []:
            if not isinstance(u, dict):
                continue
            t = (u.get("text") or "").strip()
            if not t:
                continue
            start = float(u.get("start_time") or 0) / 1000.0
            end = float(u.get("end_time") or 0) / 1000.0
            speaker = u.get("speaker")
            additions = u.get("additions")
            if speaker is None and isinstance(additions, dict):
                speaker = additions.get("speaker_id") or additions.get("speaker")
            segments.append(Segment(
                text=t, start=start, end=end,
                speaker=str(speaker) if speaker not in (None, "") else None,
            ))

        return TranscribeResult(
            text=text or "".join(s.text for s in segments),
            segments=segments,
            duration=duration,
            raw=body,
            estimated_time=False,
        )
