"""语音转录工坊 —— 本地网页应用入口。

启动：python app.py
"""
from __future__ import annotations

import io
import os
import socket
import sys
import threading
import time
import uuid
import webbrowser
import zipfile
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

from core import config
from core.pipeline import store
from shared import audio_utils as audio
from shared.asr import catalog, spec_of
from shared.version import __version__

WEB_DIR = config.ROOT / "web"
SHARED_WEB = config.ROOT.parent / "shared" / "web"
MAX_UPLOAD = 2 * 1024 * 1024 * 1024  # 2GB

ALLOWED_EXT = {
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".flac",
    ".amr", ".wma", ".opus", ".silk", ".webm",
    ".mp4", ".mov", ".mkv", ".avi", ".flv",
}

MIME = {
    "md": "text/markdown",
    "txt": "text/plain",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD
app.config["JSON_AS_ASCII"] = False


def _clean_secret(val) -> str | None:
    """空值或脱敏掩码表示「不修改」。"""
    if not isinstance(val, str):
        return None
    v = val.strip()
    if not v or set(v) == {"*"} or "******" in v:
        return None
    return v


# ---------------- 页面 ----------------

@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/static/<path:filename>")
def static_files(filename: str):
    """先找本应用的 web 目录，再找 shared/web（两个应用共用的主题样式）。"""
    for base in (WEB_DIR, SHARED_WEB):
        target = (base / filename).resolve()
        try:
            target.relative_to(base.resolve())
        except ValueError:
            continue
        if target.is_file():
            return send_from_directory(target.parent, target.name)
    return jsonify({"ok": False, "error": "文件不存在"}), 404


# ---------------- 基础信息 ----------------

@app.get("/api/providers")
def providers():
    return jsonify(catalog())


@app.get("/api/health")
def health():
    cfg = config.load()
    ffmpeg = audio.find_ffmpeg()
    pid = cfg.get("provider", "volc")
    spec = spec_of(pid)
    resp = {
        "ok": True,
        "ffmpeg": bool(ffmpeg),
        "ffmpeg_path": ffmpeg or "",
        "provider": pid,
        "ready": config.provider_ready(pid, cfg),
        "ready_map": {p: config.provider_ready(p, cfg) for p in cfg["providers"]},
        "mock": cfg.get("mock", False),
        "max_segment_minutes": max(1, spec.max_segment_seconds // 60),
        "version": __version__,
    }
    if not ffmpeg:
        resp["warning"] = "未检测到 ffmpeg，无法处理音频。请安装后重启本应用。"
    return jsonify(resp)


@app.get("/api/config")
def get_config():
    return jsonify(config.public_view(config.load()))


@app.post("/api/config")
def set_config():
    data = request.get_json(silent=True) or {}
    patch: dict = {}

    for key in ("provider", "language", "segment_minutes", "outputs"):
        if key in data:
            patch[key] = data[key]
    for key in ("include_timeline", "mock"):
        if key in data:
            patch[key] = bool(data[key])

    incoming = data.get("providers")
    if isinstance(incoming, dict):
        cleaned: dict = {}
        for pid, vals in incoming.items():
            if not isinstance(vals, dict):
                continue
            item = {}
            for k, v in vals.items():
                if k in ("api_key", "access_key"):
                    secret = _clean_secret(v)
                    if secret:
                        item[k] = secret
                elif k in ("enable_itn", "enable_punc", "enable_ddc",
                           "enable_speaker_info", "force_verbose", "vad"):
                    item[k] = bool(v)
                elif k in ("mode", "resource_id", "auth_mode", "model",
                           "base_url", "prompt", "device", "compute_type", "app_id",
                           "model_dir"):
                    item[k] = v
                elif k == "threads":
                    try:
                        item[k] = max(0, int(v))
                    except (TypeError, ValueError):
                        pass
                elif k in ("group_seconds", "gap_seconds"):
                    try:
                        item[k] = float(v)
                    except (TypeError, ValueError):
                        pass
            cleaned[pid] = item
        patch["providers"] = cleaned

    cfg = config.save(patch)
    return jsonify({"ok": True, "config": config.public_view(cfg)})


# ---------------- 上传与任务 ----------------

@app.post("/api/upload")
def upload():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"ok": False, "error": "没有收到文件"}), 400

    cfg = config.load()
    config.ensure_dirs()

    if not cfg.get("mock") and not config.provider_ready(cfg.get("provider", "volc"), cfg):
        return jsonify({
            "ok": False,
            "error": "当前识别引擎还没配置好凭证。请点右上角「设置」填写，或先打开演示模式试用。",
        }), 400

    accepted, rejected = [], []
    for f in files:
        name = f.filename or ""
        ext = Path(name).suffix.lower()
        if not name or ext not in ALLOWED_EXT:
            rejected.append({"name": name, "reason": f"不支持的格式 {ext or '(无扩展名)'}"})
            continue

        stored = config.UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
        f.save(str(stored))
        size = stored.stat().st_size

        if size == 0:
            stored.unlink(missing_ok=True)
            rejected.append({"name": name, "reason": "文件为空"})
            continue

        job = store.create(filename=name, src_path=str(stored), size=size)
        store.submit(job.id, cfg)
        accepted.append(job.to_dict())

    return jsonify({"ok": True, "accepted": accepted, "rejected": rejected})


@app.get("/api/jobs")
def list_jobs():
    return jsonify([j.to_dict() for j in store.all()])


@app.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id: str):
    job = store.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    store.update(job_id, canceled=True, stage="正在取消")
    return jsonify({"ok": True})


@app.post("/api/jobs/<job_id>/retry")
def retry_job(job_id: str):
    job = store.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    if not Path(job.src_path).exists():
        return jsonify({"ok": False, "error": "原始音频已被清理，请重新上传"}), 400

    store.update(job_id, status="queued", stage="重新排队", progress=0,
                 error="", segments_done=0, segments_total=0,
                 outputs={}, canceled=False, finished_at=0.0)
    store.submit(job_id, config.load())
    return jsonify({"ok": True})


@app.delete("/api/jobs/<job_id>")
def delete_job(job_id: str):
    job = store.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "任务不存在"}), 404

    for p in (job.outputs or {}).values():
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass

    if job.status in ("done", "failed", "canceled"):
        try:
            Path(job.src_path).unlink(missing_ok=True)
        except OSError:
            pass

    out_dir = config.OUTPUT_DIR / job_id
    if out_dir.exists():
        for f in out_dir.iterdir():
            try:
                f.unlink()
            except OSError:
                pass
        try:
            out_dir.rmdir()
        except OSError:
            pass

    store.remove(job_id)
    return jsonify({"ok": True})


@app.get("/api/jobs/<job_id>/preview")
def preview(job_id: str):
    job = store.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "任务不存在"}), 404

    path = job.outputs.get("md") or job.outputs.get("txt") or job.outputs.get("docx")
    if not path or not Path(path).exists():
        if job.status == "failed":
            return jsonify({"ok": False, "error": job.error or "任务失败"}), 400
        return jsonify({"ok": False, "error": "结果还没有生成"}), 400

    if path.endswith(".docx"):
        return jsonify({"ok": True, "format": "docx", "content": ""})

    content = Path(path).read_text(encoding="utf-8", errors="replace")
    return jsonify({"ok": True, "format": Path(path).suffix.lstrip("."), "content": content})


# ---------------- 下载 ----------------

@app.get("/api/download/<job_id>/<fmt>")
def download(job_id: str, fmt: str):
    job = store.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "任务不存在"}), 404

    path = job.outputs.get(fmt)
    if not path or not Path(path).exists():
        return jsonify({"ok": False, "error": "该格式尚未生成"}), 404

    return send_file(
        path, as_attachment=True,
        download_name=f"{Path(job.filename).stem}.{fmt}",
        mimetype=MIME.get(fmt, "application/octet-stream"),
    )


def _zip_jobs(jobs, archive_name: str):
    """把若干任务的产物打成一个 zip，按「文件名/格式」分目录。"""
    buf = io.BytesIO()
    count = 0
    used: set[str] = set()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for job in jobs:
            stem = Path(job.filename).stem or job.id
            # 同名音频可能被上传多次，加序号避免互相覆盖
            base, n = stem, 1
            while base in used:
                n += 1
                base = f"{stem}({n})"
            used.add(base)

            for fmt, p in (job.outputs or {}).items():
                fp = Path(p)
                if fp.exists():
                    zf.write(fp, f"{base}/{base}.{fmt}")
                    count += 1

    if count == 0:
        return None
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=archive_name,
                     mimetype="application/zip")


@app.get("/api/download-all")
def download_all():
    """批量打包：ids 为空时打包全部已完成任务。"""
    ids = [x for x in (request.args.get("ids") or "").split(",") if x]
    jobs = store.all()
    if ids:
        wanted = set(ids)
        jobs = [j for j in jobs if j.id in wanted]
    jobs = [j for j in jobs if j.status == "done" and j.outputs]

    if not jobs:
        return jsonify({"ok": False, "error": "没有可打包的结果"}), 400

    stamp = datetime.now().strftime("%m%d_%H%M")
    resp = _zip_jobs(jobs, f"转录结果_{stamp}.zip")
    if resp is None:
        return jsonify({"ok": False, "error": "结果文件已丢失，请重新生成"}), 400
    return resp


@app.get("/api/download/<job_id>/zip")
def download_job_zip(job_id: str):
    job = store.get(job_id)
    if not job or not job.outputs:
        return jsonify({"ok": False, "error": "该任务还没有可下载的结果"}), 404
    stem = Path(job.filename).stem or job_id
    resp = _zip_jobs([job], f"{stem}.zip")
    if resp is None:
        return jsonify({"ok": False, "error": "结果文件已丢失"}), 400
    return resp


@app.errorhandler(413)
def too_large(_):
    return jsonify({"ok": False, "error": "文件超过 2GB 上限"}), 413


# ---------------- 端侧模型 ----------------

MODEL_JOB: dict = {}


@app.get("/api/local/status")
def local_status():
    try:
        from shared.asr.sensevoice import model_status
        status = model_status()
    except Exception as exc:  # noqa: BLE001
        status = {"ready": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        import sherpa_onnx  # noqa: F401
        status["installed_pkg"] = True
    except ImportError:
        status["installed_pkg"] = False

    return jsonify({"asr": status})


@app.post("/api/local/asr/download")
def start_model_download():
    if MODEL_JOB.get("status") == "running":
        return jsonify({"ok": True, "job": MODEL_JOB})

    MODEL_JOB.clear()
    MODEL_JOB.update({"status": "running", "stage": "准备下载", "pct": 0.0, "error": ""})

    def worker():
        try:
            from shared.asr.sensevoice import download_model
            download_model(MODEL_JOB)
            MODEL_JOB.update({"status": "done", "stage": "已就绪", "pct": 100.0})
        except Exception as exc:  # noqa: BLE001
            MODEL_JOB.update({"status": "failed",
                              "error": f"{type(exc).__name__}: {exc}"})

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "job": MODEL_JOB})


@app.get("/api/local/asr/download")
def model_download_state():
    return jsonify({"job": MODEL_JOB})


# ---------------- 启动 ----------------

def _free_port(start: int = 8756, tries: int = 20) -> int:
    for offset in range(tries):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    config.ensure_dirs()
    port = int(os.environ.get("PORT") or _free_port())
    url = f"http://127.0.0.1:{port}"

    print("=" * 56)
    print("  语音转录工坊")
    print(f"  访问地址: {url}")
    if not audio.find_ffmpeg():
        print("  [!] 未检测到 ffmpeg，音频处理将不可用")
    print("  关闭本窗口即可停止服务")
    print("=" * 56)

    if "--no-browser" not in sys.argv:
        threading.Thread(
            target=lambda: (time.sleep(1.0), webbrowser.open(url)),
            daemon=True,
        ).start()

    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
