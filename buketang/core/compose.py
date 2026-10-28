"""复习指导渲染：极限型 / 详细型，输出 Markdown + Word。

两种版本共用同一份分析结果，只是取舍不同：
- 极限型：只留高重要度考点，去掉截图和拓展，考前 10 分钟能看完
- 详细型：全量展开，每条关键点配讲义原页截图，附易错点、老师强调与拓展
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
import re
import shutil
from shared.document import write_docx

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

STAR_FULL = "★"


def stars(n: int) -> str:
    n = max(1, min(5, int(n or 3)))
    return STAR_FULL * n + "☆" * (5 - n)


def _level_text(n: int) -> str:
    return {5: "核心", 4: "重点", 3: "掌握", 2: "理解", 1: "了解"}.get(int(n), "掌握")


# ---------------- 选取 ----------------

def pick_extreme(analysis: dict, max_points: int = 12) -> list[dict]:
    kps = analysis.get("key_points") or []
    strong = [k for k in kps if int(k.get("importance", 3)) >= 4]
    chosen = strong[:max_points]
    if len(chosen) < min(6, max_points):
        for k in kps:
            if k not in chosen:
                chosen.append(k)
            if len(chosen) >= max_points:
                break
    return chosen


# ---------------- Markdown ----------------

def _md_header(title: str, lesson_name: str, mode: str, analysis: dict) -> list[str]:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    badge = "极限版 · 核心考点" if mode == "extreme" else "详细版 · 全量整理"
    lines = [
        "---",
        f'title: "{lesson_name} · {badge}"',
        f'generated: "{stamp}"',
        f"key_points: {len(analysis.get('key_points') or [])}",
        "---",
        "",
        f"# {title}",
        "",
        f"> {badge} · 生成于 {stamp}",
        "",
    ]
    if analysis.get("summary"):
        lines += ["## 一句话说清这节课", "", analysis["summary"], ""]
    return lines


def _md_point(kp: dict, idx: int, with_hint: bool = True) -> list[str]:
    lines = [f"### {idx}. {kp['title']}　`{stars(kp.get('importance', 3))}`", ""]
    if kp.get("detail"):
        lines += [kp["detail"], ""]

    meta = []
    if kp.get("pages"):
        meta.append("讲义 " + "、".join(f"[P{p}](source:{p})" for p in kp["pages"]))
    if kp.get("timestamp"):
        meta.append(f"录音 {kp['timestamp']}")
    if kp.get("type"):
        meta.append(kp["type"])
    if meta:
        lines += [f"*{' · '.join(meta)}*", ""]

    if with_hint and kp.get("exam_hint"):
        lines += [f"- **可能考法**：{kp['exam_hint']}"]
    lines.extend(_rich_blocks(kp))
    lines.append("")
    return lines


def _rich_blocks(point: dict) -> list[str]:
    lines = []
    for formula in point.get("formulas", []):
        latex = formula.get("latex", "").strip().strip("$")
        lines += ["", "$$", latex, "$$", ""]
        if formula.get("explanation"):
            lines += [formula["explanation"], ""]
        if formula.get("conditions"):
            lines += ["适用条件：" + formula["conditions"], ""]
        sources = [f"[P{page}](source:{page})" for page in formula.get("pages", [])]
        if formula.get("timestamp"):
            sources.append(formula["timestamp"])
        lines += ["公式依据：" + ("、".join(sources) or "待补充来源") +
                  ("（待核对）" if formula.get("needs_review") else "（请与原材料核对）"), ""]
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")
    for table in point.get("tables", []):
        columns = table.get("columns", [])
        if not columns:
            continue
        lines += [table.get("caption", ""), "", "| " + " | ".join(map(cell, columns)) + " |",
                  "| " + " | ".join(["---"] * len(columns)) + " |"]
        lines += ["| " + " | ".join(map(cell, row)) + " |" for row in table.get("rows", [])]
        lines.append("")
    for image in point.get("images", []):
        caption = str(image.get("caption", "辅助图片")).replace("[", "").replace("]", "").replace("\n", " ")
        name = image.get("cached_name", "")
        target = "assets/external/" + name if re.fullmatch(r"[a-f0-9]{24}\.png", name) else image.get("url", "")
        lines += [f"![{caption}](<{target}>)", "", f"图片来源：{image.get('url', '')}", ""]
    return lines


def render_extreme_md(lesson_name: str, analysis: dict, max_points: int) -> str:
    points = pick_extreme(analysis, max_points)
    title = analysis.get("topic") or lesson_name
    lines = _md_header(title, lesson_name, "extreme", analysis)

    lines += ["## 必背考点", ""]
    if not points:
        lines += ["_本次分析没有提取到明确的考点，请检查讲义或录音内容。_", ""]
    for i, kp in enumerate(points, start=1):
        lines += _md_point(kp, i)

    focus = analysis.get("exam_focus") or []
    if focus:
        lines += ["## 考点预测", ""]
        for ef in focus[:6]:
            line = f"- **{ef['item']}**"
            if ef.get("how"):
                line += f" — {ef['how']}"
            if ef.get("pages"):
                line += f"（P{'、P'.join(str(p) for p in ef['pages'])}）"
            lines.append(line)
        lines.append("")

    emphasis = analysis.get("emphasis") or []
    if emphasis:
        lines += ["## 老师划的重点", ""]
        for e in emphasis[:6]:
            ts = f"`{e['timestamp']}` " if e.get("timestamp") else ""
            lines.append(f"- {ts}{e['quote']}" + ("（AI 概括，原话待核对）" if e.get("verified") is False else ""))
        lines.append("")

    pitfalls = analysis.get("pitfalls") or []
    if pitfalls:
        lines += ["## 别踩这些坑", ""]
        for p in pitfalls[:6]:
            lines.append(f"- {p['point']}")
        lines.append("")

    if analysis.get("memory_sources"):
        lines += ["## 参考的前后课材料", "", "> 以下属于其他课堂，未作为本课教师原话。", ""]
        for source in analysis["memory_sources"]:
            lines += [f"- [{source['lesson_name']}](lesson:{source['lesson_id']})：{source['title']}"]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_detailed_md(lesson_name: str, analysis: dict,
                       shot_rel: dict[int, str]) -> str:
    title = analysis.get("topic") or lesson_name
    lines = _md_header(title, lesson_name, "detailed", analysis)

    chapters = analysis.get("chapters") or []
    if chapters:
        lines += ["## 课程脉络", ""]
        for c in chapters:
            pages = f"（P{'、P'.join(str(p) for p in c['pages'])}）" if c.get("pages") else ""
            lines.append(f"- **{c['title']}**{pages}　{c.get('gist', '')}")
        lines.append("")

    lines += ["## 知识点详解", ""]
    points = analysis.get("key_points") or []
    if not points:
        lines += ["_没有提取到知识点。_", ""]
    inserted = set()
    for i, kp in enumerate(points, start=1):
        lines.append(f"### {i}. {kp['title']}　`{stars(kp.get('importance', 3))}`")
        lines.append("")
        if kp.get("detail"):
            lines += [kp["detail"], ""]
        lines.extend(_rich_blocks(kp))

        meta = []
        if kp.get("pages"):
            meta.append("讲义 " + "、".join(f"[P{p}](source:{p})" for p in kp["pages"]))
        if kp.get("timestamp"):
            meta.append(f"录音 {kp['timestamp']}")
        if kp.get("type"):
            meta.append(kp["type"])
        if meta:
            lines += [f"*{' · '.join(meta)}*", ""]

        if kp.get("exam_hint"):
            lines += [f"- **可能考法**：{kp['exam_hint']}"]
            lines.append("")

        for page in kp.get("illustration_pages", kp.get("pages")) or []:
            rel = shot_rel.get(page)
            if rel and page not in inserted:
                lines += [f"![讲义第 {page} 页]({rel})", ""]
                inserted.add(page)

    focus = analysis.get("exam_focus") or []
    if focus:
        lines += ["## 考点预测", ""]
        for ef in focus:
            lines.append(f"### {ef['item']}　`{stars(ef.get('importance', 4))}`")
            lines.append("")
            if ef.get("why"):
                lines += [f"- **为什么是考点**：{ef['why']}"]
            if ef.get("how"):
                lines += [f"- **可能考法**：{ef['how']}"]
            if ef.get("pages"):
                lines += [f"- **对应讲义**：P{'、P'.join(str(p) for p in ef['pages'])}"]
            lines.append("")

    pitfalls = analysis.get("pitfalls") or []
    if pitfalls:
        lines += ["## 易错点", ""]
        for p in pitfalls:
            pages = f"（P{'、P'.join(str(x) for x in p['pages'])}）" if p.get("pages") else ""
            lines.append(f"- {p['point']}{pages}")
        lines.append("")

    emphasis = analysis.get("emphasis") or []
    if emphasis:
        lines += ["## 老师强调", ""]
        for e in emphasis:
            ts = f"`{e['timestamp']}` " if e.get("timestamp") else ""
            line = f"- {ts}**{e['quote']}**"
            if e.get("verified") is False:
                line += "（AI 概括，原话待核对）"
            if e.get("note"):
                line += f"　—— {e['note']}"
            lines.append(line)
        lines.append("")

    extensions = analysis.get("extensions") or []
    if extensions:
        lines += ["## 拓展补充", "",
                  "> 以下是帮助理解的背景与延伸，不属于课堂原始内容，按需阅读。", ""]
        for x in extensions:
            lines.append(f"### {x['topic']}")
            lines.append("")
            lines.append(x["content"])
            lines.append("")

    if analysis.get("memory_sources"):
        lines += ["## 参考的前后课材料", "", "> 以下属于其他课堂，未作为本课教师原话。", ""]
        for source in analysis["memory_sources"]:
            lines += [f"- [{source['lesson_name']}](lesson:{source['lesson_id']})：{source['title']}"]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------- Word ----------------

def _set_font(target, name: str = "Microsoft YaHei") -> None:
    if hasattr(target, "font"):
        target.font.name = name
    element = target.element if hasattr(target, "element") else target._element
    rpr = element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    for attr in ("w:eastAsia", "w:ascii", "w:hAnsi"):
        rfonts.set(qn(attr), name)


def _heading(doc: Document, text: str, size: int, color: RGBColor,
             space_before: int = 14, space_after: int = 6):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(space_before)
    p.paragraph_format.space_after = Pt(space_after)
    run = p.add_run(text)
    run.bold = True
    run.font.size = Pt(size)
    run.font.color.rgb = color
    return p


def _kv_line(doc: Document, text: str, color: RGBColor, size: float):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(4)
    run = p.add_run(text)
    run.font.size = Pt(size)
    run.font.color.rgb = color
    return p


def _shot_block(doc: Document, img_path: str, page: int, max_width: float = 6.1) -> None:
    try:
        doc.add_picture(str(img_path), width=Inches(max_width))
        pic_par = doc.paragraphs[-1]
        pic_par.alignment = WD_ALIGN_PARAGRAPH.CENTER
        cap = doc.add_paragraph()
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        cap.paragraph_format.space_after = Pt(10)
        run = cap.add_run(f"讲义第 {page} 页")
        run.font.size = Pt(8)
        run.font.color.rgb = RGBColor(0xA0, 0xA6, 0xB2)
    except Exception:  # noqa: BLE001  图片损坏不影响整体
        pass


def render_docx(path: str | Path, lesson_name: str, analysis: dict,
                mode: str, shots: dict[int, str], max_points: int = 12) -> Path:
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.size = Pt(10.5)
    _set_font(normal)
    normal.paragraph_format.line_spacing = 1.55
    normal.paragraph_format.space_after = Pt(6)

    for section in doc.sections:
        section.top_margin = section.bottom_margin = Pt(52)
        section.left_margin = section.right_margin = Pt(58)

    title = analysis.get("topic") or lesson_name
    badge = "极限版" if mode == "extreme" else "详细版"

    # 封面标题
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(2)
    run = p.add_run(f"{title}")
    run.bold = True
    run.font.size = Pt(21)
    run.font.color.rgb = RGBColor(0x1F, 0x24, 0x30)

    sub = doc.add_paragraph()
    sub.paragraph_format.space_after = Pt(16)
    r = sub.add_run(f"{badge} · 复习指导　|　{datetime.now().strftime('%Y-%m-%d')}")
    r.font.size = Pt(9)
    r.font.color.rgb = RGBColor(0x8A, 0x90, 0x9C)

    if analysis.get("summary"):
        _heading(doc, "一句话说清这节课", 13, RGBColor(0x2C, 0x3A, 0x5E), 6, 4)
        para = doc.add_paragraph(analysis["summary"])
        para.paragraph_format.first_line_indent = Pt(21)

    if mode == "extreme":
        points = pick_extreme(analysis, max_points)
        _heading(doc, "必背考点", 14, RGBColor(0x2C, 0x3A, 0x5E))
        for i, kp in enumerate(points, start=1):
            _heading(doc, f"{i}. {kp['title']}", 11.5, RGBColor(0x1F, 0x24, 0x30), 10, 3)
            star_par = doc.add_paragraph()
            star_par.paragraph_format.space_after = Pt(3)
            sr = star_par.add_run(stars(kp.get("importance", 3)))
            sr.font.size = Pt(9)
            sr.font.color.rgb = RGBColor(0xE0, 0x9B, 0x2D)
            if kp.get("detail"):
                para = doc.add_paragraph(kp["detail"])
                para.paragraph_format.first_line_indent = Pt(21)

            meta = []
            if kp.get("pages"):
                meta.append("讲义 " + "、".join(f"[P{p}](source:{p})" for p in kp["pages"]))
            if kp.get("timestamp"):
                meta.append(f"录音 {kp['timestamp']}")
            if meta:
                _kv_line(doc, " · ".join(meta), RGBColor(0xA0, 0xA6, 0xB2), 8)

            if kp.get("exam_hint"):
                ph = doc.add_paragraph()
                ph.paragraph_format.left_indent = Pt(10)
                ph.paragraph_format.space_after = Pt(8)
                a = ph.add_run("可能考法：")
                a.bold = True
                a.font.size = Pt(9)
                a.font.color.rgb = RGBColor(0x4F, 0x46, 0xE5)
                b = ph.add_run(kp["exam_hint"])
                b.font.size = Pt(9.5)

        if analysis.get("exam_focus"):
            _heading(doc, "考点预测", 14, RGBColor(0x2C, 0x3A, 0x5E))
            for ef in analysis["exam_focus"][:6]:
                p = doc.add_paragraph(style="List Bullet")
                r1 = p.add_run(ef["item"])
                r1.bold = True
                r1.font.size = Pt(10)
                if ef.get("how"):
                    r2 = p.add_run(f" —— {ef['how']}")
                    r2.font.size = Pt(9.5)

        if analysis.get("emphasis"):
            _heading(doc, "老师划的重点", 14, RGBColor(0x2C, 0x3A, 0x5E))
            for e in analysis["emphasis"][:6]:
                p = doc.add_paragraph(style="List Bullet")
                ts = f"[{e['timestamp']}] " if e.get("timestamp") else ""
                r1 = p.add_run(ts)
                r1.font.size = Pt(8.5)
                r1.font.color.rgb = RGBColor(0xA0, 0xA6, 0xB2)
                r2 = p.add_run(e["quote"])
                r2.font.size = Pt(9.5)

        if analysis.get("pitfalls"):
            _heading(doc, "别踩这些坑", 14, RGBColor(0x2C, 0x3A, 0x5E))
            for pt in analysis["pitfalls"][:8]:
                p = doc.add_paragraph(style="List Bullet")
                r = p.add_run(pt["point"])
                r.font.size = Pt(9.5)

    else:
        chapters = analysis.get("chapters") or []
        if chapters:
            _heading(doc, "课程脉络", 14, RGBColor(0x2C, 0x3A, 0x5E), 6, 4)
            for c in chapters:
                p = doc.add_paragraph()
                r1 = p.add_run(c["title"])
                r1.bold = True
                r1.font.size = Pt(10.5)
                if c.get("pages"):
                    r2 = p.add_run("  P" + "、P".join(str(x) for x in c["pages"]))
                    r2.font.size = Pt(8)
                    r2.font.color.rgb = RGBColor(0x4F, 0x46, 0xE5)
                if c.get("gist"):
                    r3 = p.add_run(f"　{c['gist']}")
                    r3.font.size = Pt(9.5)
                    r3.font.color.rgb = RGBColor(0x5B, 0x62, 0x73)

        _heading(doc, "知识点详解", 14, RGBColor(0x2C, 0x3A, 0x5E))
        for i, kp in enumerate(analysis.get("key_points") or [], start=1):
            _heading(doc, f"{i}. {kp['title']}", 11.5, RGBColor(0x1F, 0x24, 0x30), 10, 2)
            sp = doc.add_paragraph()
            sp.paragraph_format.space_after = Pt(3)
            sr = sp.add_run(stars(kp.get("importance", 3)))
            sr.font.size = Pt(9)
            sr.font.color.rgb = RGBColor(0xE0, 0x9B, 0x2D)

            if kp.get("detail"):
                para = doc.add_paragraph(kp["detail"])
                para.paragraph_format.first_line_indent = Pt(21)

            meta = []
            if kp.get("pages"):
                meta.append("讲义 " + "、".join(f"[P{p}](source:{p})" for p in kp["pages"]))
            if kp.get("timestamp"):
                meta.append(f"录音 {kp['timestamp']}")
            if meta:
                _kv_line(doc, " · ".join(meta), RGBColor(0xA0, 0xA6, 0xB2), 8)

            if kp.get("exam_hint"):
                ph = doc.add_paragraph()
                ph.paragraph_format.left_indent = Pt(10)
                a = ph.add_run("可能考法：")
                a.bold = True
                a.font.size = Pt(9)
                a.font.color.rgb = RGBColor(0x4F, 0x46, 0xE5)
                b = ph.add_run(kp["exam_hint"])
                b.font.size = Pt(9.5)

            for page in kp.get("pages") or []:
                img = shots.get(page)
                if img and Path(img).exists():
                    _shot_block(doc, img, page)

        if analysis.get("exam_focus"):
            _heading(doc, "考点预测", 14, RGBColor(0x2C, 0x3A, 0x5E))
            for ef in analysis["exam_focus"]:
                _heading(doc, ef["item"], 11, RGBColor(0x1F, 0x24, 0x30), 8, 2)
                for label, key in (("为什么是考点", "why"), ("可能考法", "how")):
                    if ef.get(key):
                        p = doc.add_paragraph()
                        p.paragraph_format.left_indent = Pt(10)
                        a = p.add_run(f"{label}：")
                        a.bold = True
                        a.font.size = Pt(9)
                        a.font.color.rgb = RGBColor(0x4F, 0x46, 0xE5)
                        b = p.add_run(ef[key])
                        b.font.size = Pt(9.5)

        if analysis.get("pitfalls"):
            _heading(doc, "易错点", 14, RGBColor(0x2C, 0x3A, 0x5E))
            for pt in analysis["pitfalls"]:
                p = doc.add_paragraph(style="List Bullet")
                r = p.add_run(pt["point"])
                r.font.size = Pt(9.5)

        if analysis.get("emphasis"):
            _heading(doc, "老师强调", 14, RGBColor(0x2C, 0x3A, 0x5E))
            for e in analysis["emphasis"]:
                p = doc.add_paragraph(style="List Bullet")
                if e.get("timestamp"):
                    ts = p.add_run(f"[{e['timestamp']}] ")
                    ts.font.size = Pt(8.5)
                    ts.font.color.rgb = RGBColor(0xA0, 0xA6, 0xB2)
                r1 = p.add_run(e["quote"])
                r1.bold = True
                r1.font.size = Pt(9.5)
                if e.get("note"):
                    r2 = p.add_run(f"　—— {e['note']}")
                    r2.font.size = Pt(9)
                    r2.font.color.rgb = RGBColor(0x5B, 0x62, 0x73)

        if analysis.get("extensions"):
            _heading(doc, "拓展补充", 14, RGBColor(0x2C, 0x3A, 0x5E))
            note = doc.add_paragraph()
            nr = note.add_run("以下是帮助理解的背景与延伸，不属于课堂原始内容。")
            nr.font.size = Pt(8.5)
            nr.font.color.rgb = RGBColor(0x8A, 0x90, 0x9C)
            for x in analysis["extensions"]:
                _heading(doc, x["topic"], 11, RGBColor(0x1F, 0x24, 0x30), 8, 2)
                para = doc.add_paragraph(x["content"])
                para.paragraph_format.first_line_indent = Pt(21)

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    return out


# ---------------- 总入口 ----------------

def build_guides(lesson_name: str, analysis: dict, out_dir: Path,
                 shots: dict[int, str], options: dict[str, Any]) -> dict[str, str]:
    """生成四种产物：极限版 md/docx、详细版 md/docx。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    max_points = int(options.get("extreme_max_points", 12))
    external = options.get("external_assets_dir")
    for point in analysis.get("key_points", []):
        for image in point.get("images", []):
            name = image.get("cached_name", "")
            if external and re.fullmatch(r"[a-f0-9]{24}\.png", name):
                source = Path(external) / name
                if source.is_file():
                    target = out_dir / "assets" / "external" / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)

    # Markdown 里的图片走相对路径，方便整个目录拷走
    shot_rel: dict[int, str] = {}
    for page, img in shots.items():
        try:
            shot_rel[page] = Path(img).relative_to(out_dir).as_posix()
        except ValueError:
            shot_rel[page] = Path(img).as_posix()

    result: dict[str, str] = {}

    extreme_md = out_dir / "极限版.md"
    extreme_md.write_text(render_extreme_md(lesson_name, analysis, max_points),
                          encoding="utf-8")
    result["extreme_md"] = str(extreme_md)

    detailed_md = out_dir / "详细版.md"
    detailed_md.write_text(render_detailed_md(lesson_name, analysis, shot_rel),
                           encoding="utf-8")
    result["detailed_md"] = str(detailed_md)
    for mode, label in (("extreme", "极限版"), ("detailed", "详细版")):
        try:
            write_docx(Path(result[f"{mode}_md"]).read_text(encoding="utf-8"), out_dir / f"{label}.docx", out_dir)
        except Exception as exc:
            raise RuntimeError(f"{label} Word 导出失败，旧结果已保留，可单独重试导出：{exc}") from exc
        result[f"{mode}_docx"] = str(out_dir / f"{label}.docx")

    return result
