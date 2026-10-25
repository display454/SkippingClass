"""课程级汇总：把一门课所有课堂的考点合并成一份期末总复习。

为什么需要单独一层：
- 单堂课分析时不知道后面还会讲什么，跨课去重无从谈起
- 期末要的是「整门课的骨架」，不是 16 份彼此独立的清单

合并策略刻意不用第二次 LLM 调用，全部程序化完成：
1. 按标题字符二元组相似度跨课去重（"反向传播" 和 "反向传播算法" 会合并）
2. 排序主键是「出现在几堂课里」——讲得越多越可能是这门课的主干，
   这个信号比模型给的 importance 更客观，也是跨课汇总真正新增的价值
3. 同一考点保留最长的那份讲解，重要度取最大值

好处是零成本、秒级生成、结果可复现；代价是归类的语义不如人，
所以正文里同时保留「按课堂溯源」，需要原话时可以回到单堂课去看。
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from docx import Document
from docx.shared import Pt, RGBColor

from .compose import _heading, _kv_line, _set_font, _level_text, stars

INK = RGBColor(0x2B, 0x24, 0x1C)
INK_SOFT = RGBColor(0x6B, 0x5D, 0x4F)
ACCENT = RGBColor(0x8A, 0x6A, 0x45)
MUTED = RGBColor(0x99, 0x8E, 0x80)


class SummarizeError(RuntimeError):
    pass


# ---------------- 去重与合并 ----------------

_PUNCT = re.compile(r"[\s，。、,.\-—_()（）「」『』【】\[\]:：;；!！?？\"'“”‘’·…]")


def _norm(text: str) -> str:
    return _PUNCT.sub("", (text or "").lower())


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def similarity(a: str, b: str) -> float:
    """字符二元组 Jaccard。中文短标题上比编辑距离更稳。"""
    x, y = _bigrams(_norm(a)), _bigrams(_norm(b))
    if not x or not y:
        return 0.0
    inter = len(x & y)
    return inter / len(x | y) if inter else 0.0


def collect(lessons: list[dict], threshold: float = 0.62) -> list[dict]:
    merged: list[dict] = []

    for lesson in lessons:
        analysis = lesson.get("analysis") or {}
        name = lesson.get("name") or "未命名课堂"
        for kp in analysis.get("key_points") or []:
            title = (kp.get("title") or "").strip()
            if not title:
                continue

            hit = None
            for item in merged:
                if similarity(title, item["title"]) >= threshold:
                    hit = item
                    break

            if hit is None:
                merged.append({
                    "title": title,
                    "detail": (kp.get("detail") or "").strip(),
                    "type": kp.get("type") or "要点",
                    "importance": int(kp.get("importance") or 3),
                    "exam_hint": (kp.get("exam_hint") or "").strip(),
                    "sources": [name],
                })
                continue

            if name not in hit["sources"]:
                hit["sources"].append(name)
            hit["importance"] = max(hit["importance"], int(kp.get("importance") or 3))
            detail = (kp.get("detail") or "").strip()
            if len(detail) > len(hit["detail"]):
                hit["detail"] = detail
            if not hit["exam_hint"] and kp.get("exam_hint"):
                hit["exam_hint"] = (kp["exam_hint"] or "").strip()
            if hit["type"] == "要点" and kp.get("type"):
                hit["type"] = kp["type"]

    for item in merged:
        item["count"] = len(item["sources"])

    merged.sort(key=lambda x: (-x["count"], -x["importance"], -len(x["detail"])))
    return merged


def _top(items: list[dict], n: int) -> list[dict]:
    return items[:n]


def spine_threshold(lesson_count: int) -> int:
    """判断「重复出现几次算主干」。

    只有三四堂课时要求出现三次会把主干章节清空，所以小课程放宽到两次。
    """
    return 3 if lesson_count >= 5 else 2


# ---------------- Markdown ----------------

def render_summary_md(course: dict, lessons: list[dict], points: list[dict],
                      options: dict | None = None) -> str:
    options = options or {}
    top_n = int(options.get("summary_top", 15))
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    raw_count = sum(len((l.get("analysis") or {}).get("key_points") or []) for l in lessons)
    name = course.get("name") or "未命名课程"

    lines = [
        "---",
        f'title: "{name} · 期末总复习"',
        f'course: "{name}"',
        f'term: "{course.get("term", "")}"',
        f"lessons: {len(lessons)}",
        f"points: {len(points)}",
        f'generated: "{stamp}"',
        "---",
        "",
        f"# {name} · 期末总复习",
        "",
        f"> 覆盖 {len(lessons)} 堂课、{raw_count} 个原始考点，合并重复后 {len(points)} 个。"
        f"　排序依据是「出现在几堂课里」——讲得越多，越可能是这门课真正的主干。",
        "",
    ]

    lessons_list = "、".join(f"《{l.get('name', '')}》" for l in lessons)
    lines += ["**已覆盖**：" + lessons_list, ""]

    # 一、最该背的
    must = [p for p in points if p["importance"] >= 4][:top_n]
    lines += [f"## 一、最该背的 {len(must)} 条", ""]
    if not must:
        lines += ["_没有识别出高重要度考点。_", ""]
    for i, p in enumerate(must, start=1):
        lines += _md_point(p, i)
    lines += [""]

    # 二、跨课主干
    need = spine_threshold(len(lessons))
    spine = [p for p in points if p["count"] >= need and p not in must]
    if spine:
        lines += [f"## 二、贯穿整门课的主干（在 {need} 堂以上重复出现）", ""]
        for i, p in enumerate(spine, start=1):
            sources = "、".join(p["sources"][:5])
            lines += [_md_point(p, i, hint=False)]
            lines += [f"*出现于 {p['count']} 堂课：{sources}*", ""]
        lines.append("")

    # 三、完整清单
    lines += ["## 三、完整考点清单", ""]
    lines += ["| # | 考点 | 重要度 | 出现课次 | 类型 |", "|---|------|--------|----------|------|"]
    for i, p in enumerate(points, start=1):
        title = p["title"].replace("|", "／")
        lines.append(f"| {i} | {title} | {_level_text(p['importance'])} | {p['count']} | {p['type']} |")
    lines += [""]

    # 四、考点预测
    focus: list[dict] = []
    seen: set[str] = set()
    for lesson in lessons:
        for ef in (lesson.get("analysis") or {}).get("exam_focus") or []:
            key = _norm(ef.get("item", ""))[:24]
            if not key or key in seen:
                continue
            seen.add(key)
            focus.append({**ef, "from": lesson.get("name", "")})
    focus.sort(key=lambda x: -int(x.get("importance") or 3))
    if focus:
        lines += ["## 四、可能怎么考", ""]
        for i, ef in enumerate(focus[:24], start=1):
            lines.append(f"{i}. **{ef.get('item', '')}**　`{_level_text(ef.get('importance', 4))}`")
            if ef.get("how"):
                lines += [f"   - 怎么考：{ef['how']}"]
            if ef.get("why"):
                lines += [f"   - 为什么：{ef['why']}"]
            lines += [f"   - 出自《{ef['from']}》"]
        lines.append("")

    # 五、易错点
    pitfalls: list[dict] = []
    seen = set()
    for lesson in lessons:
        for pf in (lesson.get("analysis") or {}).get("pitfalls") or []:
            text = (pf.get("point") or "").strip()
            key = _norm(text)[:24]
            if not key or key in seen:
                continue
            seen.add(key)
            pitfalls.append({**pf, "from": lesson.get("name", "")})
    if pitfalls:
        lines += ["## 五、容易错的地方", ""]
        for pf in pitfalls[:30]:
            lines += [f"- **{pf['point']}**　_（《{pf['from']}》）_"]
        lines.append("")

    # 六、老师强调
    quotes: list[dict] = []
    seen = set()
    for lesson in lessons:
        for e in (lesson.get("analysis") or {}).get("emphasis") or []:
            quote = (e.get("quote") or "").strip()
            key = _norm(quote)[:24]
            if not key or key in seen:
                continue
            seen.add(key)
            quotes.append({**e, "from": lesson.get("name", "")})
    if quotes:
        lines += ["## 六、老师明确强调过的话", ""]
        for q in quotes[:24]:
            lines.append(f"> {q['quote']}")
            meta = [f"《{q['from']}》"]
            if q.get("timestamp"):
                meta.insert(0, f"录音 {q['timestamp']}")
            if q.get("note"):
                meta.append(q["note"])
            lines += ["", f"　　— {' · '.join(meta)}", ""]

    # 七、分课索引
    lines += ["## 七、分课索引", ""]
    for lesson in lessons:
        analysis = lesson.get("analysis") or {}
        titles = [k.get("title", "") for k in (analysis.get("key_points") or [])][:6]
        topic = analysis.get("topic") or lesson.get("name", "")
        lines.append(f"- **《{lesson.get('name', '')}》**　{topic}")
        if titles:
            lines.append(f"  - 主要考点：{'、'.join(titles)}")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _md_point(p: dict, idx: int, hint: bool = True) -> list[str]:
    lines = [f"### {idx}. {p['title']}　`{stars(p['importance'])}`", ""]
    if p.get("detail"):
        lines += [p["detail"], ""]
    meta = [p["type"]]
    if p["count"] > 1:
        meta.insert(0, f"出现于 {p['count']} 堂课")
    lines += [f"*{' · '.join(meta)}*", ""]
    if hint and p.get("exam_hint"):
        lines += [f"- **可能考法**：{p['exam_hint']}", ""]
    return lines


# ---------------- Word ----------------

def render_summary_docx(path: str | Path, course: dict, lessons: list[dict],
                        points: list[dict], options: dict | None = None) -> Path:
    options = options or {}
    top_n = int(options.get("summary_top", 15))
    name = course.get("name") or "未命名课程"
    raw_count = sum(len((l.get("analysis") or {}).get("key_points") or []) for l in lessons)

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.size = Pt(10.5)
    _set_font(normal)
    normal.paragraph_format.line_spacing = 1.55
    normal.paragraph_format.space_after = Pt(6)

    for section in doc.sections:
        section.top_margin = section.bottom_margin = Pt(54)
        section.left_margin = section.right_margin = Pt(60)

    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(2)
    run = p.add_run(f"{name}")
    run.bold = True
    run.font.size = Pt(22)
    run.font.color.rgb = INK

    sub = doc.add_paragraph()
    sub.paragraph_format.space_after = Pt(14)
    run = sub.add_run("期末总复习")
    run.font.size = Pt(12)
    run.font.color.rgb = ACCENT

    _kv_line(doc,
             f"覆盖 {len(lessons)} 堂课 · {raw_count} 个原始考点 · 合并后 {len(points)} 个",
             INK_SOFT, 9.5)
    if course.get("term"):
        _kv_line(doc, f"学期：{course['term']}", MUTED, 9)
    _kv_line(doc, f"生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}", MUTED, 9)

    _heading(doc, "已覆盖的课堂", 13, INK, space_before=18)
    for lesson in lessons:
        _kv_line(doc, f"· {lesson.get('name', '')}", INK_SOFT, 10)

    must = [x for x in points if x["importance"] >= 4][:top_n]
    _heading(doc, f"一、最该背的 {len(must)} 条", 15, ACCENT, space_before=20)
    if not must:
        _kv_line(doc, "没有识别出高重要度考点。", INK_SOFT, 10)
    for i, item in enumerate(must, start=1):
        _docx_point(doc, item, i)

    need = spine_threshold(len(lessons))
    spine = [x for x in points if x["count"] >= need and x not in must]
    if spine:
        _heading(doc, "二、贯穿整门课的主干", 15, ACCENT, space_before=20)
        _kv_line(doc, f"在 {need} 堂以上重复出现，通常是这门课真正的主线。", MUTED, 9)
        for i, item in enumerate(spine, start=1):
            _docx_point(doc, item, i, hint=False)

    _heading(doc, "三、完整考点清单", 15, ACCENT, space_before=20)
    table = doc.add_table(rows=1, cols=4)
    table.style = "Light Grid Accent 1"
    for cell, text in zip(table.rows[0].cells, ("考点", "重要度", "课次", "类型")):
        cell.text = text
    for item in points:
        row = table.add_row().cells
        row[0].text = item["title"]
        row[1].text = _level_text(item["importance"])
        row[2].text = str(item["count"])
        row[3].text = item["type"]

    focus: list[dict] = []
    seen: set[str] = set()
    for lesson in lessons:
        for ef in (lesson.get("analysis") or {}).get("exam_focus") or []:
            key = _norm(ef.get("item", ""))[:24]
            if key and key not in seen:
                seen.add(key)
                focus.append({**ef, "from": lesson.get("name", "")})
    focus.sort(key=lambda x: -int(x.get("importance") or 3))
    if focus:
        _heading(doc, "四、可能怎么考", 15, ACCENT, space_before=20)
        for i, ef in enumerate(focus[:24], start=1):
            _kv_line(doc, f"{i}. {ef.get('item', '')}（{_level_text(ef.get('importance', 4))}）", INK, 10)
            if ef.get("how"):
                _kv_line(doc, f"    怎么考：{ef['how']}", INK_SOFT, 9.5)
            if ef.get("why"):
                _kv_line(doc, f"    为什么：{ef['why']}", INK_SOFT, 9.5)

    pitfalls: list[dict] = []
    seen = set()
    for lesson in lessons:
        for pf in (lesson.get("analysis") or {}).get("pitfalls") or []:
            text = (pf.get("point") or "").strip()
            key = _norm(text)[:24]
            if key and key not in seen:
                seen.add(key)
                pitfalls.append({**pf, "from": lesson.get("name", "")})
    if pitfalls:
        _heading(doc, "五、容易错的地方", 15, ACCENT, space_before=20)
        for pf in pitfalls[:30]:
            _kv_line(doc, f"· {pf['point']}　（《{pf['from']}》）", INK_SOFT, 10)

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    return out


def _docx_point(doc: Document, item: dict, idx: int, hint: bool = True) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    p.paragraph_format.space_after = Pt(3)
    run = p.add_run(f"{idx}. {item['title']}")
    run.bold = True
    run.font.size = Pt(11.5)
    run.font.color.rgb = INK
    run = p.add_run(f"　{stars(item['importance'])}")
    run.font.size = Pt(8)
    run.font.color.rgb = ACCENT

    if item.get("detail"):
        _kv_line(doc, item["detail"], INK_SOFT, 10)

    meta = [item["type"]]
    if item["count"] > 1:
        meta.insert(0, f"出现于 {item['count']} 堂课")
    _kv_line(doc, " · ".join(meta), MUTED, 8.5)

    if hint and item.get("exam_hint"):
        _kv_line(doc, f"可能考法：{item['exam_hint']}", ACCENT, 9.5)


# ---------------- 入口 ----------------

def build_summary(course: dict, lessons: list[dict], out_dir: Path,
                  options: dict | None = None) -> dict[str, str]:
    done = [l for l in lessons
            if l.get("status") == "done" and (l.get("analysis") or {}).get("key_points")]
    if not done:
        raise SummarizeError("这门课下还没有分析完成的课堂，先去把课跑完")

    points = collect(done)
    if not points:
        raise SummarizeError("这些课堂没有提取到考点，无法汇总")

    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_summary_md(course, done, points, options)
    md_path = out_dir / "期末总复习.md"
    md_path.write_text(md, encoding="utf-8")

    docx_path = render_summary_docx(out_dir / "期末总复习.docx", course, done, points, options)

    return {"summary_md": str(md_path), "summary_docx": str(docx_path)}
