"""全局配置与路径管理。

配置分两层：
- 顶层：与厂商无关的选项（当前引擎、语言、分段、输出格式）
- providers：每家厂商各自的凭证与特有参数，切换引擎时互不覆盖

配置保存在 data/config.json，读时补默认值，避免升级后旧配置缺字段。
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
# 允许用环境变量把数据挪到别处：便携版放 U 盘、多份数据并行、自动化测试不碰真实数据
DATA_DIR = Path(os.environ.get("TRANSCRIBER_DATA_DIR") or (ROOT / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
WORK_DIR = DATA_DIR / "work"
CONFIG_PATH = DATA_DIR / "config.json"

# 火山引擎 v3 资源 ID
RESOURCE_STANDARD_20 = "volc.seedasr.auc"      # 豆包录音文件识别模型 2.0（异步）
RESOURCE_STANDARD_10 = "volc.bigasr.auc"       # 豆包录音文件识别模型 1.0（异步）
RESOURCE_FLASH_20 = "volc.seedasr.auc_turbo"   # 模型 2.0 极速版（同步）
RESOURCE_FLASH_10 = "volc.bigasr.auc_turbo"    # 模型 1.0 极速版（同步）

PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "volc": {
        "auth_mode": "api_key",       # api_key（新版控制台） | legacy（APP ID + Token）
        "api_key": "",
        "app_id": "",
        "access_key": "",
        "mode": "standard",           # standard（异步） | flash（同步）
        "resource_id": RESOURCE_STANDARD_20,
        "enable_itn": True,
        "enable_punc": True,
        "enable_ddc": False,
        "enable_speaker_info": False,
    },
    "openai": {
        "api_key": "",
        "base_url": "https://api.openai.com/v1",
        "model": "whisper-1",
        "prompt": "",
        "force_verbose": False,
    },
    "dashscope": {
        "api_key": "",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3-asr-flash",
    },
    "local": {
        "model": "small",
        "device": "cpu",
        "compute_type": "int8",
        "vad": True,
    },
    "sensevoice": {
        "threads": 0,          # 0 = 自动按 CPU 核数
        "enable_itn": True,
        "group_seconds": 25,
        "model_dir": "",
    },
}

DEFAULTS: dict[str, Any] = {
    "provider": "volc",
    "language": "",                  # 空 = 自动识别
    "segment_minutes": 20,
    "outputs": ["md", "docx"],
    "include_timeline": True,
    "mock": False,
    "providers": PROVIDER_DEFAULTS,
}

_lock = threading.Lock()


def ensure_dirs() -> None:
    for d in (DATA_DIR, UPLOAD_DIR, OUTPUT_DIR, WORK_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _merge_providers(raw: Any) -> dict[str, dict[str, Any]]:
    merged = copy.deepcopy(PROVIDER_DEFAULTS)
    if isinstance(raw, dict):
        for pid, vals in raw.items():
            if pid in merged and isinstance(vals, dict):
                merged[pid].update({k: v for k, v in vals.items() if k in merged[pid]})
    return merged


LEGACY_VOLC_KEYS = (
    "auth_mode", "api_key", "app_id", "access_key", "mode", "resource_id",
    "enable_itn", "enable_punc", "enable_ddc", "enable_speaker_info",
)


def _migrate_legacy(raw: dict[str, Any]) -> dict[str, Any]:
    """把 1.0 的扁平配置（api_key 等直接放在顶层）迁移成 providers 结构。

    不迁移的话，升级后旧配置里的火山密钥会被当成未知字段丢掉。
    """
    if isinstance(raw.get("providers"), dict) and raw["providers"]:
        return raw

    volc = {k: raw[k] for k in LEGACY_VOLC_KEYS if k in raw}
    if not volc:
        return raw

    out = dict(raw)
    out["providers"] = {"volc": volc}
    return out


def load() -> dict[str, Any]:
    ensure_dirs()
    cfg = copy.deepcopy(DEFAULTS)
    cfg["providers"] = copy.deepcopy(PROVIDER_DEFAULTS)

    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                raw = _migrate_legacy(raw)
                for k, v in raw.items():
                    if k in DEFAULTS and k != "providers":
                        cfg[k] = v
                cfg["providers"] = _merge_providers(raw.get("providers"))
        except (json.JSONDecodeError, OSError):
            pass

    return _validate(cfg)


def save(patch: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        cfg = load()
        for k, v in patch.items():
            if k in DEFAULTS and k != "providers" and v is not None:
                cfg[k] = v

        incoming = patch.get("providers")
        if isinstance(incoming, dict):
            for pid, vals in incoming.items():
                if pid in cfg["providers"] and isinstance(vals, dict):
                    for k, v in vals.items():
                        if k in cfg["providers"][pid] and v is not None:
                            cfg["providers"][pid][k] = v

        cfg = _validate(cfg)
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return cfg


def _validate(cfg: dict[str, Any]) -> dict[str, Any]:
    if cfg.get("provider") not in PROVIDER_DEFAULTS:
        cfg["provider"] = "volc"

    try:
        cfg["segment_minutes"] = max(1, min(120, int(cfg.get("segment_minutes", 20))))
    except (TypeError, ValueError):
        cfg["segment_minutes"] = 20

    outs = cfg.get("outputs") or ["md", "docx"]
    outs = [o for o in outs if o in ("md", "docx", "txt")]
    cfg["outputs"] = outs or ["md"]

    for key in ("include_timeline", "mock"):
        cfg[key] = bool(cfg.get(key))

    volc = cfg["providers"]["volc"]
    if volc.get("auth_mode") not in ("api_key", "legacy"):
        volc["auth_mode"] = "api_key"
    if volc.get("mode") not in ("standard", "flash"):
        volc["mode"] = "standard"
    valid_res = {RESOURCE_STANDARD_20, RESOURCE_STANDARD_10,
                 RESOURCE_FLASH_20, RESOURCE_FLASH_10}
    if volc.get("resource_id") not in valid_res:
        volc["resource_id"] = (
            RESOURCE_STANDARD_20 if volc.get("mode") == "standard" else RESOURCE_FLASH_20
        )
    for key in ("enable_itn", "enable_punc", "enable_ddc", "enable_speaker_info"):
        volc[key] = bool(volc.get(key))

    if cfg["providers"]["local"].get("model") not in (
        "tiny", "base", "small", "medium", "large-v3", "distil-large-v3"
    ):
        cfg["providers"]["local"]["model"] = "small"

    return cfg


def mask(secret: str) -> str:
    if not secret:
        return ""
    s = str(secret)
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}{'*' * 6}{s[-4:]}"


def provider_ready(provider_id: str, cfg: dict[str, Any]) -> bool:
    p = cfg.get("providers", {}).get(provider_id, {})
    if provider_id == "volc":
        if p.get("auth_mode", "api_key") == "api_key":
            return bool(p.get("api_key"))
        return bool(p.get("app_id") and p.get("access_key"))
    if provider_id == "local":
        return True          # 本地引擎不需要凭证
    if provider_id == "sensevoice":
        try:
            from shared.asr.sensevoice import model_status
            return bool(model_status()["ready"])
        except Exception:    # noqa: BLE001  没装依赖或没下模型
            return False
    return bool(p.get("api_key"))


def public_view(cfg: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(cfg)
    for _pid, p in out["providers"].items():
        for key in ("api_key", "access_key"):
            if key in p:
                p[key] = mask(p.get(key, ""))
    out.pop("ready", None)
    out["ready"] = provider_ready(cfg.get("provider", "volc"), cfg)
    out["ready_map"] = {pid: provider_ready(pid, cfg) for pid in cfg["providers"]}
    return out
