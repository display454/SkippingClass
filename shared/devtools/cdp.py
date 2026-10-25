"""CDP 驱动无头浏览器：界面验收与截图用（只在开发时跑，不进入运行时）。

为什么不用 Selenium / Playwright：这两个应用是「双击就能用」的本地工具，
测试依赖越少越好。这里只用一个 `websocket-client`，其余全走 CDP 原生协议，
Chrome / Edge / Chromium 都能驱动。

浏览器查找顺序：
    1. 环境变量 CHROME_PATH
    2. 各系统常见安装位置（Chrome → Edge → Chromium）
    3. PATH 里的 chrome / google-chrome / msedge

用法见各应用 tests/ui_check.py。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

_WIN = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
_MAC = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]
_LINUX = ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"]
_WHICH = ["chrome", "google-chrome", "chromium", "msedge", "microsoft-edge"]


def find_browser() -> str:
    env = os.environ.get("CHROME_PATH")
    if env and Path(env).exists():
        return env
    pool = _WIN if sys.platform == "win32" else (_MAC if sys.platform == "darwin" else _LINUX)
    for p in pool:
        if Path(p).exists():
            return p
    for name in _WHICH:
        hit = shutil.which(name)
        if hit:
            return hit
    raise RuntimeError(
        "没找到 Chrome / Edge，界面验收需要一个 Chromium 内核的浏览器。\n"
        "装了别的版本可以设环境变量 CHROME_PATH 指向可执行文件。"
    )


def free_port(start: int = 9333, tries: int = 60) -> int:
    for offset in range(tries):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


class CDP:
    """一个最小可用的 DevTools 客户端。

    每个实例用独立的用户目录和端口，两个应用同时跑不会打架；
    close() 时一起清掉，不在系统临时目录里留垃圾。
    """

    def __init__(self, url: str = "about:blank", port: int | None = None, size=(1440, 980)):
        self.browser = find_browser()
        self.port = port or free_port()
        self.profile = Path(tempfile.mkdtemp(prefix="cdp_profile_"))
        self.proc = self._launch(size)
        self.ws = None
        self._id = 0
        self._connect()

    def _launch(self, size) -> subprocess.Popen:
        base = [
            self.browser,
            "--disable-gpu", "--hide-scrollbars", "--no-first-run",
            "--no-default-browser-check", "--disable-extensions",
            "--remote-allow-origins=*",
            f"--remote-debugging-port={self.port}",
            f"--window-size={size[0]},{size[1]}",
            f"--user-data-dir={self.profile}",
        ]
        for headless in ("--headless=new", "--headless"):
            proc = subprocess.Popen(
                base + [headless, "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            for _ in range(50):
                if proc.poll() is not None:
                    break
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=1)
                    return proc
                except Exception:  # noqa: BLE001
                    time.sleep(0.25)
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    proc.kill()
        raise RuntimeError(f"浏览器没起来：{self.browser}")

    def _connect(self) -> None:
        page = None
        for _ in range(40):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=2) as r:
                    data = json.load(r)
                page = next((t for t in data if t["type"] == "page"), None)
                if page:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.3)
        if not page:
            raise RuntimeError("没拿到可调试的页面")

        try:
            from websocket import create_connection
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("缺少 websocket-client，请先 pip install websocket-client") from exc

        self.ws = create_connection(page["webSocketDebuggerUrl"], timeout=60)

    # ---------- 协议封装 ----------

    def cmd(self, method: str, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def viewport(self, width: int, height: int, mobile: bool = False, scale: float = 1):
        self.cmd("Emulation.setDeviceMetricsOverride", width=width, height=height,
                 deviceScaleFactor=scale, mobile=mobile)

    def goto(self, url: str, wait: float = 2.6) -> None:
        self.cmd("Page.enable")
        self.cmd("Page.navigate", url=url)
        time.sleep(wait)

    def js(self, expr: str):
        r = self.cmd("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if r.get("exceptionDetails"):
            det = r["exceptionDetails"]
            desc = (det.get("exception") or {}).get("description") or det.get("text")
            raise RuntimeError(f"JS 执行出错：{desc}")
        return r.get("result", {}).get("value")

    def watch_errors(self) -> None:
        """挂一个收集器，最后取 window.__errs 看有没有报错。"""
        self.js(
            "window.__errs=[];"
            "window.addEventListener('error',e=>{try{__errs.push(e.message)}catch(x){}});"
            "window.addEventListener('unhandledrejection',"
            "e=>{try{__errs.push('promise:'+e.reason)}catch(x){}});"
        )

    def shot(self, name: str, out: Path, clip: dict | None = None, full: bool = False) -> Path:
        """截一张 PNG。

        默认只截当前视口——README 里的图不会变成几万像素的长条，
        滚动到哪就拍哪，一张图说明一件事。full=True 才截整页。
        """
        out.mkdir(parents=True, exist_ok=True)
        params: dict = {"format": "png", "captureBeyondViewport": full}
        if clip:
            params["clip"] = dict(clip, scale=1)
        r = self.cmd("Page.captureScreenshot", **params)
        p = out / f"{name}.png"
        p.write_bytes(base64.b64decode(r["data"]))
        return p

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=6)
        except Exception:  # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(self.profile, ignore_errors=True)
