"""重拍 README 用的截图。

先让服务在跑（建议开演示模式，跑一个几十秒的音频，任务卡才有内容可看）：

    TRANSCRIBER_DATA_DIR=C:/Temp/tr_demo PORT=8764 python app.py --no-browser
    python tests/shots.py http://127.0.0.1:8764/ assets/screenshots

浏览器驱动在 shared/devtools/cdp.py，Chrome / Edge 都行。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from shared.devtools.cdp import CDP  # noqa: E402


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8764/"
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "assets/screenshots")

    c = CDP(url)
    try:
        c.viewport(1440, 980, scale=1.5)
        c.goto(url, 3.0)
        c.watch_errors()

        # 有任务时先拍主界面
        c.js("refreshJobs()")
        time.sleep(1.0)
        c.shot("main", out)

        # 预览
        if c.js("document.querySelectorAll('.job-actions .dl').length"):
            c.js("[...document.querySelectorAll('.job-actions .dl')]"
                 ".find(b=>b.textContent.includes('预览'))?.click()")
            time.sleep(1.8)
            c.shot("preview", out)
            c.js("document.querySelector('#btnCloseModal')?.click()")
            time.sleep(0.4)

        # 设置抽屉
        c.viewport(1440, 1100, scale=1.5)
        c.js("document.querySelector('#btnSettings').click()")
        time.sleep(1.4)
        c.shot("settings", out)

        # 两家代表性引擎的配置块：本地免费 / 云端
        c.js("[...document.querySelectorAll('#engineList .engine-card')]"
             ".find(x=>x.dataset.id==='sensevoice')?.click()")
        time.sleep(0.7)
        c.js("document.querySelector('#drawer .drawer-body').scrollTop=260")
        time.sleep(0.4)
        c.shot("settings-sensevoice", out)

        c.js("[...document.querySelectorAll('#engineList .engine-card')]"
             ".find(x=>x.dataset.id==='openai')?.click()")
        time.sleep(0.7)
        c.js("document.querySelector('#drawer .drawer-body').scrollTop=340")
        time.sleep(0.4)
        c.shot("settings-openai", out)

        c.js("document.querySelector('#btnCloseDrawer').click()")
        time.sleep(0.4)

        # 深色模式
        c.viewport(1440, 980, scale=1.5)
        c.js("SuiteTheme.set('dark')")
        time.sleep(0.5)
        c.shot("dark", out)
        c.js("SuiteTheme.set('auto')")
        time.sleep(0.3)

        # 窄屏
        c.viewport(820, 1000, mobile=True, scale=1.5)
        time.sleep(0.6)
        c.shot("narrow", out)

        print("页面错误：", c.js("(window.__errs||[]).join(' | ')") or "无")
    finally:
        c.close()


if __name__ == "__main__":
    main()
