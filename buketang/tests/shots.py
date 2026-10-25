"""重拍 README 用的截图。

服务已经在跑（默认 8763），数据用 tests/seed.py 生成的最像正式使用：

    python tests/seed.py --dir C:/Temp/bt_demo
    BUKETANG_DATA_DIR=C:/Temp/bt_demo PORT=8763 python app.py --no-browser
    python tests/shots.py http://127.0.0.1:8763/ assets/screenshots

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
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8763/"
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "assets/screenshots")

    c = CDP(url)
    try:
        c.viewport(1440, 980, scale=1.5)
        c.goto(url, 3.4)
        c.watch_errors()
        print("默认视图：", c.js("state.view"), "| 课堂:", c.js("state.lesson && state.lesson.name"))
        c.shot("main", out)

        # 详细版 + 正文内的讲义截图
        c.js("[...document.querySelectorAll('#modeTabs button')].find(b=>b.dataset.mode==='detailed').click()")
        time.sleep(1.8)
        c.js("document.querySelector('#resultBody img')?.scrollIntoView({block:'center'})")
        time.sleep(0.7)
        c.shot("detailed", out)

        # 转录全文展开
        c.js("document.querySelector('.main').scrollTop=0")
        c.js("document.querySelector('#btnTranscript')?.click()")
        time.sleep(1.4)
        c.js("document.querySelector('#transcript')?.scrollIntoView({block:'start'})")
        time.sleep(0.6)
        c.shot("transcript", out)

        # 课程视图 + 期末总复习
        c.js("openCourse('c0001')")
        time.sleep(2.2)
        c.shot("course", out)
        c.js("document.querySelector('#sumResult')?.scrollIntoView({block:'start'})")
        time.sleep(0.8)
        c.shot("summary", out)

        # 设置抽屉
        c.viewport(1440, 1100, scale=1.5)
        c.js("document.querySelector('.main').scrollTop=0")
        c.js("openDrawer()")
        time.sleep(1.6)
        c.shot("settings", out)
        c.js("closeDrawer()")

        # 深色模式
        c.viewport(1440, 980, scale=1.5)
        c.js("SuiteTheme.set('dark')")
        time.sleep(0.6)
        c.js("openLesson('l0002')")
        time.sleep(1.8)
        c.shot("dark", out)
        c.js("SuiteTheme.set('auto')")
        time.sleep(0.4)

        # 窄屏（笔记本半屏 / 平板竖屏）
        c.viewport(820, 1000, mobile=True, scale=1.5)
        time.sleep(0.8)
        c.shot("narrow", out)

        print("页面错误：", c.js("(window.__errs||[]).join(' | ')") or "无")
    finally:
        c.close()


if __name__ == "__main__":
    main()
