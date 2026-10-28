"""OpenAI 兼容的 LLM 客户端。

只做三件事：发请求、重试、把返回的 JSON 抠出来。
豆包方舟 / DeepSeek / 通义 / OpenAI 都用同一套协议，换 base_url 即可。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import requests
from shared.local_security import local_url


class LLMError(RuntimeError):
    pass


def _normalize_base(url: str) -> str:
    base = (url or "").strip().rstrip("/")
    if not base:
        raise LLMError("未配置 LLM 的 Base URL")
    if not base.endswith("/v1") and "/v1" not in base and "/api/v3" not in base:
        base += "/v1"
    return base


def is_local_endpoint(url: str) -> bool:
    """本地推理服务（Ollama / LM Studio / llama.cpp）不校验 API Key。"""
    return local_url(url)


def extract_json(text: str) -> dict:
    """从模型输出里抠出 JSON。

    模型常常会加 ```json 围栏、前置说明或尾逗号，这里逐级兜底。
    """
    if not text:
        raise LLMError("模型返回为空")

    raw = text.strip()

    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        candidate = raw[start:end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # 去掉对象/数组末尾的多余逗号再试一次
            fixed = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                pass

    raise LLMError(f"模型没有返回合法 JSON：{raw[:200]}")


class LLMClient:
    def __init__(self, cfg: dict[str, Any]):
        self.api_key = (cfg.get("api_key") or "").strip()
        self.base_url = _normalize_base(cfg.get("base_url", ""))
        self.model = (cfg.get("model") or "").strip()
        self.temperature = float(cfg.get("temperature", 0.3))
        self.max_tokens = int(cfg.get("max_tokens", 8000))
        self.timeout = int(cfg.get("timeout", 300))
        self.input_chars = int(cfg.get("input_chars", 24000))
        self.usage = {"attempts": 0, "reported": False, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.on_usage = None
        # 本地推理服务完全绕开系统代理，否则开着梯子会连不上自己
        self.session = requests.Session()
        if is_local_endpoint(self.base_url):
            self.session.trust_env = False

    def validate(self) -> None:
        if not is_local_endpoint(self.base_url) and not self.api_key:
            raise LLMError("尚未配置大模型 API Key，请点右上角「设置」填写")
        if not self.model:
            raise LLMError("尚未填写模型名称（例如 deepseek-chat / ep-xxxx）")

    def chat(self, messages: list[dict], temperature: float | None = None,
             max_tokens: int | None = None, json_mode: bool = True) -> str:
        self.validate()
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key or 'local'}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }

        # 先尝试强制 JSON；少数网关不认这个字段，失败后自动去掉重试
        attempts = [dict(payload, response_format={"type": "json_object"})] if json_mode else []
        attempts.append(payload)

        last_err = ""
        for body in attempts:
            for retry in range(3):
                try:
                    self.usage["attempts"] += 1
                    if self.on_usage:
                        self.on_usage(dict(self.usage))
                    resp = self.session.post(url, headers=headers, json=body,
                                             timeout=(20, self.timeout))
                except requests.RequestException as exc:
                    last_err = f"网络异常：{exc}"
                    time.sleep(2.0 * (retry + 1))
                    continue

                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        usage = data.get("usage")
                        if isinstance(usage, dict):
                            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                                if isinstance(usage.get(key), int) and usage[key] >= 0:
                                    self.usage[key] += usage[key]
                                    self.usage["reported"] = True
                        if self.on_usage:
                            self.on_usage(dict(self.usage))
                        if data["choices"][0].get("finish_reason") == "length":
                            raise LLMError("模型输出被长度上限截断，请调高输出上限或降低单次输入预算后重试")
                        return data["choices"][0]["message"]["content"] or ""
                    except (ValueError, KeyError, IndexError) as exc:
                        last_err = f"返回格式异常：{exc}"
                        break

                last_err = self._error_text(resp)
                if resp.status_code in (400, 404, 422):
                    break            # 参数问题，换个 body 再试
                if resp.status_code in (429, 500, 502, 503, 504):
                    time.sleep(2.0 * (retry + 1))
                    continue
                break

        if self.api_key:
            last_err = last_err.replace(self.api_key, "[凭证已隐藏]")
        raise LLMError(last_err or "调用大模型失败")

    def chat_json(self, messages: list[dict], **kwargs: Any) -> dict:
        text = self.chat(messages, **kwargs)
        value = extract_json(text)
        if not isinstance(value, dict):
            raise LLMError("模型返回的 JSON 顶层必须是对象")
        return value

    @staticmethod
    def _error_text(resp: requests.Response) -> str:
        try:
            data = resp.json()
            err = data.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or err
            else:
                msg = err or data.get("message") or data
        except ValueError:
            msg = (resp.text or "")[:300]

        hint = ""
        if resp.status_code == 401:
            hint = "（API Key 无效）"
        elif resp.status_code == 404:
            hint = "（Base URL 或模型名不存在）"
        elif resp.status_code == 403:
            hint = "（没有该模型的权限，可能未开通）"
        return f"大模型返回 {resp.status_code}：{msg}{hint}"
