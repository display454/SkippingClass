"""「不听课」—— 课堂录音 + 讲义 -> 考前复习指导。

启动：python app.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
import zipfile
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

from core import config, courses as course_store, summarize
from core.backup import prepare_restore
from core.store import STATUS_LABEL, store, RUNNING_STATUS
from shared.version import __version__

WEB_DIR = config.ROOT / "web"
SHARED_WEB = config.ROOT.parent / "shared" / "web"
MAX_UPLOAD = 2 * 1024 * 1024 * 1024

MATERIAL_EXT = {".pdf", ".ppt", ".pptx", ".doc", ".docx", ".md", ".markdown", ".txt"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".amr",
             ".wma", ".opus", ".webm", ".mp4", ".mov", ".mkv"}

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD
app.config["JSON_AS_ASCII"] = False


def _status_label(lesson: dict) -> str:
    """状态文案：已经有结果就别再显示「待分析」，那会让人以为白跑了。"""
    label = STATUS_LABEL.get(lesson["status"], lesson["status"])
    if lesson.get("guides") and lesson["status"] in ("draft", "canceled"):
        return "已有结果"
    return label


def serialize(lesson: dict, detail: bool = False) -> dict:
    analysis = lesson.get("analysis") or {}
    key_points = analysis.get("key_points") or []

    # 侧栏搜索要能搜到考点，不只是课堂名。这里把可搜文本预先拼好，
    # 前端不必为了搜索再逐堂请求详情。
    needles = [lesson.get("name") or "", analysis.get("topic") or ""]
    needles += [kp.get("title") or "" for kp in key_points if isinstance(kp, dict)]

    out = {
        "id": lesson["id"],
        "name": lesson["name"],
        "course_id": lesson.get("course_id", ""),
        "created": lesson["created"],
        "updated": lesson["updated"],
        "status": lesson["status"],
        "status_label": _status_label(lesson),
        "stage": lesson.get("stage", ""),
        "progress": lesson.get("progress", 0),
        "error": lesson.get("error", ""),
        "stale": bool(lesson.get("stale")),
        "search_text": " ".join(x for x in needles if x),
        "materials": [
            {k: v for k, v in m.items() if k != "pages"}
            for m in lesson.get("materials", [])
        ],
        "audios": [
            {k: v for k, v in a.items() if k not in ("segments", "path")}
            for a in lesson.get("audios", [])
        ],
        "guides": sorted((lesson.get("guides") or {}).keys()),
        "has_transcript": bool(lesson.get("transcript_text")),
        "duration": round(sum(a.get("duration", 0) for a in lesson.get("audios", [])), 1),
        "points": len(key_points),
    }
    if detail:
        out["analysis"] = lesson.get("analysis")
        out["transcript_preview"] = (lesson.get("transcript_text") or "")[:1200]
    return out


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


# ---------------- 基础 ----------------

@app.get("/api/health")
def health():
    cfg = config.load()
    asr = config.asr_config(cfg)
    return jsonify({
        "ok": True,
        "llm_ready": config.llm_ready(cfg),
        "asr_provider": asr.get("provider", "volc"),
        "asr_inherited": config.asr_inherited(cfg),
        "office": os.name == "nt",
        "version": __version__,
    })


@app.get("/api/config")
def get_config():
    return jsonify(config.public_view(config.load()))


@app.post("/api/config")
def set_config():
    data = request.get_json(silent=True) or {}
    patch: dict = {}

    llm = data.get("llm")
    if isinstance(llm, dict):
        item: dict = {}
        for key in ("preset", "base_url", "model"):
            if isinstance(llm.get(key), str):
                item[key] = llm[key].strip()
        if llm.get("api_key") and set(str(llm["api_key"])) != {"*"} and "******" not in str(llm["api_key"]):
            item["api_key"] = str(llm["api_key"]).strip()
        for key in ("temperature", "max_tokens", "timeout"):
            if key in llm:
                try:
                    item[key] = float(llm[key]) if key == "temperature" else int(llm[key])
                except (TypeError, ValueError):
                    pass
        patch["llm"] = item

    guide = data.get("guide")
    if isinstance(guide, dict):
        g: dict = {}
        if "extreme_max_points" in guide:
            try:
                g["extreme_max_points"] = max(5, min(30, int(guide["extreme_max_points"])))
            except (TypeError, ValueError):
                pass
        if "detailed_with_shots" in guide:
            g["detailed_with_shots"] = bool(guide["detailed_with_shots"])
        if "summary_top" in guide:
            try:
                g["summary_top"] = max(8, min(40, int(guide["summary_top"])))
            except (TypeError, ValueError):
                pass
        patch["guide"] = g

    asr = data.get("asr")
    if asr == "inherit":
        patch["asr"] = None
    elif isinstance(asr, dict) and asr.get("provider"):
        item: dict = {"provider": asr["provider"]}
        provs = asr.get("providers")
        if isinstance(provs, dict):
            clean: dict = {}
            for pid, vals in provs.items():
                if not isinstance(vals, dict):
                    continue
                entry: dict = {}
                for key in ("api_key", "app_id", "access_key"):
                    val = vals.get(key)
                    # 空值或脱敏掩码表示「不修改」，避免把 ****** 存进去
                    if isinstance(val, str) and val.strip() and set(val.strip()) != {"*"} \
                            and "******" not in val:
                        entry[key] = val.strip()
                for key in ("base_url", "model", "resource_id", "auth_mode", "mode"):
                    val = vals.get(key)
                    if isinstance(val, str) and val.strip():
                        entry[key] = val.strip()
                if entry:
                    clean[pid] = entry
            if clean:
                item["providers"] = clean
        patch["asr"] = item

    cfg = config.save(patch)
    return jsonify({"ok": True, "config": config.public_view(cfg)})


# ---------------- 课程 ----------------

SUMMARY_JOBS: dict[str, dict] = {}
SUMMARY_LOCK = threading.Lock()


def lessons_of_course(course_id: str) -> list[dict]:
    return [l for l in store.all() if (l.get("course_id") or "") == course_id]


def serialize_course(course: dict) -> dict:
    items = lessons_of_course(course["id"])
    done = [l for l in items if l.get("status") == "done"]
    out = dict(course)
    out.update({
        "lessons": len(items),
        "done": len(done),
        "points": sum(len((l.get("analysis") or {}).get("key_points") or []) for l in done),
        "summary": course.get("summary") or {},
        "job": SUMMARY_JOBS.get(course["id"]) if SUMMARY_JOBS.get(course["id"], {}).get("status") == "running" else None,
    })
    return out


@app.get("/api/courses")
def list_courses():
    return jsonify({
        "courses": [serialize_course(c) for c in course_store.courses.all()],
        "unassigned": sum(1 for l in store.all() if not l.get("course_id")),
    })


@app.post("/api/courses")
def create_course():
    data = request.get_json(silent=True) or {}
    course = course_store.courses.create(data.get("name", ""), data.get("term", ""))
    return jsonify({"ok": True, "course": serialize_course(course)})


@app.patch("/api/courses/<course_id>")
def update_course(course_id: str):
    data = request.get_json(silent=True) or {}
    course = course_store.courses.update(course_id, **data)
    if not course:
        return jsonify({"ok": False, "error": "课程不存在"}), 404
    return jsonify({"ok": True, "course": serialize_course(course)})


@app.delete("/api/courses/<course_id>")
def delete_course(course_id: str):
    moved = store.clear_course(course_id)
    ok = course_store.courses.delete(course_id)
    return jsonify({"ok": ok, "moved_lessons": moved})


@app.post("/api/courses/<course_id>/summary")
def make_summary(course_id: str):
    course = course_store.courses.get(course_id)
    if not course:
        return jsonify({"ok": False, "error": "课程不存在"}), 404

    with SUMMARY_LOCK:
        job = SUMMARY_JOBS.get(course_id)
        if job and job.get("status") == "running":
            return jsonify({"ok": True, "job": job})
        SUMMARY_JOBS[course_id] = {"status": "running", "stage": "合并考点", "error": ""}

    threading.Thread(target=_summary_worker, args=(course_id,), daemon=True).start()
    return jsonify({"ok": True, "job": SUMMARY_JOBS[course_id]})


def _summary_worker(course_id: str) -> None:
    job = SUMMARY_JOBS.setdefault(course_id, {})
    try:
        course = course_store.courses.get(course_id)
        if not course:
            raise summarize.SummarizeError("课程不存在")
        lessons = lessons_of_course(course_id)
        out_dir = config.DATA_DIR / "courses" / course_id
        files = summarize.build_summary(course, lessons, out_dir,
                                       config.load().get("guide", {}))
        job.update({"status": "done", "stage": "已生成"})
        course_store.courses.set_summary(course_id, {
            "files": files,
            "lessons": len([l for l in lessons if l.get("status") == "done"]),
            "generated": time.time(),
        })
    except summarize.SummarizeError as exc:
        job.update({"status": "failed", "stage": "汇总失败", "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        job.update({"status": "failed", "stage": "汇总失败",
                    "error": f"{type(exc).__name__}: {exc}"})


@app.get("/api/courses/<course_id>/summary")
def summary_state(course_id: str):
    course = course_store.courses.get(course_id)
    if not course:
        return jsonify({"ok": False, "error": "课程不存在"}), 404
    return jsonify({"ok": True, "summary": course.get("summary") or {},
                    "job": SUMMARY_JOBS.get(course_id)})


@app.get("/api/courses/<course_id>/summary/preview")
def summary_preview(course_id: str):
    course = course_store.courses.get(course_id)
    if not course:
        return jsonify({"ok": False, "error": "课程不存在"}), 404
    path = ((course.get("summary") or {}).get("files") or {}).get("summary_md")
    if not path or not Path(path).exists():
        return jsonify({"ok": False, "error": "还没有生成总复习"}), 404
    return jsonify({"ok": True, "content": Path(path).read_text(encoding="utf-8", errors="replace")})


@app.get("/api/courses/<course_id>/summary/download/<fmt>")
def summary_download(course_id: str, fmt: str):
    course = course_store.courses.get(course_id)
    if not course:
        return jsonify({"ok": False, "error": "课程不存在"}), 404
    path = ((course.get("summary") or {}).get("files") or {}).get(f"summary_{fmt}")
    if not path or not Path(path).exists():
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    label = (course.get("name") or "课程").replace("/", "-")
    return send_file(
        path, as_attachment=True,
        download_name=f"{label}_期末总复习.{fmt}",
        mimetype="text/markdown" if fmt == "md"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


# ---------------- 课堂 ----------------

@app.get("/api/lessons")
def list_lessons():
    course_id = request.args.get("course")
    items = store.all()
    if course_id is not None:
        items = [l for l in items if (l.get("course_id") or "") == course_id]
    return jsonify([serialize(l) for l in items])


@app.patch("/api/lessons/<lesson_id>")
def patch_lesson(lesson_id: str):
    lesson = store.get(lesson_id)
    if not lesson:
        return jsonify({"ok": False, "error": "课堂不存在"}), 404

    data = request.get_json(silent=True) or {}
    if isinstance(data.get("name"), str):
        store.rename(lesson_id, data["name"])
    if "course_id" in data:
        cid = str(data.get("course_id") or "")
        if cid and not course_store.courses.get(cid):
            return jsonify({"ok": False, "error": "课程不存在"}), 404
        store.set_course(lesson_id, cid)
    return jsonify({"ok": True, "lesson": serialize(store.get(lesson_id), detail=True)})


@app.post("/api/lessons/<lesson_id>/cancel")
def cancel_lesson(lesson_id: str):
    if not store.cancel(lesson_id):
        return jsonify({"ok": False, "error": "这堂课没有在运行"}), 400
    return jsonify({"ok": True})


@app.post("/api/lessons")
def create_lesson():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip() or f"课堂 {time.strftime('%m-%d %H:%M')}"
    lesson = store.create(name)

    cid = str(data.get("course_id") or "")
    if cid and course_store.courses.get(cid):
        store.set_course(lesson["id"], cid)
        lesson = store.get(lesson["id"])

    return jsonify({"ok": True, "lesson": serialize(lesson, detail=True)})


@app.get("/api/lessons/<lesson_id>")
def get_lesson(lesson_id: str):
    lesson = store.get(lesson_id)
    if not lesson:
        return jsonify({"ok": False, "error": "课堂不存在"}), 404
    return jsonify({"ok": True, "lesson": serialize(lesson, detail=True)})


@app.delete("/api/lessons/<lesson_id>")
def delete_lesson(lesson_id: str):
    store.delete(lesson_id)
    return jsonify({"ok": True})


@app.post("/api/lessons/<lesson_id>/materials")
def upload_materials(lesson_id: str):
    lesson = store.get(lesson_id)
    if not lesson:
        return jsonify({"ok": False, "error": "课堂不存在"}), 404

    files = request.files.getlist("files")
    if not files:
        return jsonify({"ok": False, "error": "没有收到文件"}), 400

    added, rejected = [], []
    for f in files:
        name = f.filename or ""
        ext = Path(name).suffix.lower()
        if ext not in MATERIAL_EXT:
            rejected.append({"name": name, "reason": f"不支持的讲义格式 {ext or '(无扩展名)'}"})
            continue
        stored = store.files_dir(lesson_id) / f"{uuid.uuid4().hex}{ext}"
        f.save(str(stored))
        mat = store.add_material(lesson_id, name, stored)
        added.append(mat["id"])

    return jsonify({"ok": True, "added": added, "rejected": rejected})


@app.post("/api/lessons/<lesson_id>/audios")
def upload_audios(lesson_id: str):
    lesson = store.get(lesson_id)
    if not lesson:
        return jsonify({"ok": False, "error": "课堂不存在"}), 404

    files = request.files.getlist("files")
    if not files:
        return jsonify({"ok": False, "error": "没有收到文件"}), 400

    added, rejected = [], []
    for f in files:
        name = f.filename or ""
        ext = Path(name).suffix.lower()
        if ext not in AUDIO_EXT:
            rejected.append({"name": name, "reason": f"不支持的音频格式 {ext or '(无扩展名)'}"})
            continue
        stored = store.files_dir(lesson_id) / f"{uuid.uuid4().hex}{ext}"
        f.save(str(stored))
        size = stored.stat().st_size
        if size == 0:
            stored.unlink(missing_ok=True)
            rejected.append({"name": name, "reason": "文件为空"})
            continue
        item = store.add_audio(lesson_id, name, stored, size)
        added.append(item["id"])

    return jsonify({"ok": True, "added": added, "rejected": rejected})


@app.delete("/api/lessons/<lesson_id>/materials/<material_id>")
def delete_material(lesson_id: str, material_id: str):
    store.remove_material(lesson_id, material_id)
    return jsonify({"ok": True})


@app.delete("/api/lessons/<lesson_id>/audios/<audio_id>")
def delete_audio(lesson_id: str, audio_id: str):
    store.remove_audio(lesson_id, audio_id)
    return jsonify({"ok": True})


@app.get("/api/lessons/<lesson_id>/audios/<audio_id>/file")
def audio_file(lesson_id: str, audio_id: str):
    """把原始录音回放给网页。课件听不懂的那一句，要能立刻回去听。"""
    lesson = store.get(lesson_id)
    if not lesson:
        return jsonify({"ok": False, "error": "课堂不存在"}), 404

    item = next((a for a in lesson.get("audios", []) if a.get("id") == audio_id), None)
    path = Path((item or {}).get("path") or "")
    if not path.is_file():
        return jsonify({"ok": False, "error": "录音文件不在了"}), 404

    # conditional=True 让浏览器能拖动进度条（服务端按 Range 返回）
    return send_file(str(path), conditional=True,
                     download_name=item.get("filename") or "audio",
                     mimetype=None)


@app.post("/api/lessons/<lesson_id>/run")
def run_lesson(lesson_id: str):
    lesson = store.get(lesson_id)
    if not lesson:
        return jsonify({"ok": False, "error": "课堂不存在"}), 404

    cfg = config.load()
    if not config.llm_ready(cfg):
        return jsonify({
            "ok": False,
            "error": "还没有配置大模型。请点右上角「设置」填写 API Key 与模型名。",
        }), 400

    asr = config.asr_config(cfg)
    if not config.asr_ready(asr):
        return jsonify({
            "ok": False,
            "error": "语音引擎还没配好：转录用的是「语音转录工坊」里的引擎设置，"
                     "去那边把 API Key 填上，这里会自动生效。",
        }), 400

    if lesson["status"] in ("parsing", "transcribing", "analyzing", "rendering"):
        return jsonify({"ok": False, "error": "这堂课还在处理中"}), 409

    store.update(lesson_id, status="parsing", stage="排队中", progress=1, error="")
    store.run(lesson_id)
    return jsonify({"ok": True})


# ---------------- 结果 ----------------

@app.get("/api/lessons/<lesson_id>/preview")
def preview(lesson_id: str):
    lesson = store.get(lesson_id)
    if not lesson:
        return jsonify({"ok": False, "error": "课堂不存在"}), 404

    mode = request.args.get("mode", "extreme")
    key = f"{mode}_md"
    path = (lesson.get("guides") or {}).get(key)
    if not path or not Path(path).exists():
        return jsonify({"ok": False, "error": "该版本还没有生成"}), 404

    content = Path(path).read_text(encoding="utf-8", errors="replace")
    return jsonify({"ok": True, "mode": mode, "content": content})


@app.get("/api/lessons/<lesson_id>/transcript")
def transcript(lesson_id: str):
    """课堂转录全文。复习指导是浓缩后的结果，原文留个入口供核对。"""
    lesson = store.get(lesson_id)
    if not lesson:
        return jsonify({"ok": False, "error": "课堂不存在"}), 404
    return jsonify({"ok": True, "text": lesson.get("transcript_text") or ""})


@app.get("/api/lessons/<lesson_id>/download/<mode>/<fmt>")
def download(lesson_id: str, mode: str, fmt: str):
    lesson = store.get(lesson_id)
    if not lesson:
        return jsonify({"ok": False, "error": "课堂不存在"}), 404

    path = (lesson.get("guides") or {}).get(f"{mode}_{fmt}")
    if not path or not Path(path).exists():
        return jsonify({"ok": False, "error": "该文件尚未生成"}), 404

    suffix = {"md": "text/markdown", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
    label = "极限版" if mode == "extreme" else "详细版"
    return send_file(
        path, as_attachment=True,
        download_name=f"{lesson['name']}_{label}.{fmt}",
        mimetype=suffix.get(fmt, "application/octet-stream"),
    )


@app.get("/api/lessons/<lesson_id>/asset/<path:rel>")
def asset(lesson_id: str, rel: str):
    base = store.outputs_dir(lesson_id).resolve()
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return jsonify({"ok": False, "error": "非法路径"}), 400
    if not target.exists():
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    return send_file(str(target))


@app.errorhandler(413)
def too_large(_):
    return jsonify({"ok": False, "error": "文件超过 2GB 上限"}), 413


# ---------------- 备份与恢复 ----------------

BACKUP_SKIP = ("uploads", "work", "selftest")
RESTORE_LOCK = threading.Lock()


def _stripped_config() -> dict:
    """导出配置时默认去掉密钥，避免备份包流出去把 Key 一起带出去。"""
    cfg = config.load()
    cfg.setdefault("llm", {})["api_key"] = ""
    asr = cfg.get("asr") or {}
    for p in (asr.get("providers") or {}).values():
        if isinstance(p, dict):
            for key in ("api_key", "access_key", "app_id"):
                p.pop(key, None)
    if cfg.get("asr") is not None:
        cfg["asr"] = asr
    return cfg


@app.get("/api/backup")
def backup():
    root = config.DATA_DIR
    include_secrets = request.args.get("secrets") == "1"
    cfg = config.load() if include_secrets else _stripped_config()

    buf = io.BytesIO()
    files = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps({
            "app": "buketang",
            "version": __version__,
            "created": time.time(),
            "lessons": len(store.all()),
            "courses": len(course_store.courses.all()),
            "secrets": include_secrets,
        }, ensure_ascii=False, indent=2))

        zf.writestr("config.json", json.dumps(cfg, ensure_ascii=False, indent=2))

        if root.exists():
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(root)
                if rel.parts and rel.parts[0] in BACKUP_SKIP:
                    continue
                if len(rel.parts) == 1 and rel.name in ("config.json", "manifest.json"):
                    continue
                zf.write(path, str(rel).replace("\\", "/"))
                files += 1

    buf.seek(0)
    stamp = time.strftime("%Y%m%d_%H%M")
    return send_file(buf, as_attachment=True,
                     download_name=f"buketang-backup_{stamp}.zip",
                     mimetype="application/zip")


@app.post("/api/restore")
def restore():
    upload = request.files.get("file")
    if not upload:
        return jsonify({"ok": False, "error": "没有收到备份文件"}), 400
    if not RESTORE_LOCK.acquire(blocking=False):
        return jsonify({"ok": False, "error": "正在恢复，请稍候"}), 409

    before = config.load()
    tmp = Path(tempfile.mkdtemp(prefix="bt_restore_", dir=config.DATA_DIR.parent))
    try:
        if any(l.get("status") in RUNNING_STATUS
               or any(m.get("status") == "pending" for m in l.get("materials", []))
               for l in store.all()) or any(j.get("status") == "running" for j in SUMMARY_JOBS.values()):
            return jsonify({"ok": False, "error": "请先等待当前任务完成或取消，再恢复备份"}), 409
        with zipfile.ZipFile(upload) as zf:
            for info in zf.infolist():
                target = (tmp / info.filename).resolve()
                try:
                    target.relative_to(tmp.resolve())
                except ValueError:
                    return jsonify({"ok": False, "error": "备份包里有非法路径，已拒绝"}), 400
            zf.extractall(tmp)

        try:
            prepare_restore(tmp, config.DATA_DIR)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        stamp = time.strftime("%Y%m%d_%H%M%S")
        saved = config.DATA_DIR.parent / "restore_backups" / f"{stamp}_{uuid.uuid4().hex[:8]}"
        saved.parent.mkdir(parents=True, exist_ok=True)
        moved = False
        if config.DATA_DIR.exists():
            config.DATA_DIR.rename(saved)
            moved = True

        try:
            # 临时目录与数据目录在同一磁盘上，校验完成后用重命名完成替换。
            tmp.rename(config.DATA_DIR)
            _keep_secrets(before)
            store.reload()
            course_store.courses.reload()
        except Exception:
            if moved:
                if config.DATA_DIR.exists():
                    shutil.rmtree(config.DATA_DIR)
                saved.rename(config.DATA_DIR)
                store.reload()
                course_store.courses.reload()
            raise

        SUMMARY_JOBS.clear()

        return jsonify({
            "ok": True,
            "lessons": len(store.all()),
            "courses": len(course_store.courses.all()),
            "saved_to": str(saved) if moved else "",
        })
    except zipfile.BadZipFile:
        return jsonify({"ok": False, "error": "这不是一个有效的 zip 文件"}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"恢复失败：{type(exc).__name__}: {exc}"}), 500
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        RESTORE_LOCK.release()


def _keep_secrets(before: dict) -> None:
    """把恢复前本机已有的密钥补回配置里空缺的位置。"""
    after = config.load()
    llm_before = (before.get("llm") or {}).get("api_key") or ""
    llm_after = (after.get("llm") or {}).get("api_key") or ""
    if llm_before and not llm_after:
        after.setdefault("llm", {})["api_key"] = llm_before

    asr_after = after.get("asr") or {}
    provs_after = asr_after.get("providers") or {}
    provs_before = ((before.get("asr") or {}).get("providers")) or {}
    for pid, old in provs_before.items():
        if not isinstance(old, dict):
            continue
        cur = provs_after.setdefault(pid, {})
        if not isinstance(cur, dict):
            continue
        for key in ("api_key", "access_key", "app_id"):
            if old.get(key) and not cur.get(key):
                cur[key] = old[key]

    if provs_after:
        asr_after["providers"] = provs_after
    if asr_after:
        after["asr"] = asr_after
    config.save(after)


# ---------------- 端侧能力 ----------------

MODEL_JOB: dict = {}


@app.get("/api/local/status")
def local_status():
    """端侧模型总览：装了什么、能不能用、建议用什么。"""
    out: dict = {"asr": {}, "llm": {}}

    try:
        from shared.asr.sensevoice import model_status
        out["asr"] = model_status()
    except Exception as exc:  # noqa: BLE001
        out["asr"] = {"ready": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        import sherpa_onnx  # noqa: F401
        out["asr"]["installed_pkg"] = True
    except ImportError:
        out["asr"]["installed_pkg"] = False

    try:
        import psutil
        ram_gb = psutil.virtual_memory().total / 1024 ** 3
    except Exception:  # noqa: BLE001
        ram_gb = 16.0
    out["ram_gb"] = round(ram_gb, 1)
    out["suggest_model"] = config.recommend_local_model(ram_gb)

    cfg = config.load()
    out["llm"] = config.ollama_status(cfg.get("llm", {}).get("base_url", ""))
    return jsonify(out)


@app.post("/api/local/asr/download")
def download_asr_model():
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
def download_asr_state():
    return jsonify({"job": MODEL_JOB})


@app.get("/api/llm/ollama")
def ollama_models():
    url = request.args.get("base_url") or ""
    if not url:
        # 当前配置本来就是本地端点时才沿用它；否则固定探测 Ollama 默认端口，
        # 否则会拿 DeepSeek 的地址去问 /api/tags，永远返回 401。
        cfg_url = config.load().get("llm", {}).get("base_url", "")
        url = cfg_url if config._is_local_url(cfg_url) else "http://127.0.0.1:11434/v1"
    return jsonify(config.ollama_status(url))


# ---------------- 启动 ----------------

def _free_port(start: int = 8759, tries: int = 20) -> int:
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
    print("  不听课 · 课堂录音转复习指导")
    print(f"  访问地址: {url}")
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
