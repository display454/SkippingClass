"""生成一套可复现的演示数据。

两个用途：
1. 给 tests/ui_check.py 提供固定 fixture —— 课堂 id、课程 id、考点都写死，
   测试断言就不用跟着真实数据漂移；
2. 截图和演示时不必翻自己的课堂，跑一条命令就有一份像样的数据。

用法：
    python tests/seed.py --dir C:/Temp/bt_demo
    BUKETANG_DATA_DIR=C:/Temp/bt_demo PORT=8799 python app.py

数据里不含真实录音内容（只有 1 秒静音 wav），但结构、字段、状态都是真的，
分析结果和复习指导也是按真实格式生成的。
"""
from __future__ import annotations

import argparse
import json
import shutil
import struct
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import summarize  # noqa: E402

# ---------------- 演示内容 ----------------

COURSES = [
    {"id": "c0001", "name": "操作系统", "term": "2026 秋", "color": "#8A6A45", "created": 1758000000},
    {"id": "c0002", "name": "数据结构", "term": "2026 秋", "color": "#4E6B58", "created": 1758000100},
]

LESSONS = [
    {
        "id": "l0001",
        "course_id": "c0001",
        "name": "第 1 讲 · 进程与线程",
        "topic": "进程与线程",
        "summary": "从程序的执行讲起，引出进程、线程、PCB 和进程状态转换。",
        "points": [
            ("进程控制块 PCB", "PCB 是操作系统感知进程存在的唯一标志，保存进程标识、处理机状态、调度信息和资源清单。", 5, "核心概念", "必考，注意和进程映像的区别"),
            ("进程的三种基本状态", "就绪、运行、阻塞。就绪是只差 CPU，阻塞是等 I/O 或事件，两者的转换方向不可逆。", 4, "核心概念", ""),
            ("进程与线程的区别", "进程是资源分配单位，线程是调度单位；同一进程的线程共享地址空间，切换开销更小。", 4, "对比辨析", "常出简答题"),
            ("进程调度算法", "先来先服务、短作业优先、时间片轮转、多级反馈队列。要会算平均周转时间和带权周转时间。", 3, "计算方法", ""),
        ],
        "transcript": [
            (0, "这学期我们讲操作系统，第一件事是把「进程」这个概念说清楚。"),
            (46, "程序是静态的，放在磁盘上；进程是动态的，是程序的一次执行过程。"),
            (132, "操作系统怎么知道一个进程存在？靠进程控制块，也就是 PCB。"),
            (210, "PCB 里面存了什么？进程标识符、处理机状态、调度信息、还有资源清单。"),
            (318, "进程有三个基本状态：就绪、运行、阻塞。注意就绪和阻塞的区别。"),
            (402, "就绪是万事俱备只欠 CPU，阻塞是在等某个事件，比如等 I/O 完成。"),
            (515, "调度算法这块，考试一定会算平均周转时间，大家把公式记牢。"),
        ],
    },
    {
        "id": "l0002",
        "course_id": "c0001",
        "name": "第 5 讲 · 信号量与死锁",
        "topic": "进程同步与死锁",
        "summary": "PV 操作、临界区互斥，以及死锁的四个必要条件和处理策略。",
        "points": [
            ("信号量 P/V 操作", "P 操作申请资源、信号量减一，V 操作释放资源、信号量加一。必须成对出现，顺序反了会死锁。", 5, "核心概念", "代码题高频"),
            ("死锁的四个必要条件", "互斥、请求并保持、不可剥夺、循环等待。四个同时成立才可能死锁，破坏任意一个就能预防。", 5, "核心概念", "必背"),
            ("进程控制块 PCB", "PCB 是操作系统感知进程存在的唯一标志，保存进程标识、处理机状态、调度信息和资源清单。", 4, "核心概念", ""),
            ("银行家算法", "在分配资源前先判断系统是否处于安全状态，是死锁避免的经典方法。", 4, "计算方法", ""),
        ],
        "transcript": [
            (0, "上一次讲进程，这次讲同步。先回忆一下进程控制块 PCB 里有什么。"),
            (58, "临界区的问题：多个进程同时访问共享变量，结果就不可预期了。"),
            (145, "信号量就是一个整数变量，配两个原子操作，P 和 V。"),
            (230, "P 操作是申请，信号量减一；V 操作是释放，信号量加一。"),
            (330, "死锁有四个必要条件，互斥、请求并保持、不可剥夺、循环等待。"),
            (430, "破坏其中任意一个，死锁就不会发生，这就是死锁预防的思路。"),
            (520, "银行家算法属于死锁避免，每次分配前先算一遍安全序列。"),
        ],
    },
    {
        "id": "l0003",
        "course_id": "c0001",
        "name": "第 9 讲 · 虚拟内存",
        "topic": "虚拟内存与页面置换",
        "summary": "页表、缺页中断与常见页面置换算法。",
        "points": [
            ("页面置换算法", "OPT、FIFO、LRU、Clock。FIFO 会出现 Belady 异常，LRU 性能接近 OPT 但开销大。", 4, "核心概念", ""),
            ("缺页中断处理流程", "查页表、产生缺页中断、调入页面、更新页表、恢复指令执行。", 4, "流程步骤", ""),
            ("死锁的四个必要条件", "互斥、请求并保持、不可剥夺、循环等待。四个同时成立才可能死锁，破坏任意一个就能预防。", 3, "核心概念", ""),
        ],
        "transcript": [
            (0, "虚拟内存要解决的是内存不够用的问题。"),
            (70, "每个进程都以为自己有一整块连续的内存，其实是页表映射出来的。"),
            (180, "访问的页不在内存里，就触发缺页中断，操作系统把页调进来。"),
            (290, "置换算法里 FIFO 最简单，但会有 Belady 异常，页框变多反而缺页更多。"),
        ],
    },
    {
        "id": "l0004",
        "course_id": "",
        "name": "第 2 讲 · 链表与树",
        "topic": "链表与二叉树",
        "summary": "单链表、双链表的插入删除，二叉树的遍历。",
        "points": [
            ("单链表插入与删除", "插入要改两个指针，删除要先找到前驱。头结点可以省掉大量边界判断。", 3, "代码实现", ""),
            ("二叉树三种遍历", "前序、中序、后序，递归写法要注意回溯位置。", 3, "核心概念", ""),
        ],
        "transcript": [
            (0, "链表和数组最大的区别是不要求连续空间。"),
            (90, "单链表插入，先让新结点指向后继，再让前驱指向新结点，顺序不能反。"),
        ],
    },
]


# ---------------- 生成器 ----------------

def _wav(path: Path, seconds: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<h", 0) * (16000 * seconds))


def _clock(sec: int) -> str:
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"[{h:02d}:{m:02d}:{s:02d}]"


def _guide_md(lesson: dict, mode: str) -> str:
    name, topic = lesson["name"], lesson["topic"]
    head = [
        "---",
        f'title: "{name}"',
        f'mode: {mode}',
        'generated: "2026-10-20 10:00"',
        "---",
        "",
        f"# {topic}",
        "",
        f"> {'极限版 · 只留必考' if mode == 'extreme' else '详细版 · 完整整理'} · 演示数据",
        "",
        "## 一句话说清这节课",
        "",
        lesson["summary"],
        "",
        "## 核心考点",
        "",
    ]
    body = []
    for i, (title, detail, imp, kind, hint) in enumerate(lesson["points"], 1):
        stars = "★" * imp + "☆" * (5 - imp)
        body.append(f"### {i}. {title} {stars}")
        body.append("")
        body.append(detail)
        body.append("")
        if hint:
            body.append(f"> {hint}")
            body.append("")
        body.append(f"*类型：{kind}*")
        body.append("")

    tail = [
        "## 考前检查表",
        "",
        "| 考点 | 类型 | 重要度 | 是否背熟 |",
        "|---|---|---|---|",
    ]
    for title, _detail, imp, kind, _hint in lesson["points"]:
        tail.append(f"| {title} | {kind} | {'★' * imp} | ☐ |")
    tail += ["", "---", "", "*这份内容由演示数据生成，用于检查排版，不代表真实课堂。*", ""]
    return "\n".join(head + body + tail)


def seed(target: Path) -> Path:
    target = target.resolve()
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    courses = [dict(c) for c in COURSES]
    written: list[dict] = []

    now = time.time()
    for idx, lesson in enumerate(LESSONS):
        lid = lesson["id"]
        base = target / "lessons" / lid
        files = base / "files"
        out = base / "outputs"
        files.mkdir(parents=True)
        out.mkdir(parents=True)

        audio_path = files / "demo.wav"
        _wav(audio_path)

        transcript_text = "\n".join(
            f"{_clock(sec)} {text}" for sec, text in lesson["transcript"]
        )

        guides = {}
        for mode in ("extreme", "detailed"):
            md_path = out / f"{mode}.md"
            md_path.write_text(_guide_md(lesson, mode), encoding="utf-8")
            guides[f"{mode}_md"] = str(md_path)

        key_points = []
        for title, detail, imp, kind, hint in lesson["points"]:
            key_points.append({
                "title": title,
                "detail": detail,
                "type": kind,
                "importance": imp,
                "pages": [idx + 1, idx + 2],
                "timestamp": "00:05:00",
                "exam_hint": hint,
            })

        data = {
            "id": lid,
            "name": lesson["name"],
            "course_id": lesson["course_id"],
            "created": now - (len(LESSONS) - idx) * 86400,
            "updated": now - idx * 3600,
            "status": "done",
            "stage": "",
            "progress": 100,
            "stale": False,
            "error": "",
            "materials": [{
                "id": f"m{lid[-3:]}",
                "filename": f"{lesson['topic']} · 讲义.pdf",
                "path": str(files / "demo.pdf"),
                "page_count": 42,
                "chars": 8600 + idx * 900,
                "status": "parsed",
                "can_shot": True,
                "warnings": [],
                "pages": [],
            }],
            "audios": [{
                "id": f"a{lid[-3:]}",
                "filename": "demo.wav",
                "path": str(audio_path),
                "size": audio_path.stat().st_size,
                "duration": 5400.0,
                "segments": len(lesson["transcript"]),
                "status": "done",
                "error": "",
            }],
            "transcript_text": transcript_text,
            "analysis": {
                "topic": lesson["topic"],
                "summary": lesson["summary"],
                "chapters": [{"title": "主线", "detail": lesson["summary"]}],
                "key_points": key_points,
                "exam_focus": [
                    {"item": f"{key_points[0]['title']}的原理", "why": "老师强调了三次",
                     "how": "先背定义，再画状态图", "pages": [1], "importance": 5}
                ],
                "pitfalls": [
                    {"point": "把「就绪」和「阻塞」混为一谈", "why": "两者等待的对象不同",
                     "pages": [3]}
                ],
                "emphasis": [{"text": "调度算法必须会算周转时间", "timestamp": "00:08:35"}],
                "extensions": [
                    {"topic": "延伸：Linux 的 CFS 调度", "content": "完全公平调度器用红黑树维护虚拟运行时间，是时间片轮转的现代实现。", "pages": [2]}
                ],
                "stats": {"key_points": len(key_points), "exam_focus": 1},
            },
            "guides": guides,
        }
        (base / "lesson.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        written.append(data)

    # 课程级「期末总复习」用真实渲染器生成，不是手写的假文件，
    # 这样演示看到的表格、下载按钮和用户实际用到的完全一致。
    for course in courses:
        mine = [l for l in written if l["course_id"] == course["id"]]
        if not mine:
            continue
        out_dir = target / "courses" / course["id"]
        try:
            files = summarize.build_summary(course, mine, out_dir)
        except summarize.SummarizeError as exc:      # 空课程本来就汇不出来
            print(f"  跳过 {course['name']} 的总复习：{exc}")
            continue
        course["summary"] = {
            "files": files,
            "lessons": len(mine),
            "generated": now - 3 * 3600,
        }

    (target / "courses.json").write_text(
        json.dumps({"courses": courses}, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    print(target)
    return target


def main() -> None:
    ap = argparse.ArgumentParser(description="生成「不听课」演示数据")
    ap.add_argument("--dir", default=str(Path(__file__).parent / "_demo_data"),
                    help="数据目录，会被清空后重建")
    args = ap.parse_args()
    seed(Path(args.dir))


if __name__ == "__main__":
    main()
