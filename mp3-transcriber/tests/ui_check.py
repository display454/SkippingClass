"""交互验收：用真实浏览器把「语音转录工坊」的主路径点一遍。

默认自给自足 —— 起一个临时服务、用演示模式跑一个几十秒的假音频、跑完清干净，
不消耗任何接口额度，也不碰你 `data/` 里的真实任务：

    python tests/ui_check.py
    python tests/ui_check.py --keep        # 失败时保留临时目录方便翻查
    python tests/ui_check.py --url http://127.0.0.1:8764/   # 测已在跑的服务
"""
from __future__ import annotations

import argparse
import math
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))

from shared.devtools.cdp import CDP  # noqa: E402

PASS, FAIL = [], []


def check(name: str, got, want) -> None:
    ok = got == want if not callable(want) else bool(want(got))
    if ok:
        PASS.append(name)
        print(f"  ok   {name}")
    else:
        FAIL.append(f"{name}: 期望 {want!r}，实际 {got!r}")
        print(f"  FAIL {name}: 期望 {want!r}，实际 {got!r}")


def make_wav(path: Path, seconds: float = 3.0) -> Path:
    """造一段能过 ffprobe 的正弦波，避免仓库里塞二进制音频。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 16000
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(rate * seconds)):
            v = int(3000 * math.sin(2 * math.pi * 440 * i / rate))
            frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))
    return path


def free_port(start: int = 8764) -> int:
    for offset in range(40):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def api(base: str, path: str, method: str = "GET", payload=None):
    import json
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                headers={"Content-Type": "application/json", "X-CSRF-Token": __import__("json").load(urllib.request.urlopen(base + "/api/security"))["token"]})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode("utf-8", "replace") or "{}")


def upload(base: str, path: Path) -> dict:
    import requests
    token = requests.get(base + "/api/security").json()["token"]
    with path.open("rb") as fh:
        r = requests.post(f"{base}/api/upload", files={"files": (path.name, fh, "audio/wav")}, headers={"X-CSRF-Token": token},
                          timeout=120)
    return r.json()


def wait_health(base: str, timeout: float = 25.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{base}/api/health", timeout=1.5) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            time.sleep(0.35)
    return False


def boot(data_dir: Path, log_path: Path) -> tuple[subprocess.Popen, str]:
    """起服务，日志写文件。

    不能用 subprocess.PIPE：Werkzeug 每个请求打一行日志，管道缓冲区填满后
    服务会卡死在 write 上，表现成「应用没响应」，其实是测试没读管子。
    """
    port = free_port()
    env = dict(os.environ)
    env["TRANSCRIBER_DATA_DIR"] = str(data_dir)
    env["PORT"] = str(port)
    env["PYTHONIOENCODING"] = "utf-8"
    log = log_path.open("wb")
    proc = subprocess.Popen(
        [sys.executable, "app.py", "--no-browser"],
        cwd=str(HERE.parent), env=env,
        stdout=log, stderr=subprocess.STDOUT,
    )
    log.close()
    base = f"http://127.0.0.1:{port}"
    if not wait_health(base):
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError("服务没起来：\n"
                           + log_path.read_text(encoding="utf-8", errors="replace")[-1500:])
    return proc, base


def main() -> None:
    ap = argparse.ArgumentParser(description="「语音转录工坊」交互验收")
    ap.add_argument("--url", default="", help="指向已运行的服务；不填则自动起一个")
    ap.add_argument("--keep", action="store_true", help="保留临时数据目录")
    args = ap.parse_args()

    proc, tmp_root, base, log_path = None, None, args.url, None
    if not base:
        tmp_root = Path(tempfile.mkdtemp(prefix="tr_ui_"))
        data_dir = tmp_root / "data"
        data_dir.mkdir(parents=True)
        log_path = tmp_root / "server.log"
        proc, base = boot(data_dir, log_path)
        print(f"\n临时服务: {base}\n数据目录: {data_dir}")

    audio = make_wav((tmp_root or Path(tempfile.mkdtemp(prefix="tr_ui_"))) / "demo.wav")
    c = CDP(base)
    try:
        c.viewport(1440, 1000)
        c.goto(base, 3.0)
        c.watch_errors()

        print("\n[首屏]")
        check("空状态可见", c.js("document.querySelector('#emptyState').classList.contains('show')"), True)
        check("任务计数为 0", c.js("document.querySelector('#taskCount').textContent"), "0")
        check("拖拽区在", c.js("!!document.querySelector('#dropzone')"), True)
        check("引擎标签有文字", c.js(
            "(document.querySelector('#chipEngine').textContent||'').trim().length > 0"), True)

        print("\n[未配置凭证时的引导]")
        api(base, "/api/config", "POST", {"mock": False})
        c.js("loadConfig()")
        time.sleep(1.6)
        check("提示条出现而不是弹窗抢焦点", c.js(
            "!document.querySelector('#setupNote').classList.contains('hidden')"), True)
        check("状态标签说未配置", c.js("document.querySelector('#chipKey').textContent"), "未配置凭证")
        check("没有自动弹出设置抽屉", c.js("document.querySelector('#drawer').classList.contains('open')"), False)

        print("\n[打开演示模式]")
        api(base, "/api/config", "POST", {"mock": True})
        c.js("loadConfig()")
        time.sleep(1.6)
        check("标签变为演示模式", c.js("document.querySelector('#chipKey').textContent"), "演示模式")
        check("提示条收起", c.js("document.querySelector('#setupNote').classList.contains('hidden')"), True)

        print("\n[上传 → 转写 → 完成]")
        res = upload(base, audio)
        check("上传被接受", (res or {}).get("ok"), True)
        check("没有文件被拒", (res or {}).get("rejected"), [])
        c.js("refreshJobs()")
        time.sleep(1.0)
        check("任务卡出现", c.js("document.querySelectorAll('.job').length"), 1)
        check("计数变 1", c.js("document.querySelector('#taskCount').textContent"), "1")
        check("空状态收起", c.js("document.querySelector('#emptyState').classList.contains('show')"), False)
        check("文件名正确", c.js("document.querySelector('.job .job-name').textContent"), "demo.wav")

        done = False
        for _ in range(60):
            if c.js("document.querySelector('.job .badge').textContent") == "已完成":
                done = True
                break
            c.js("refreshJobs()")
            time.sleep(1.0)
        check("转写完成", done, True)

        print("\n[结果操作]")
        check("每个输出格式一个下载按钮", c.js(
            "document.querySelectorAll('.job-actions .dl').length"), lambda n: n is not None and n >= 3)
        check("下载按钮文字含 Markdown", c.js(
            "[...document.querySelectorAll('.job-actions .dl')].some(b=>b.textContent.includes('Markdown'))"), True)
        check("有压缩包下载", c.js(
            "[...document.querySelectorAll('.job-actions .dl')].some(b=>b.textContent.includes('压缩包'))"), True)

        print("\n[预览]")
        c.js("[...document.querySelectorAll('.job-actions .dl')]"
             ".find(b=>b.textContent.includes('预览')).click()")
        time.sleep(1.8)
        check("弹层打开", c.js("document.querySelector('#modal').classList.contains('open')"), True)
        check("正文有渲染内容", c.js("document.querySelector('#modalBody').textContent.trim().length"),
              lambda n: n is not None and n > 20)
        check("两个视图标签", c.js("document.querySelectorAll('#modalTabs button').length"), 2)
        c.js("[...document.querySelectorAll('#modalTabs button')].find(b=>b.dataset.tab==='raw').click()")
        time.sleep(0.5)
        check("原文视图是 pre", c.js("!!document.querySelector('#modalBody pre')"), True)

        print("\n[复制 Markdown]")
        c.js("Object.defineProperty(navigator,'clipboard',{configurable:true,"
             "value:{writeText:()=>Promise.reject(new Error('denied'))}})")
        c.js("document.querySelector('#btnCopyMd').click()")
        time.sleep(0.7)
        check("剪贴板被拒时弹手动复制", c.js("!!document.querySelector('.copy-area')"), True)
        check("复制内容非空", c.js("(document.querySelector('.copy-area')||{}).value?.length"),
              lambda n: n is not None and n > 20)
        c.js("document.querySelector('.copy-area')?.closest('.modal')?.remove()")
        c.js("document.querySelector('#btnCloseModal')?.click()")
        time.sleep(0.5)
        check("弹层可关闭", c.js("document.querySelector('#modal').classList.contains('open')"), False)

        print("\n[设置抽屉 · 引擎]")
        c.js("document.querySelector('#btnSettings').click()")
        time.sleep(1.2)
        check("抽屉打开", c.js("document.querySelector('#drawer').classList.contains('open')"), True)
        check("引擎卡片数 ≥ 5", c.js("document.querySelectorAll('#engineList .engine-card').length"),
              lambda n: n is not None and n >= 5)
        c.js("[...document.querySelectorAll('#engineList .engine-card')]"
             ".find(c=>c.dataset.id==='sensevoice').click()")
        time.sleep(0.6)
        check("选中后高亮", c.js(
            "document.querySelector('#engineList .engine-card[data-id=\"sensevoice\"]').classList.contains('active')"), True)
        check("只显示对应的配置块", c.js(
            "[...document.querySelectorAll('.engine-config')].filter(b=>!b.classList.contains('hidden')).length"), 1)
        c.js("[...document.querySelectorAll('#engineList .engine-card')]"
             ".find(c=>c.dataset.id==='volc').click()")
        time.sleep(0.5)
        check("火山配置块显示", c.js(
            "!document.querySelector('.engine-config[data-engine=\"volc\"]').classList.contains('hidden')"), True)
        check("有鉴权方式切换", c.js("!!document.querySelector('#authMode')"), True)
        c.js("document.querySelector('#btnCloseDrawer').click()")
        time.sleep(0.5)

        print("\n[暗色模式]")
        c.js("try{localStorage.removeItem('suite.theme')}catch(e){};SuiteTheme.set('auto')")
        check("初始跟随系统", c.js("document.documentElement.dataset.theme || 'auto'"), "auto")
        c.js("document.querySelector('[data-theme-toggle]').click()")
        time.sleep(0.3)
        check("第一次点 → 浅色", c.js("document.documentElement.dataset.theme"), "light")
        c.js("document.querySelector('[data-theme-toggle]').click()")
        time.sleep(0.3)
        check("第二次点 → 深色", c.js("document.documentElement.dataset.theme"), "dark")
        check("深色下纸面 token 变深", c.js(
            "getComputedStyle(document.documentElement).getPropertyValue('--paper').trim()"), "#17150f")
        check("深色下正文是浅色字（可读）", c.js(
            "(()=>{const m=getComputedStyle(document.body).color.match(/\\d+/g);"
            "return m? (+m[0]+ +m[1]+ +m[2])/3 : null})()"),
            lambda n: n is not None and n > 120)
        c.js("document.querySelector('[data-theme-toggle]').click()")
        time.sleep(0.3)
        check("第三次点 → 回到自动", c.js("document.documentElement.dataset.theme || 'auto'"), "auto")

        print("\n[删除保护]")
        c.js("document.querySelector('.job-actions .icon-btn')?.click()")
        time.sleep(0.6)
        check("删除前先确认", c.js(
            "!!document.querySelector('.modal.open [data-act=\"yes\"]')"), True)
        check("说明里提到不能恢复", c.js(
            "document.body.innerText.includes('不能恢复')"), True)
        c.js("document.querySelector('.modal.open [data-act=\"no\"]').click()")
        time.sleep(0.6)
        check("取消后任务还在", c.js("document.querySelectorAll('.job').length"), 1)

        print("\n[清理任务]")
        c.js("document.querySelector('#btnClearDone').click()")
        time.sleep(0.6)
        check("批量清除也要确认", c.js(
            "!!document.querySelector('.modal.open [data-act=\"yes\"]')"), True)
        c.js("document.querySelector('.modal.open [data-act=\"yes\"]').click()")
        time.sleep(1.8)
        check("任务被删掉", c.js("document.querySelectorAll('.job').length"), 0)
        check("空状态回来", c.js("document.querySelector('#emptyState').classList.contains('show')"), True)

        print("\n[窄屏]")
        c.viewport(420, 880, mobile=True)
        time.sleep(0.8)
        check("窄屏不横向溢出", c.js(
            "document.documentElement.scrollWidth <= window.innerWidth + 2"), True)

        print("\n[运行时错误]")
        check("无 JS 报错", c.js("(window.__errs||[]).slice(0,5)"), [])

    finally:
        c.close()
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                proc.kill()
        if FAIL and log_path is not None and log_path.exists():
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            print("\n服务端日志（末尾 2KB）：\n" + tail)
        if tmp_root is not None:
            if args.keep:
                print(f"\n临时目录保留在：{tmp_root}")
            else:
                shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  -", f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
