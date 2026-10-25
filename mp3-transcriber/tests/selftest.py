"""端到端自检：不消耗接口额度，用演示模式验证整条链路。

运行：python tests/selftest.py

数据落在临时目录（`TRANSCRIBER_DATA_DIR`），不会污染你 `data/` 里的真实任务。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在 import core.config 之前设好：数据目录在模块加载时就定下来了
os.environ.setdefault("TRANSCRIBER_DATA_DIR", tempfile.mkdtemp(prefix="tr_selftest_"))

from core import config, render                  # noqa: E402
from core.pipeline import store                  # noqa: E402
from shared import audio_utils as audio          # noqa: E402
from shared.asr import catalog, spec_of          # noqa: E402

TMP = config.DATA_DIR / "selftest"


def make_audio(path: Path, seconds: int) -> None:
    ff = audio.find_ffmpeg()
    if not ff:
        raise SystemExit("缺少 ffmpeg，无法自检")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        ff, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-ac", "1", "-ar", "16000", "-b:a", "32k", str(path),
    ], check=True)


def check(label: str, cond: bool, detail: str = "") -> bool:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")
    return cond


def main() -> int:
    ok = True
    config.ensure_dirs()
    TMP.mkdir(parents=True, exist_ok=True)

    print("\n[1/6] 生成测试音频（25 分钟，用于验证多段切片）")
    src = TMP / "selftest_25min.mp3"
    if not src.exists():
        make_audio(src, 25 * 60)
    ok &= check("测试音频已生成", src.exists() and src.stat().st_size > 0,
                f"{src.stat().st_size / 1024 / 1024:.1f} MB")

    print("\n[2/6] 音频探测与分段规划")
    info = audio.probe(src)
    ok &= check("时长识别", 1495 <= info["duration"] <= 1505, f'{info["duration"]:.1f}s')
    plan = audio.plan_segments(info["duration"], 300)
    ok &= check("按 5 分钟切分并保留编码尾段",
                len(plan) in (5, 6) and all(0 < length <= 300 for _, length in plan)
                and abs(sum(length for _, length in plan) - info["duration"]) < 0.01,
                str(plan))

    print("\n[3/6] 多引擎分段自适应")
    for pid in ("volc", "openai", "dashscope", "local"):
        spec = spec_of(pid)
        secs = audio.effective_segment_seconds(spec, 20 * 60)
        ok &= check(f"{pid} 分段不超过引擎上限", secs <= spec.max_segment_seconds,
                    f"{secs // 60} 分钟")
    ok &= check("引擎目录可枚举", len(catalog()) >= 4, f"{len(catalog())} 个")

    print("\n[4/6] 执行完整流水线（演示模式）")
    cfg = config.load()
    cfg.update({
        "mock": True,
        "provider": "volc",
        "segment_minutes": 5,
        "outputs": ["md", "docx", "txt"],
        "include_timeline": True,
    })

    job = store.create(filename=src.name, src_path=str(src), size=src.stat().st_size)
    t0 = time.time()
    store._run(job.id, cfg)
    elapsed = time.time() - t0

    job = store.get(job.id)
    ok &= check("任务状态为 done", job.status == "done", f"{job.stage} / 用时 {elapsed:.1f}s")
    if job.status == "failed":
        print(f"        错误：{job.error}")

    print("\n[5/6] 校验产物")
    for fmt in ("md", "docx", "txt"):
        p = Path(job.outputs.get(fmt, ""))
        ok &= check(f"{fmt} 已生成", p.exists() and p.stat().st_size > 0,
                    f"{p.stat().st_size / 1024:.1f} KB" if p.exists() else "缺失")

    md_path = Path(job.outputs["md"])
    md = md_path.read_text(encoding="utf-8")
    ok &= check("Markdown 含 front matter", md.startswith("---"))
    ok &= check("Markdown 含文字稿章节", "## 文字稿" in md)
    ok &= check("Markdown 含时间轴章节", "## 时间轴" in md)
    ok &= check("时间轴覆盖到最后一分钟", "[20:" in md or "[21:" in md or "[22:" in md)

    # 时间轴单调递增
    stamps = [
        int(m.group(1)) * 60 + int(m.group(2))
        for m in __import__("re").finditer(r"\[(\d{2}):(\d{2})\]", md)
    ]
    ok &= check("时间轴单调不减", all(b >= a for a, b in zip(stamps, stamps[1:])),
                f"{len(stamps)} 个时间点")

    print("\n[6/6] 校验 Word 结构")
    from docx import Document
    doc = Document(str(job.outputs["docx"]))
    texts = [p.text for p in doc.paragraphs if p.text.strip()]
    ok &= check("Word 段落数正常", len(texts) > 10, f"{len(texts)} 段")
    ok &= check("Word 含文字稿标题", any(t == "文字稿" for t in texts))
    ok &= check("Word 含时间轴标题", any(t == "时间轴" for t in texts))

    print("\n--- Markdown 前 700 字 ---")
    print(md[:700])
    print("--- 截断 ---")

    print("\n--- 产物路径 ---")
    for fmt, p in job.outputs.items():
        print(f"  {fmt:5s} {p}")

    pass_cnt = "全部通过" if ok else "存在失败项"
    print(f"\n自检结果：{pass_cnt}")
    print("提示：这是演示模式的产物，接入真实 API Key 后关闭演示模式即可得到真实文本。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
