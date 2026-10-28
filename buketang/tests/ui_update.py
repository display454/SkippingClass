"""Exercise v1.1 controls in a real browser using isolated, explicit fixtures."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))

import seed
from ui_check import boot
from shared.devtools.cdp import CDP
from shared.transcript import fingerprint, normalize


def main():
    passed = []
    with tempfile.TemporaryDirectory(prefix="buketang_ui_update_") as temp:
        root = Path(temp)
        data = root / "data"
        seed.seed(data)
        record = data / "lessons/l0001/lesson.json"
        lesson = json.loads(record.read_text(encoding="utf-8"))
        raw = normalize([{"text": "课程原文", "start": 0, "end": 1},
                         {"text": "今天吃什么", "start": 2, "end": 3},
                         {"text": "这个不考", "start": 4, "end": 5}])
        lesson["transcript"] = raw
        lesson["transcript_text"] = "[00:00:00] 课程原文\n[00:00:02] 今天吃什么\n[00:00:04] 这个不考"
        lesson["cleaned"] = {"version": "fixture", "source_hash": fingerprint(raw),
                             "annotations": {raw[1]["id"]: {"category": "irrelevant", "keep": False, "reason": "明确无关"}}}
        lesson["history"] = [{"id": "legacy", "guides": dict(lesson["guides"]), "created": 1}]
        revision = "rev_000000000001"
        output = data / "lessons/l0001/outputs" / revision
        output.mkdir()
        content = ("# 富文本验收\n\n行内公式 $x_i^2$。\n\n$$\n\\frac{a}{b} + \\sum_{i=1}^{n}x_i\n$$\n\n"
                   "| 符号 | 含义 |\n|---|---|\n| $w$ | 权重 |\n\n"
                   "![示意图](https://example.com/diagram.png)\n\n<script>window.injected=true</script>")
        for mode in ("extreme", "detailed"):
            path = output / f"{mode}.md"
            path.write_text(content, encoding="utf-8")
            lesson["guides"][mode + "_md"] = str(path)
        lesson["current_revision"] = revision
        record.write_text(json.dumps(lesson, ensure_ascii=False), encoding="utf-8")
        proc, base = boot(data, root / "server.log")
        browser = None
        try:
            browser = CDP(base)
            browser.goto(base, 2)
            browser.watch_errors()
            browser.js("openLesson('l0001')")
            time.sleep(1)
            def check(label, expression, expected=True):
                actual = browser.js(expression)
                if actual != expected:
                    raise AssertionError(f"{label}: {actual!r} != {expected!r}")
                passed.append(label)
                print("PASS", label, flush=True)
            check("offline inline and display math", "document.querySelectorAll('#resultBody .katex').length >= 3")
            check("real rendered table", "document.querySelectorAll('#resultBody table').length===1")
            check("untrusted HTML is text", "window.injected!==true")
            check("external images are opt-in", "!!document.querySelector('[data-remote-image]') && !document.querySelector('img[src^=\"https://example.com\"]')")
            check("stage and vision controls", "['btnTranscribe','btnPreprocess','btnExport','btnFormulaEvidence'].every(id=>!!document.getElementById(id))")
            browser.js("document.querySelector('#resultBody').scrollIntoView();document.fonts.ready")
            browser.shot("v1.1-richtext", HERE.parent.parent / ".venv/qa")
            browser.js("loadTranscriptView('clean')")
            time.sleep(.5)
            check("clean view excludes chatter", "!document.querySelector('#transcriptBody').innerText.includes('今天吃什么')")
            check("exam notice retained", "document.querySelector('#transcriptBody').innerText.includes('这个不考')")
            browser.js("loadTranscriptView('removed')")
            time.sleep(.4)
            check("removed content remains inspectable", "document.querySelector('#transcriptBody').innerText.includes('今天吃什么')")
            browser.js("document.querySelector('[data-segment-toggle]').click()")
            time.sleep(.7)
            browser.js("loadTranscriptView('clean')")
            time.sleep(.4)
            check("removed segment can be restored", "document.querySelector('#transcriptBody').innerText.includes('今天吃什么')")
            browser.js("window.prompt=()=> '人工修订';document.querySelector('[data-segment-edit]').click()")
            time.sleep(.7)
            check("manual correction visible", "document.querySelector('#transcriptBody').innerText.includes('人工修订')")
            browser.js("loadTranscriptView('raw')")
            time.sleep(.4)
            check("original remains unchanged", "document.querySelector('#transcriptBody').innerText.includes('课程原文') && !document.querySelector('#transcriptBody').innerText.includes('人工修订')")
            check("JSON export includes annotations", "(async()=>{const r=await fetch('/api/lessons/l0001/transcript/download?format=json');const d=await r.json();return d.segments[0].text==='课程原文' && !!d.cleaned.annotations})()")
            browser.js("document.querySelector('#revisionSelect').value='legacy';document.querySelector('#revisionSelect').dispatchEvent(new Event('change'))")
            time.sleep(.6)
            check("historical guide can be read", "document.querySelector('#resultBody').innerText.includes('进程')")
            browser.js("openCourse('c0001')")
            time.sleep(.5)
            browser.js("document.querySelector('#memoryNotes').value='符号 w 表示权重';document.querySelector('#btnSaveMemory').click()")
            time.sleep(.5)
            check("course notes saved", "(async()=>{const d=await (await fetch('/api/courses')).json();return d.courses.find(x=>x.id==='c0001').memory_notes==='符号 w 表示权重'})()")
            browser.js("(()=>{const files=new DataTransfer();files.items.add(new File(['课程一'],'批量一.txt',{type:'text/plain'}));files.items.add(new File(['课程二'],'批量二.md',{type:'text/markdown'}));document.querySelector('#batchFiles').files=files.files;document.querySelector('#batchFiles').dispatchEvent(new Event('change'));})()")
            check("batch import offers grouping review", "document.querySelectorAll('[data-batch-group]').length===2")
            browser.js("document.querySelector('#btnBatchImport').onclick()")
            check("batch files create separate course lessons", "(async()=>{const d=await (await fetch('/api/lessons?course=c0001')).json();return d.some(l=>l.name==='批量一') && d.some(l=>l.name==='批量二')})()")
            browser.js("document.querySelector('#btnViewMemory').click()")
            time.sleep(.4)
            check("memory provenance accessible", "!document.querySelector('#memorySources').classList.contains('hidden')")
            browser.js("openLesson('l0001');")
            time.sleep(.5)
            browser.viewport(420, 880, mobile=True)
            time.sleep(.4)
            check("mobile layout does not overflow", "document.documentElement.scrollWidth <= window.innerWidth+2")
            check("no runtime errors", "JSON.stringify(window.__errs||[])", "[]")
        finally:
            if browser:
                browser.close()
            proc.terminate()
            proc.wait(timeout=8)
    print(f"Passed {len(passed)} update UI checks")


if __name__ == "__main__":
    main()
