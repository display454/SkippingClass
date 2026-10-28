"""「不听课」配置与路径。

语音识别配置默认继承隔壁「音频转文本」应用的设置——同一个模型没必要配两遍。
LLM 配置独立，因为分析用的语言模型和转写用的语音模型是两回事。
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

import requests
from shared.storage import write_json
from shared.local_security import bind_endpoint, local_url, public_config

ROOT = Path(__file__).resolve().parent.parent
# 数据目录可用环境变量重定向，便于隔离测试或把数据放到别处
DATA_DIR = Path(os.environ.get("BUKETANG_DATA_DIR") or (ROOT / "data")).resolve()
LESSON_DIR = DATA_DIR / "lessons"
UPLOAD_DIR = DATA_DIR / "uploads"
CONFIG_PATH = DATA_DIR / "config.json"

# 隔壁应用的配置，用于继承语音引擎。允许用环境变量指向别处，
# 这样单独打包「不听课」时也能把配置放到任意位置。
PEER_CONFIG = Path(
    os.environ.get("BUKETANG_PEER_CONFIG")
    or (ROOT.parent / "mp3-transcriber" / "data" / "config.json")
)

LLM_PRESETS: list[dict[str, str]] = [
    {
        "id": "ark",
        "name": "豆包 · 火山方舟",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "hint": "模型填方舟控制台的接入点 ID（ep-xxx）或模型名",
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "hint": "模型填 deepseek-chat",
    },
    {
        "id": "qwen",
        "name": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "hint": "模型填 qwen-plus / qwen-max",
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "hint": "模型填 gpt-4o / gpt-4o-mini",
    },
    {
        "id": "ollama",
        "name": "本地 Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "hint": "先装 Ollama 并 ollama pull qwen3:8b，模型名填 qwen3:8b",
        "local": "1",
    },
    {
        "id": "custom",
        "name": "自定义",
        "base_url": "",
        "hint": "任何 OpenAI 兼容端点",
    },
]

DEFAULTS: dict[str, Any] = {
    "llm": {
        "preset": "ark",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key": "",
        "model": "",
        "vision_model": "",
        "temperature": 0.3,
        "max_tokens": 8000,
        "timeout": 300,
        "input_chars": 24000,
    },
    "asr": None,          # None = 继承音频应用的配置
    "guide": {
        "extreme_max_points": 12,     # 极限型最多留几个考点
        "summary_top": 15,            # 期末总复习「最该背」保留几条
        "detailed_with_shots": True,  # 详细型是否插入讲义截图
        "shot_dpi": 130,
        "shot_quality": 82,
        "max_shots": 3,
        "preprocess": True,
    },
}

_lock = threading.Lock()


def ensure_dirs() -> None:
    for d in (DATA_DIR, LESSON_DIR, UPLOAD_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _deep_merge(base: dict, patch: dict, allow_new: bool = False) -> dict:
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v, allow_new=allow_new or k == "asr")
        elif k in out or allow_new:
            out[k] = v
    return out


def load() -> dict[str, Any]:
    ensure_dirs()
    cfg = copy.deepcopy(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg = _deep_merge(cfg, raw)
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save(patch: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        before = load()
        cfg = _deep_merge(before, patch or {})
        bind_endpoint(before["llm"], cfg["llm"], patch.get("llm") or {})
        old_providers = ((before.get("asr") or {}).get("providers") or {})
        for pid, values in ((cfg.get("asr") or {}).get("providers") or {}).items():
            bind_endpoint(old_providers.get(pid, {}), values,
                          ((patch.get("asr") or {}).get("providers") or {}).get(pid, {}))
        write_json(CONFIG_PATH, cfg)
        return cfg


def mask(secret: str) -> str:
    if not secret:
        return ""
    s = str(secret)
    return "*" * len(s) if len(s) <= 8 else f"{s[:4]}{'*' * 6}{s[-4:]}"


def _is_local_url(url: str) -> bool:
    return local_url(url)


def llm_ready(cfg: dict[str, Any]) -> bool:
    llm = cfg.get("llm", {})
    if not (llm.get("base_url") and llm.get("model")):
        return False
    # 本地推理服务不需要 API Key
    return bool(llm.get("api_key")) or _is_local_url(llm.get("base_url", ""))


def ollama_status(base_url: str = "") -> dict[str, Any]:
    """探测本机 Ollama：有没有在跑、装了哪些模型、哪个适合本机内存。"""
    base = (base_url or "http://127.0.0.1:11434/v1").rstrip("/")
    root = base[:-3] if base.endswith("/v1") else base
    out: dict[str, Any] = {"running": False, "models": [], "url": root, "error": ""}
    try:
        # 本机地址必须完全绕开系统代理，否则开着梯子时会拿到 502/401
        session = requests.Session()
        session.trust_env = False
        resp = session.get(f"{root}/api/tags", timeout=2.5)
        if resp.status_code != 200:
            out["error"] = f"HTTP {resp.status_code}"
            return out
        data = resp.json()
        models = []
        for m in data.get("models") or []:
            name = m.get("name") or m.get("model") or ""
            if not name:
                continue
            models.append({
                "name": name,
                "size": int(m.get("size") or 0),
                "family": (m.get("details") or {}).get("family", ""),
            })
        models.sort(key=lambda x: x["size"])
        out["models"] = models
        out["running"] = True
    except requests.RequestException as exc:
        out["error"] = f"连不上：{type(exc).__name__}"
    return out


def recommend_local_model(total_ram_gb: float) -> dict[str, str]:
    """按内存给出建议，避免用户拉个跑不动的模型。"""
    if total_ram_gb >= 30:
        return {"name": "qwen3:14b", "note": "内存充足，质量接近云端"}
    if total_ram_gb >= 15:
        return {"name": "qwen3:8b", "note": "16GB 内存的甜点，约 5GB"}
    return {"name": "qwen3:4b", "note": "8GB 内存推荐，约 2.5GB"}


def asr_inherited(cfg: dict[str, Any] | None = None) -> bool:
    """本应用没单独配语音引擎，**且确实有隔壁应用的配置可以继承**。

    单应用安装包里没有隔壁应用，此时用的是内置默认值，不该说成「继承」——
    否则用户会去隔壁找根本没安装的设置项。
    """
    cfg = cfg or load()
    return cfg.get("asr") in (None, {}) and _peer_asr() is not None


def public_view(cfg: dict[str, Any]) -> dict[str, Any]:
    out = public_config(cfg)
    out["llm_ready"] = llm_ready(cfg)
    out["presets"] = LLM_PRESETS
    out["llm_is_local"] = _is_local_url(cfg.get("llm", {}).get("base_url", ""))
    asr = asr_config(cfg)
    provs = asr.get("providers") or {}
    pid = asr.get("provider", "volc")
    cur = provs.get(pid) or {}
    out["asr_info"] = {
        "provider": pid,
        "inherited": asr_inherited(cfg),
        "ready": asr_ready(asr),
        # 只暴露「有没有填过」，不回传明文，前端据此决定是否提示要重填
        "has_key": {
            k: bool((v or {}).get("api_key") or (v or {}).get("access_key"))
            for k, v in provs.items() if isinstance(v, dict)
        },
        "base_url": cur.get("base_url", ""),
        "model": cur.get("model", ""),
    }
    return out


VOLC_KEYS = (
    "auth_mode", "api_key", "app_id", "access_key", "mode", "resource_id",
    "enable_itn", "enable_punc", "enable_ddc", "enable_speaker_info",
)


def _peer_asr() -> dict[str, Any] | None:
    """读隔壁音频应用的配置，同时兼容 1.0 的扁平格式和 2.0 的 providers 格式。"""
    if not PEER_CONFIG.exists():
        return None
    try:
        peer = json.loads(PEER_CONFIG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(peer, dict):
        return None

    providers = peer.get("providers")
    if not isinstance(providers, dict) or not providers:
        legacy = {k: peer[k] for k in VOLC_KEYS if k in peer}
        if not legacy:
            return None
        providers = {"volc": legacy}

    return {
        "provider": peer.get("provider") or "volc",
        "providers": providers,
        "language": peer.get("language", ""),
        "segment_minutes": peer.get("segment_minutes", 20),
    }


def asr_config(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """语音引擎配置：优先用本应用自己配的，否则继承隔壁音频应用。

    cfg["asr"] 可以只写一个 provider，表示「沿用隔壁的凭证，但换一个引擎」，
    这样用户可以在本应用里单独切到本地 SenseVoice，不影响音频应用。
    """
    cfg = cfg or load()
    own = cfg.get("asr")

    base = _peer_asr() or {
        # 没有隔壁应用可继承（单应用安装包）时默认端侧 SenseVoice：
        # 五家里只有它不需要任何 Key，装上模型就能用。
        "provider": "sensevoice",
        "providers": {},
        "language": "",
        "segment_minutes": 20,
    }

    # 本应用的设置逐项覆盖，没写的仍然沿用隔壁应用——
    # 只填了一个 API Key 也不该把隔壁的语言、分段长度、其他引擎凭证弄丢。
    if isinstance(own, dict):
        base = dict(base)
        if own.get("provider"):
            base["provider"] = own["provider"]
        providers = dict(base.get("providers") or {})
        for pid, values in (own.get("providers") or {}).items():
            previous = providers.get(pid, {})
            providers[pid] = {**previous, **values}
            bind_endpoint(previous, providers[pid], values)
        base["providers"] = providers
        for key in ("segment_minutes", "language"):
            if opt := own.get(key):
                base[key] = opt

    return base


def asr_ready(asr: dict[str, Any]) -> bool:
    pid = asr.get("provider", "volc")
    p = (asr.get("providers") or {}).get(pid, {})

    if pid == "sensevoice":
        try:
            from shared.asr.sensevoice import model_status
            return bool(model_status()["ready"])
        except Exception:  # noqa: BLE001  引擎不可用时按未就绪处理
            return False

    if pid == "local":
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False
        return True

    if pid == "volc":
        if p.get("auth_mode", "api_key") == "api_key":
            return bool(p.get("api_key"))
        return bool(p.get("app_id") and p.get("access_key"))

    return bool(p.get("api_key"))
