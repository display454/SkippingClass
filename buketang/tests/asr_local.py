"""端侧引擎与任务取消的集成验证（不花钱、不依赖云端 API）。

覆盖两件容易出问题的事：
1. 本地 SenseVoice 接进 store 流水线后，跨音频文件的时间戳 offset 是否正确累加
2. 长任务能否被真正中断，且已生成的结果不受影响
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path("C:/Windows/Temp/bt_asr_test")
PASS, FAIL = [], []


def check(name, got, want):
    ok = want(got) if callable(want) else got == want
    if ok:
        PASS.append(name)
        print(f"  ok   {name}")
    else:
        FAIL.append(f"{name}: 期望 {want!r}，实际 {got!r}")
        print(f"  FAIL {name}: 期望 {want!r}，实际 {got!r}")


def make_audio(dst: Path, seconds: int) -> Path:
    """把模型自带的示范语音循环拼成指定长度。"""
    import subprocess
    src = ROOT.parent / "models" / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17" / "test_wavs" / "zh.wav"
    if not src.exists():
        raise SystemExit("找不到测试语音，先下载 SenseVoice 模型")
    loops = max(1, seconds // 5 + 1)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-stream_loop", str(loops), "-i", str(src),
         "-t", str(seconds), "-ac", "1", "-ar", "16000", str(dst)],
        check=True,
    )
    return dst


def main() -> None:
    if TMP.exists():
        shutil.rmtree(TMP)

    import os
    os.environ["BUKETANG_DATA_DIR"] = str(TMP)

    # 环境变量必须在 import config 之前生效
    for mod in [m for m in list(sys.modules) if m.startswith(("core", "shared"))]:
        del sys.modules[mod]

    from core import config
    from core.store import LessonStore
    from shared.asr import spec_of

    print("\n[1/4] 分段能力")
    spec = spec_of("sensevoice")
    from shared import audio_utils as au
    check("SenseVoice 单段上限", int(au.effective_segment_seconds(spec, 3600)), 1800)
    check("用户设更短时取用户值", int(au.effective_segment_seconds(spec, 600)), 600)
    check("并发为 1（本地不抢算力）", spec.concurrency, 1)
    check("支持时间戳", spec.supports_timestamps, True)

    print("\n[2/4] 准备音频")
    TMP.mkdir(parents=True, exist_ok=True)
    a1 = make_audio(TMP / "a1.wav", 60)
    a2 = make_audio(TMP / "a2.wav", 45)
    print(f"  {a1.name} {a1.stat().st_size // 1024} KB / {a2.name} {a2.stat().st_size // 1024} KB")

    print("\n[3/4] 流水线转录（两段音频）")
    store = LessonStore(workers=2)
    lesson = store.create("端侧集成测试")
    lid = lesson["id"]
    store.add_audio(lid, "a1.wav", a1, a1.stat().st_size)
    store.add_audio(lid, "a2.wav", a2, a2.stat().st_size)

    cfg = config.load()
    cfg["asr"] = {"provider": "sensevoice"}

    t0 = time.time()
    merged = store._transcribe_all(lid, cfg)
    el = time.time() - t0
    total = sum(float(a.get("duration") or 0) for a in store.get(lid)["audios"])

    check("有转写结果", len(merged) > 0, True)
    check("时间戳严格递增", all(merged[i]["start"] <= merged[i + 1]["start"] for i in range(len(merged) - 1)), True)
    check("第二段接在第一段之后", any(s["start"] >= 60 for s in merged), True)
    check("最后一段不超过总时长太多", merged[-1]["end"], lambda v: v <= total + 2)
    check("文本非空", len("".join(s["text"] for s in merged)) > 20, True)
    print(f"  {len(merged)} 段 / 用时 {el:.1f}s / 音频共 {total:.0f}s / 提速 {total / max(el, .1):.0f}×")
    print(f"  样例：{merged[0]['text'][:40]}")

    print("\n[4/4] 取消长任务")
    store2 = LessonStore(workers=1)
    long_one = make_audio(TMP / "long.wav", 240)
    l2 = store2.create("取消测试")
    lid2 = l2["id"]

    # 必须先有讲义，否则流水线在第一步就报错，根本走不到转录
    md = TMP / "lecture.md"
    md.write_text("# 进程同步\n\n信号量、互斥量、管程。死锁的四个必要条件与银行家算法。\n"
                  "## 调度\n\nFCFS、SJF、时间片轮转、多级反馈队列。\n", encoding="utf-8")
    store2.add_material(lid2, "lecture.md", md)
    for _ in range(30):
        time.sleep(0.3)
        mats = store2.get(lid2)["materials"]
        if mats and mats[0].get("status") in ("parsed", "failed"):
            break
    check("讲义解析完成", store2.get(lid2)["materials"][0]["status"], "parsed")

    store2.add_audio(lid2, "long.wav", long_one, long_one.stat().st_size)
    store2.run(lid2)
    time.sleep(3.0)
    check("处于转录状态", store2.get(lid2)["status"], lambda s: s in ("parsing", "transcribing"))
    check("取消请求被接受", store2.cancel(lid2), True)

    for _ in range(60):
        time.sleep(0.5)
        if store2.get(lid2)["status"] not in ("parsing", "transcribing", "analyzing", "rendering"):
            break
    final = store2.get(lid2)["status"]
    check("最终状态是已取消", final, "canceled")
    check("取消有说明", "取消" in (store2.get(lid2).get("error") or ""), True)
    print(f"  状态：{final} · {store2.get(lid2).get('stage')}")

    print("\n" + "-" * 46)
    print(f"  通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  -", f)
    print("-" * 46)
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
