"""交互验收：把「不听课」的关键操作在真实浏览器里逐项点一遍。

默认自给自足 —— 生成一套演示数据、起一个临时服务、跑完断言再收拾干净，
全程不碰你自己 `data/` 里的课堂：

    python tests/ui_check.py

也可以指向已经在跑的服务（数据需与 tests/seed.py 生成的保持一致）：

    python tests/ui_check.py --url http://127.0.0.1:8799/

加了 --keep 会保留临时目录，方便失败后进去翻数据。
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))

from shared.devtools.cdp import CDP  # noqa: E402
import seed as seeder  # noqa: E402

PASS, FAIL = [], []


def check(name: str, got, want) -> None:
    ok = got == want if not callable(want) else bool(want(got))
    if ok:
        PASS.append(name)
        print(f"  ok   {name}")
    else:
        FAIL.append(f"{name}: 期望 {want!r}，实际 {got!r}")
        print(f"  FAIL {name}: 期望 {want!r}，实际 {got!r}")


def free_port(start: int = 8763) -> int:
    for offset in range(40):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


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
    """按用户真实使用的方式起服务：同一份 app.py、同样的环境变量。

    服务端日志写文件，不能用 PIPE——Werkzeug 每个请求都打一行日志，
    管道缓冲区（几十 KB）写满又没人读，服务就会卡死在 write 上，
    看起来像「应用挂了」，其实是测试自己把管子堵了。
    """
    port = free_port()
    env = dict(os.environ)
    env["BUKETANG_DATA_DIR"] = str(data_dir)
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
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
        raise RuntimeError("服务没起来：\n" + tail)
    return proc, base


def main() -> None:
    ap = argparse.ArgumentParser(description="「不听课」交互验收")
    ap.add_argument("--url", default="", help="指向已运行的服务；不填则自动起一个")
    ap.add_argument("--keep", action="store_true", help="保留临时数据目录")
    args = ap.parse_args()

    proc, tmp_root, base = None, None, args.url
    log_path = None
    if not base:
        tmp_root = Path(tempfile.mkdtemp(prefix="bt_ui_"))
        data_dir = tmp_root / "data"
        log_path = tmp_root / "server.log"
        seeder.seed(data_dir)
        proc, base = boot(data_dir, log_path)
        print(f"\n临时服务: {base}\n数据目录: {data_dir}")

    c = CDP(base)
    try:
        c.viewport(1440, 1000)
        c.goto(base, 3.4)
        c.watch_errors()
        c.js("try{localStorage.removeItem('suite.theme')}catch(e){};SuiteTheme.set('auto')")

        print("\n[侧栏树]")
        check("课程节点数（2 门课 + 未分类）", c.js("document.querySelectorAll('.course-node').length"), 3)
        check("课堂条目数", c.js("document.querySelectorAll('.lesson-item').length"), 4)
        check("操作系统有 3 堂课", c.js(
            "document.querySelector('.course-node[data-cid=\"c0001\"] .node-count').textContent"), "3")
        check("数据结构 0 堂课", c.js(
            "document.querySelector('.course-node[data-cid=\"c0002\"] .node-count').textContent"), "0")
        check("未分类里有 1 堂课", c.js(
            "document.querySelector('.course-node[data-cid=\"\"] .node-count').textContent"), "1")

        print("\n[页面结构]")
        check("三个视图都挂在 .main 里", c.js(
            "['welcome','detail','courseView'].every(id=>"
            "document.querySelector('.main #'+id))"), True)
        check("顶层没有漏在 body 下的视图", c.js(
            "[...document.body.children].filter(e=>['welcome','detail','courseView']"
            ".includes(e.id)).length"), 0)

        print("\n[搜索]（含考点文本）")
        def search(q: str) -> None:
            c.js("(()=>{const s=document.querySelector('#search');s.value=" + repr(q) +
                 ";s.dispatchEvent(new Event('input'));})()")
            time.sleep(0.45)
        search("调度")
        check("搜「调度」命中 1 条", c.js("document.querySelectorAll('.lesson-item').length"), 1)
        search("死锁")
        check("搜「死锁」命中 2 条（考点文本也算）",
              c.js("document.querySelectorAll('.lesson-item').length"), 2)
        search("链表")
        check("搜「链表」命中 1 条", c.js("document.querySelectorAll('.lesson-item').length"), 1)
        search("")
        check("清空后恢复 4 条", c.js("document.querySelectorAll('.lesson-item').length"), 4)

        print("\n[折叠]")
        c.js("document.querySelector('.course-node .caret').click()")
        time.sleep(0.35)
        check("折叠后隐藏课堂", c.js(
            "getComputedStyle(document.querySelector('.node-lessons')).display"), "none")
        c.js("document.querySelector('.course-node .caret').click()")
        time.sleep(0.35)
        check("展开后课堂回来", c.js("document.querySelectorAll('.lesson-item').length"), 4)

        print("\n[课程视图 · 有内容]")
        c.js("document.querySelector('.course-node[data-cid=\"c0001\"] .node-course').click()")
        time.sleep(1.8)
        check("进入课程视图", c.js("state.view"), "course")
        # 视图容器必须真的挂在 .main 里并且可见：标签少闭合一个 div，
        # 整个课程页就会跑到 body 下面、被 hidden 卡住，而选择器照样能查到元素
        check("课程视图挂在主区里", c.js(
            "!!document.querySelector('.main #courseView')"), True)
        check("课程视图可见", c.js(
            "(()=>{const el=document.querySelector('#courseView');"
            "return !!el && !el.classList.contains('hidden') && el.getBoundingClientRect().height > 200})()"), True)
        check("课堂行数", c.js("document.querySelectorAll('.clist-row').length"), 3)
        check("头部信息含课堂数", c.js("document.querySelector('#courseMeta').textContent.includes('3 堂课')"), True)
        check("总复习区已显示", c.js("!document.querySelector('#sumResult').classList.contains('hidden')"), True)
        check("总复习渲染成表格", c.js("!!document.querySelector('#sumBody table')"), True)
        check("总复习表格有内容", c.js("document.querySelectorAll('#sumBody tbody tr').length"), lambda n: n >= 8)
        check("提供 md / docx 下载", c.js("document.querySelectorAll('#sumDl a').length"), 2)
        check("按钮文案变「重新汇总」", c.js("document.querySelector('#btnSummary span').textContent"), "重新汇总")
        check("总复习按钮可用", c.js("document.querySelector('#btnSummary').disabled"), False)

        print("\n[课程视图 · 空课程]")
        c.js("document.querySelector('.course-node[data-cid=\"c0002\"] .node-course').click()")
        time.sleep(1.2)
        check("空课程给提示而不是空白", c.js("document.querySelector('#courseLessons .empty-tip')?.textContent"), "这门课下还没有课堂")
        check("空课程不显示总复习区", c.js("document.querySelector('#sumResult').classList.contains('hidden')"), True)
        check("空课程汇总按钮禁用", c.js("document.querySelector('#btnSummary').disabled"), True)

        print("\n[课堂视图]")
        c.js("openLesson('l0001')")
        time.sleep(1.6)
        check("打开第 1 讲", c.js("state.lesson.name"), "第 1 讲 · 进程与线程")
        check("课堂视图可见", c.js(
            "(()=>{const el=document.querySelector('#detail');"
            "return !!el && !el.classList.contains('hidden') && el.getBoundingClientRect().height > 200})()"), True)
        check("复习指导已渲染", c.js("!!document.querySelector('#resultBody h3, #resultBody table')"), True)
        check("两种模式标签都在", c.js("document.querySelectorAll('#modeTabs button').length"), 2)

        print("\n[转录全文]")
        check("转录入口可见", c.js("!document.querySelector('#transcript').classList.contains('hidden')"), True)
        check("默认收起", c.js("document.querySelector('#transcriptBody').classList.contains('hidden')"), True)
        c.js("document.querySelector('#btnTranscript').click()")
        time.sleep(1.4)
        check("展开后可见", c.js("!document.querySelector('#transcriptBody').classList.contains('hidden')"), True)
        check("按行拆分", c.js("document.querySelectorAll('#transcriptBody .t-line').length"), 7)
        check("时间戳单独成列", c.js("document.querySelector('#transcriptBody .t-ts')?.textContent"), "00:00:00")
        check("正文首句正确", c.js(
            "document.querySelector('#transcriptBody .t-txt')?.textContent.includes('进程')"), True)
        check("按钮 aria 已展开", c.js("document.querySelector('#btnTranscript').getAttribute('aria-expanded')"), "true")
        c.js("document.querySelector('#btnTranscript').click()")
        time.sleep(0.4)
        check("再点可收起", c.js("document.querySelector('#transcriptBody').classList.contains('hidden')"), True)

        print("\n[录音试听]")
        check("有试听按钮", c.js("document.querySelectorAll('[data-play]').length"), 1)
        c.js("document.querySelector('[data-play]').click()")
        time.sleep(1.6)
        check("播放器展开", c.js("document.querySelectorAll('.audio-slot audio').length"), 1)
        check("音频可加载（Range 也走这条）", c.js(
            "document.querySelector('.audio-slot audio')?.readyState"), lambda n: n is not None and n >= 1)
        check("高亮当前条目", c.js("document.querySelector('[data-play]').classList.contains('on')"), True)
        c.js("document.querySelector('[data-play]').click()")
        time.sleep(0.4)
        check("再点收起播放器", c.js("document.querySelectorAll('.audio-slot').length"), 0)

        print("\n[一键复制]")
        c.js("Object.defineProperty(navigator,'clipboard',{configurable:true,"
             "value:{writeText:()=>Promise.reject(new Error('denied'))}})")
        c.js("document.querySelector('#btnCopy').click()")
        time.sleep(0.8)
        check("剪贴板被拒时弹手动复制", c.js("!!document.querySelector('.modal.open .copy-area')"), True)
        check("复制内容非空", c.js("(document.querySelector('.modal.open .copy-area')||{}).value?.length"),
              lambda n: n is not None and n > 200)
        check("内容确实是这份指导", c.js(
            "(document.querySelector('.modal.open .copy-area')||{}).value?.includes('进程')"), True)
        c.js("document.querySelector('.modal.open [data-act=\"close\"]').click()")
        time.sleep(0.3)
        check("可以关掉弹层", c.js("document.querySelectorAll('.modal.open').length"), 0)

        print("\n[重命名]")
        c.js("document.querySelector('#btnRename').click()")
        time.sleep(0.4)
        check("进入编辑态", c.js("document.querySelector('#lessonName').contentEditable"), "true")
        c.js("(()=>{const el=document.querySelector('#lessonName');"
             "el.textContent='第 1 讲 · 进程与线程（改）';"
             "el.dispatchEvent(new Event('blur'));})()")
        time.sleep(1.8)
        check("重命名生效", c.js("state.lesson.name"), "第 1 讲 · 进程与线程（改）")
        c.js("fetch('/api/lessons/l0001',{method:'PATCH',headers:{'Content-Type':'application/json'},"
             "body:JSON.stringify({name:'第 1 讲 · 进程与线程'})})")
        time.sleep(0.9)

        print("\n[删除确认]")
        c.js("document.querySelector('[data-del=\"l0004\"]').click()")
        time.sleep(0.6)
        check("弹出确认框", c.js("!!document.querySelector('.modal.open [data-act=\"yes\"]')"), True)
        check("确认框说清了不可恢复", c.js("document.body.innerText.includes('无法恢复')"), True)
        c.js("document.querySelector('.modal.open [data-act=\"no\"]').click()")
        time.sleep(0.5)
        check("取消后课堂还在", c.js("document.querySelectorAll('.lesson-item').length"), 4)

        print("\n[设置 · 语音引擎]")
        c.js("openDrawer()")
        time.sleep(1.6)
        check("抽屉打开", c.js("document.querySelector('#drawer').classList.contains('open')"), True)
        check("大模型预设数（含本地 Ollama）",
              c.js("document.querySelectorAll('#presetList button').length"), 6)
        c.js("[...document.querySelectorAll('#presetList button')].find(b=>b.dataset.preset==='ollama').click()")
        time.sleep(1.2)
        check("切到本地后显示 Ollama 卡片",
              c.js("!document.querySelector('#ollamaCard').classList.contains('hidden')"), True)
        check("切到本地后隐藏 API Key",
              c.js("document.querySelector('#keyField').classList.contains('hidden')"), True)

        c.js("(()=>{const s=document.querySelector('#asrProvider');s.value='sensevoice';"
             "s.dispatchEvent(new Event('change'));})()")
        time.sleep(0.5)
        check("本地引擎显示模型卡片", c.js("!document.querySelector('#svCard').classList.contains('hidden')"), True)
        check("本地引擎不要凭证", c.js("document.querySelector('#asrCreds').classList.contains('hidden')"), True)

        c.js("(()=>{const s=document.querySelector('#asrProvider');s.value='openai';"
             "s.dispatchEvent(new Event('change'));})()")
        time.sleep(0.5)
        check("云端引擎要凭证", c.js("!document.querySelector('#asrCreds').classList.contains('hidden')"), True)
        check("云端引擎显示地址/模型输入", c.js("!document.querySelector('#asrBaseField').classList.contains('hidden')"), True)
        check("地址带默认值", c.js("document.querySelector('#asrBaseUrl').value"), "https://api.openai.com/v1")
        check("模型带默认值", c.js("document.querySelector('#asrModel').value"), "whisper-1")

        c.js("(()=>{const s=document.querySelector('#asrProvider');s.value='volc';"
             "s.dispatchEvent(new Event('change'));})()")
        time.sleep(0.5)
        check("火山只需填 Key", c.js("document.querySelector('#asrBaseField').classList.contains('hidden')"), True)
        check("火山仍显示凭证框", c.js("!document.querySelector('#asrCreds').classList.contains('hidden')"), True)

        print("\n[设置 · 数据与关于]")
        check("有备份按钮", c.js("!!document.querySelector('#btnBackup')"), True)
        check("有恢复按钮", c.js("!!document.querySelector('#btnRestore')"), True)
        check("有恢复文件选择框", c.js("!!document.querySelector('#restoreInput')"), True)
        check("关于里显示版本号", c.js(
            "(document.querySelector('#aboutVer')||{}).textContent?.match(/\\d+\\.\\d+\\.\\d+/)"),
            lambda m: bool(m))
        c.js("closeDrawer()")
        time.sleep(0.5)

        print("\n[备份 → 恢复 真实往返]")
        size = c.js("(async()=>{const r=await fetch('/api/backup');const b=await r.blob();"
                    "window.__bk=b;return b.size})()")
        check("备份包非空", size, lambda n: n is not None and n > 20000)
        res = c.js("(async()=>{const fd=new FormData();"
                   "fd.append('file',new Blob([window.__bk],{type:'application/zip'}),'bk.zip');"
                   "const r=await fetch('/api/restore',{method:'POST',body:fd});"
                   "return await r.json()})()")
        check("恢复返回成功", (res or {}).get("ok"), True)
        check("恢复后课堂数不变", (res or {}).get("lessons"), 4)
        check("恢复后课程数不变", (res or {}).get("courses"), 2)
        c.js("loadAll()")
        time.sleep(1.4)
        check("恢复后侧栏重新加载", c.js("document.querySelectorAll('.lesson-item').length"), 4)

        print("\n[暗色模式]")
        check("初始跟随系统", c.js("document.documentElement.dataset.theme || 'auto'"), "auto")
        c.js("document.querySelector('[data-theme-toggle]').click()")
        time.sleep(0.35)
        check("第一次点 → 浅色", c.js("document.documentElement.dataset.theme"), "light")
        c.js("document.querySelector('[data-theme-toggle]').click()")
        time.sleep(0.35)
        check("第二次点 → 深色", c.js("document.documentElement.dataset.theme"), "dark")
        check("深色下纸面 token 变深", c.js(
            "getComputedStyle(document.documentElement).getPropertyValue('--paper').trim()"), "#17150f")
        check("深色下正文是浅色字（可读）", c.js(
            "(()=>{const m=getComputedStyle(document.body).color.match(/\\d+/g);"
            "return m? (+m[0]+ +m[1]+ +m[2])/3 : null})()"),
            lambda n: n is not None and n > 120)
        c.js("document.querySelector('[data-theme-toggle]').click()")
        time.sleep(0.35)
        check("第三次点 → 回到自动", c.js("document.documentElement.dataset.theme || 'auto'"), "auto")

        print("\n[快捷键]")
        c.js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'k',metaKey:true,bubbles:true}))")
        time.sleep(0.35)
        check("Ctrl+K 聚焦搜索", c.js("document.activeElement.id"), "search")
        c.js("document.activeElement.blur()")

        print("\n[归到课程]")
        c.js("(()=>{const l=state.lessons.find(x=>!x.course_id);openLesson(l.id);})()")
        time.sleep(1.8)
        check("未分类课堂有切换按钮", c.js("!!document.querySelector('#crumbPick')"), True)
        c.js("document.querySelector('#crumbPick').click()")
        time.sleep(0.5)
        check("菜单弹出", c.js("!document.querySelector('#courseMenu').classList.contains('hidden')"), True)
        check("菜单含未分类选项", c.js(
            "[...document.querySelectorAll('#courseMenu button')].some(b=>b.textContent.trim()==='未分类')"), True)
        c.js("(()=>{const b=[...document.querySelectorAll('#courseMenu button')]"
             ".find(x=>x.textContent.includes('操作系统'));b.click();})()")
        time.sleep(2.0)
        check("挪到操作系统", c.js("document.querySelector('#crumbCourse').textContent.trim()"), "操作系统")
        c.js("document.querySelector('#crumbPick').click()")
        time.sleep(0.5)
        c.js("(()=>{const b=[...document.querySelectorAll('#courseMenu button')]"
             ".find(x=>x.textContent.trim()==='未分类');b.click();})()")
        time.sleep(1.8)
        check("还能挪回未分类", c.js("document.querySelector('#crumbCourse').textContent.trim()"), "未分类")
        check("点空白处菜单收起", c.js(
            "document.querySelector('#courseMenu').classList.contains('hidden')"), True)

        print("\n[课程改名与删除]")

        c.js("openCourse('c0001')")
        time.sleep(1.4)
        check("打开课程", c.js("state.course.name"), "操作系统")
        c.js("document.querySelector('#btnRenameCourse').click()")
        time.sleep(0.4)
        check("课程名进入编辑态", c.js("document.querySelector('#courseName').contentEditable"), "true")
        c.js("(()=>{const el=document.querySelector('#courseName');el.textContent='操作系统（改）';"
             "el.dispatchEvent(new Event('blur'));})()")
        time.sleep(1.8)
        check("课程改名生效", c.js(
            "[...document.querySelectorAll('.node-name')].map(e=>e.textContent).includes('操作系统（改）')"), True)

        c.js("document.querySelector('#btnDeleteCourse').click()")
        time.sleep(0.6)
        check("删除课程前先确认", c.js(
            "!!document.querySelector('.modal.open [data-act=\"yes\"]')"), True)
        check("确认文案说明课堂不会被删", c.js(
            "document.body.innerText.includes('不会被删除')"), True)
        c.js("document.querySelector('.modal.open [data-act=\"yes\"]').click()")
        time.sleep(2.0)

        check("删除后有结果提示", c.js("document.querySelector('.toasts')?.innerText"),
              lambda t: t is not None and ("已删除" in t))
        check("课程节点少一个", c.js("document.querySelectorAll('.course-node').length"), 2)
        check("课堂一条没丢", c.js("document.querySelectorAll('.lesson-item').length"), 4)
        check("课堂都回到未分类", c.js(
            "document.querySelector('.course-node[data-cid=\"\"] .node-count').textContent"), "4")

        print("\n[窄屏]")
        c.viewport(420, 880, mobile=True)
        time.sleep(0.8)
        check("窄屏不横向溢出", c.js(
            "document.documentElement.scrollWidth <= window.innerWidth + 2"), True)

        print("\n[运行时错误]")
        errs = c.js("(window.__errs||[]).slice(0,5)")
        check("无 JS 报错", errs, [])

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
            print("\n服务端日志（末尾 2KB，有失败时才有用）：\n" + tail)
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
