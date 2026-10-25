"""离线端到端自检：不调用任何真实 API，验证整条流水线。

覆盖：
1. PPTX / Markdown 讲义解析，多份讲义的全局页码摊平
2. 转写文本 -> 带时间戳的行
3. LLM 返回 JSON 的规整（越界页码、错误类型、缺失字段）
4. 按引用页渲染讲义截图，页码一一对应
5. 四种产物（极限版/详细版 × md/docx）生成，md 图片链接真实存在

用法：
    python tests/e2e_offline.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import os
import sys
import tempfile
import time
from pathlib import Path

# 自检必须写进临时目录，绝不能碰用户真实的课堂数据
os.environ.setdefault("BUKETANG_DATA_DIR", tempfile.mkdtemp(prefix="bt_selftest_"))

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core.config as config          # noqa: E402
from core import analyze as analyze_mod  # noqa: E402
from core.llm import LLMClient        # noqa: E402
from core.store import store, transcript_to_text  # noqa: E402

TMP = config.DATA_DIR / "selftest"
LESSON_NAME = "[自检] 神经网络入门"

passed: list[str] = []
failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    if cond:
        passed.append(name)
        print(f"  [OK] {name}")
    else:
        failed.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")
    return bool(cond)


# ---------------- 制造素材 ----------------

SLIDES = [
    ("课程目标", ["理解神经网络的基本结构", "掌握前向传播的计算过程", "能推导反向传播的链式法则"]),
    ("神经元模型", ["输入 x 与权重 w 做内积", "加上偏置 b 后过激活函数", "常用激活函数：Sigmoid、ReLU、tanh"]),
    ("前向传播", ["逐层计算 z = Wx + b", "a = σ(z) 得到本层输出", "最后一层输出用于计算损失"]),
    ("损失函数", ["回归用均方误差 MSE", "分类用交叉熵 Cross Entropy", "损失越小说明预测越接近真值"]),
    ("反向传播", ["核心是链式法则", "误差从输出层向输入层逐层回传", "每层梯度 = 上游梯度 × 本层局部梯度"]),
    ("梯度下降", ["参数更新 w := w - η·∂L/∂w", "学习率 η 太大会震荡，太小收敛慢", "批量大小影响梯度估计的方差"]),
]

MD_HANDOUT = """# 神经网络入门 · 课堂补充讲义

## 一、为什么需要非线性激活

如果没有激活函数，多层线性变换叠加起来仍然是一个线性变换，
网络再深也等价于一层。激活函数引入非线性，才让网络具备拟合复杂函数的能力。

## 二、梯度消失与梯度爆炸

Sigmoid 的导数最大只有 0.25，深层网络连乘后梯度会指数级衰减，这就是梯度消失。
权重初始化过大时则相反，梯度会指数级增长，即梯度爆炸。

## 三、常用优化器对比

- SGD：最基础，收敛慢但对超参不敏感
- Momentum：引入动量，缓解震荡
- Adam：自适应学习率，实践中最常用
"""


def make_pptx(path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Pt

    prs = Presentation()
    for title, bullets in SLIDES:
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = title
        tf = slide.placeholders[1].text_frame
        tf.text = bullets[0]
        for line in bullets[1:]:
            p = tf.add_paragraph()
            p.text = line
            p.font.size = Pt(18)
    prs.save(str(path))


def make_mp3(path: Path, seconds: int) -> bool:
    exe = shutil.which("ffmpeg")
    if not exe:
        return False
    cmd = [
        exe, "-y", "-f", "lavfi", "-i", f"anullsrc=r=16000:cl=mono",
        "-t", str(seconds), "-c:a", "libmp3lame", "-b:a", "32k", str(path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return path.exists() and path.stat().st_size > 0
    except (subprocess.CalledProcessError, OSError):
        return False


# ---------------- 假的 LLM ----------------

def fake_analysis(valid_pages: list[int]) -> dict:
    """故意塞进几种脏数据：越界页码、字符串重要度、错误类型、缺字段。"""
    p = valid_pages
    pick = lambda i: [p[min(i, len(p) - 1)]] if p else []  # noqa: E731
    return {
        "topic": "神经网络与反向传播",
        "summary": "从神经元模型讲到前向传播、损失函数、反向传播与梯度下降，"
                   "重点是链式法则的推导和梯度消失的成因。",
        "chapters": [
            {"title": "神经元与前向传播", "pages": pick(0) + pick(1), "gist": "结构与本层输出计算"},
            {"title": "损失函数", "pages": pick(3), "gist": "MSE 与交叉熵的适用场景"},
            {"title": "反向传播与优化", "pages": pick(4) + pick(5), "gist": "链式法则与参数更新"},
        ],
        "key_points": [
            {
                "title": "神经元计算过程",
                "detail": "输入向量与权重做内积，加偏置后过激活函数，得到本层输出。",
                "type": "原理",
                "importance": "5",                     # 字符串，应被转成整数
                "pages": pick(1),
                "timestamp": "00:02:10",
                "exam_hint": "可能让你写出一个神经元的输出表达式。",
            },
            {
                "title": "前向传播的矩阵形式",
                "detail": "整层用矩阵乘法一次算完：Z = XW + b，再逐元素过激活函数。",
                "type": "方法",
                "importance": 4,
                "pages": pick(2) + [999],              # 999 越界，应被丢掉
                "timestamp": "3:40",                   # 无小时，应规整成 03:40
                "exam_hint": "给维度让你判断能否相乘。",
            },
            {
                "title": "交叉熵与均方误差的区别",
                "detail": "分类任务用交叉熵，回归任务用均方误差；交叉熵梯度更陡，收敛更快。",
                "type": "概念",
                "importance": 5,
                "pages": pick(3),
                "timestamp": "00:18:05",
                "exam_hint": "选择题常考两者适用场景。",
            },
            {
                "title": "反向传播的链式法则",
                "detail": "误差从输出层往前逐层回传，每层梯度等于上游梯度乘以本层局部梯度。",
                "type": "公式",
                "importance": 5,
                "pages": pick(4),
                "timestamp": "00:31:20",
                "exam_hint": "大题必考，要能手推两层网络的梯度。",
            },
            {
                "title": "梯度消失的成因",
                "detail": "Sigmoid 导数最大 0.25，多层连乘后梯度指数衰减，深层参数几乎不更新。",
                "type": "原理",
                "importance": 4,
                "pages": pick(5),
                "timestamp": "",
                "exam_hint": "简答题：为什么深层网络难训练。",
            },
            {   # 脏数据：没有 title 也没有 detail，应被丢弃
                "title": "",
                "detail": "",
                "importance": 2,
                "pages": [],
            },
            {
                "title": "学习率的影响",
                "detail": "学习率过大会在最优点附近震荡甚至发散，过小则收敛缓慢。",
                "type": "结论",
                "importance": 3,
                "pages": pick(5),
                "timestamp": "00:47:12",
                "exam_hint": "结合损失曲线判断学习率是否合适。",
            },
        ],
        "exam_focus": [
            {"item": "手推两层网络的反向传播", "why": "老师课上花 20 分钟完整推导了一遍，"
             "并明说这是考试重点。", "how": "计算题：给出网络结构和损失，求各层梯度。",
             "pages": pick(4), "importance": 5},
            {"item": "梯度消失的成因与缓解", "why": "在补充讲义里单独成节。",
             "how": "简答题。", "pages": pick(5), "importance": 4},
            {"item": "交叉熵 vs 均方误差", "why": "两种损失函数都讲了且做了对比。",
             "how": "选择题。", "pages": pick(3), "importance": 4},
        ],
        "pitfalls": [
            {"point": "把激活函数当成线性变换，忽略其非线性作用", "pages": pick(1)},
            {"point": "反向传播时忘记乘以本层激活函数的导数", "pages": pick(4)},
            {"point": "混淆 batch size 与学习率的作用", "pages": pick(5)},
        ],
        "emphasis": [
            {"quote": "这个链式法则你们一定要会推，考试就考这个",
             "timestamp": "00:31:20", "note": "老师重复了三次"},
            {"quote": "Sigmoid 现在基本只在输出层用了", "timestamp": "00:05:44",
             "note": "隐含考点：ReLU 为什么更常用"},
        ],
        "extensions": [
            {"topic": "为什么 ReLU 缓解梯度消失",
             "content": "ReLU 在正区间的导数恒为 1，连乘不会指数衰减；"
                        "但负区间导数为 0，可能出现神经元死亡。这是课堂外的补充说明。",
             "pages": []},
        ],
    }


_fake_payload: dict = {}


def patch_llm() -> None:
    LLMClient.validate = lambda self: None  # type: ignore[assignment]

    def fake_chat_json(self, messages, **kwargs):
        time.sleep(0.05)
        return _fake_payload

    LLMClient.chat_json = fake_chat_json  # type: ignore[assignment]


# ---------------- 主流程 ----------------

def main() -> int:
    print("=" * 62)
    print("  不听课 · 离线端到端自检")
    print("=" * 62)

    config.ensure_dirs()
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)

    # 清理上一次的自检课堂
    for lesson in list(store.all()):
        if lesson["name"] == LESSON_NAME:
            store.delete(lesson["id"])

    print("\n[1/6] 制造测试素材")
    pptx_path = TMP / "神经网络入门.pptx"
    md_path = TMP / "补充讲义.md"
    mp3_path = TMP / "课堂录音.mp3"
    make_pptx(pptx_path)
    md_path.write_text(MD_HANDOUT, encoding="utf-8")
    has_audio = make_mp3(mp3_path, 120)
    check("PPTX 生成成功", pptx_path.exists() and pptx_path.stat().st_size > 0)
    check("Markdown 讲义生成成功", md_path.exists())
    check("测试音频生成成功（ffmpeg）", has_audio)

    print("\n[2/6] 建课堂并导入讲义")
    lesson = store.create(LESSON_NAME)
    lid = lesson["id"]
    m1 = store.add_material(lid, pptx_path.name, pptx_path)
    m2 = store.add_material(lid, md_path.name, md_path)
    store.add_audio(lid, mp3_path.name, mp3_path, mp3_path.stat().st_size)

    deadline = time.time() + 120
    while time.time() < deadline:
        mats = store.get(lid)["materials"]
        if all(m["status"] != "pending" for m in mats):
            break
        time.sleep(0.5)

    mats = store.get(lid)["materials"]
    ppt_mat = next(m for m in mats if m["id"] == m1["id"])
    md_mat = next(m for m in mats if m["id"] == m2["id"])
    check("PPTX 解析成功", ppt_mat["status"] == "parsed",
          ppt_mat.get("error", ""))
    check("PPTX 转 PDF 成功", bool(ppt_mat["pdf_path"]) and Path(ppt_mat["pdf_path"]).exists())
    check("PPTX 页数正确", ppt_mat["page_count"] == len(SLIDES),
          f"得到 {ppt_mat['page_count']}，预期 {len(SLIDES)}")
    check("PPTX 提取到文字", ppt_mat["chars"] > 80, f"chars={ppt_mat['chars']}")
    check("PPTX 可截图", ppt_mat["can_shot"] is True)
    check("Markdown 解析成功", md_mat["status"] == "parsed", md_mat.get("error", ""))
    check("Markdown 提取到文字", md_mat["chars"] > 100, f"chars={md_mat['chars']}")

    total_pages = ppt_mat["page_count"] + md_mat["page_count"]
    print(f"    讲义共 {len(mats)} 份 / {total_pages} 页")

    print("\n[3/6] 伪造转写并跑完整流水线")
    fake_segments = [
        {"start": 0.0, "end": 12.0, "text": "今天我们讲神经网络的基本结构。", "speaker": None},
        {"start": 12.0, "end": 40.5, "text": "先看一个神经元，输入和权重做内积，加偏置，过激活函数。", "speaker": None},
        {"start": 130.0, "end": 190.0, "text": "前向传播可以写成矩阵形式，Z 等于 XW 加 b。", "speaker": None},
        {"start": 1085.0, "end": 1120.0, "text": "分类问题用交叉熵，回归问题用均方误差。", "speaker": None},
        {"start": 1880.0, "end": 1960.0, "text": "这个链式法则你们一定要会推，考试就考这个。", "speaker": None},
        {"start": 2820.0, "end": 2890.0, "text": "Sigmoid 导数最大只有零点二五，连乘以后梯度就没了。", "speaker": None},
    ]
    _fake_payload = fake_analysis(list(range(1, total_pages + 1)))
    globals()["_fake_payload"] = _fake_payload
    patch_llm()

    store._transcribe_all = lambda lesson_id, cfg: fake_segments  # type: ignore[assignment]

    store._run(lid)
    lesson = store.get(lid)
    check("流水线状态为已完成", lesson["status"] == "done",
          f"{lesson['status']} / {lesson.get('error', '')}")
    check("转写文本带时间戳", "[00:00:00]" in lesson["transcript_text"])
    check("进度到 100", lesson["progress"] >= 99.9, str(lesson["progress"]))

    print("\n[4/6] 校验分析结果规整")
    an = lesson["analysis"] or {}
    kps = an.get("key_points") or []
    check("脏数据被丢弃（6 条有效考点）", len(kps) == 6, f"实际 {len(kps)} 条")
    valid = set(range(1, total_pages + 1))
    check("页码全部在有效范围内",
          all(set(kp["pages"]) <= valid for kp in kps),
          str([kp["pages"] for kp in kps]))
    check("越界页码 999 被剔除",
          all(999 not in kp["pages"] for kp in kps))
    check("字符串重要度被转成整数",
          all(isinstance(kp["importance"], int) for kp in kps))
    check("重要度降序", [kp["importance"] for kp in kps] ==
          sorted([kp["importance"] for kp in kps], reverse=True),
          str([kp["importance"] for kp in kps]))
    ts = [kp["timestamp"] for kp in kps]
    check("时间戳被规整成 HH:MM", "03:40" in ts, str(ts))
    check("考点预测已提取", len(an.get("exam_focus") or []) == 3)
    check("易错点已提取", len(an.get("pitfalls") or []) == 3)
    check("拓展补充已提取", len(an.get("extensions") or []) == 1)
    check("统计信息完整", (an.get("stats") or {}).get("pages") == total_pages,
          str(an.get("stats")))

    print("\n[5/6] 校验产物与截图")
    guides = lesson["guides"]
    out_dir = store.outputs_dir(lid)
    for key in ("extreme_md", "detailed_md", "extreme_docx", "detailed_docx"):
        check(f"{key} 已生成", bool(guides.get(key)) and Path(guides[key]).exists())

    detailed = Path(guides["detailed_md"]).read_text(encoding="utf-8")
    extreme = Path(guides["extreme_md"]).read_text(encoding="utf-8")
    check("详细版比极限版更长", len(detailed) > len(extreme),
          f"{len(detailed)} vs {len(extreme)}")
    check("极限版不含截图", "![" not in extreme)
    check("详细版含截图引用", detailed.count("![讲义第") >= 3,
          f"{detailed.count('![讲义第')} 处")

    imgs = [l.split("](")[1].rstrip(")") for l in detailed.splitlines()
            if l.startswith("![讲义第")]
    missing = [rel for rel in imgs if not (out_dir / rel).exists()]
    check("详细版图片链接真实存在", not missing, str(missing))
    check("极限版保留考点数受设置限制",
          extreme.count("### ") <= 12, str(extreme.count("### ")))

    from docx import Document
    doc = Document(guides["detailed_docx"])
    check("详细版 Word 内嵌截图", len(doc.inline_shapes) >= 3,
          f"{len(doc.inline_shapes)} 张")
    doc_e = Document(guides["extreme_docx"])
    check("极限版 Word 无图", len(doc_e.inline_shapes) == 0)
    check("Word 有正文内容", len(doc.paragraphs) > 20, str(len(doc.paragraphs)))

    print("\n[6/6] 汇总")
    print(f"  产物目录：{out_dir}")
    for p in sorted(out_dir.rglob("*")):
        if p.is_file():
            print(f"    {p.relative_to(out_dir)}  ({p.stat().st_size // 1024} KB)")

    print("\n" + "-" * 62)
    print(f"  通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        for f in failed:
            print(f"    FAIL: {f}")
        print("-" * 62)
        return 1
    print("  全部通过 ✓")
    print("-" * 62)
    print(f"\n课堂 ID: {lid}（可在网页里打开查看，如需清理点删除）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
