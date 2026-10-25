"""结果渲染：Markdown / 纯文本 / Word。

三个产物共享同一份结构化数据，保证内容一致：
- 文字稿：把碎句合并成可读段落
- 时间轴：保留原始分句与时间戳，便于对轴校对
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from shared.asr import Segment

SENTENCE_END = tuple("。！？!?…；;")
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')


def safe_filename(name: str, fallback: str = "transcript") -> str:
    stem = Path(name).stem if name else fallback
    stem = _ILLEGAL.sub("_", stem).strip(" .")
    return (stem or fallback)[:80]


def clock(seconds: float) -> str:
    """秒 -> MM:SS 或 HH:MM:SS"""
    if seconds is None or seconds < 0:
        seconds = 0
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def merge_paragraphs(
    segments: list[Segment],
    max_chars: int = 150,
    gap: float = 1.8,
) -> list[tuple[float, str]]:
    """把碎句合并成自然段。"""
    paragraphs: list[tuple[float, str]] = []
    buf: list[str] = []
    buf_start = 0.0
    buf_speaker: str | None = None
    prev_end = 0.0

    def flush() -> None:
        nonlocal buf
        if buf:
            paragraphs.append((buf_start, "".join(buf).strip()))
            buf = []

    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue

        if not buf:
            buf_start = seg.start
            buf_speaker = seg.speaker
        elif seg.speaker != buf_speaker:
            flush()
            buf_start = seg.start
            buf_speaker = seg.speaker

        current_len = len("".join(buf))
        long_gap = buf and (seg.start - prev_end) > gap and current_len > 30
        too_long = current_len >= max_chars

        if long_gap or (too_long and "".join(buf).rstrip().endswith(SENTENCE_END)):
            flush()
            buf_start = seg.start

        buf.append(text)
        prev_end = seg.end

        if len("".join(buf)) >= max_chars * 1.6:
            flush()

    flush()
    return paragraphs


def _front_matter(meta: dict[str, Any]) -> str:
    def esc(v: Any) -> str:
        return str(v).replace('"', '\\"')

    lines = [
        "---",
        f'title: "{esc(meta.get("title", ""))}"',
        f'source: "{esc(meta.get("source", ""))}"',
        f'duration: "{esc(meta.get("duration_text", ""))}"',
        f'sentences: {meta.get("segment_count", 0)}',
        f'engine: "{esc(meta.get("engine", ""))}"',
        f'created: "{esc(meta.get("created", ""))}"',
        "---",
        "",
    ]
    return "\n".join(lines)


def render_markdown(
    meta: dict[str, Any],
    paragraphs: list[tuple[float, str]],
    segments: list[Segment],
    include_timeline: bool = True,
) -> str:
    title = meta.get("title") or "转录结果"
    parts = [_front_matter(meta), f"# {title}", ""]

    info = f"> 时长 {meta.get('duration_text', '--:--')} · {meta.get('engine', '')}"
    if meta.get("segment_count"):
        info += f" · 共 {meta['segment_count']} 句"
    if meta.get("note"):
        info += f"\n>\n> {meta['note']}"
    parts.append(info)
    parts.append("")

    parts.append("## 文字稿")
    parts.append("")
    if paragraphs:
        for _, text in paragraphs:
            parts.append(text)
            parts.append("")
    else:
        parts.append("_未识别到有效语音内容。_")
        parts.append("")

    if include_timeline and segments:
        parts.append("## 时间轴")
        parts.append("")
        for seg in segments:
            prefix = f"[{clock(seg.start)}]"
            if seg.speaker:
                prefix = f"[{clock(seg.start)}] 说话人 {seg.speaker}："
            parts.append(f"- `{prefix}` {seg.text}")
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def render_txt(meta: dict[str, Any], paragraphs: list[tuple[float, str]],
               segments: list[Segment], include_timeline: bool = True) -> str:
    parts = [
        meta.get("title", "转录结果"),
        f"来源：{meta.get('source', '')}",
        f"时长：{meta.get('duration_text', '--:--')}",
        f"引擎：{meta.get('engine', '')}",
        f"生成：{meta.get('created', '')}",
        "",
        "=" * 48,
        "文字稿",
        "=" * 48,
        "",
    ]
    if paragraphs:
        parts.extend(text for _, text in paragraphs)
    else:
        parts.append("（未识别到有效语音内容）")

    if include_timeline and segments:
        parts.extend(["", "=" * 48, "时间轴", "=" * 48, ""])
        for seg in segments:
            speaker = f"说话人 {seg.speaker}：" if seg.speaker else ""
            parts.append(f"[{clock(seg.start)}] {speaker}{seg.text}")

    return "\n".join(parts).rstrip() + "\n"


# ---------------- Word ----------------

def _set_east_asian_font(target, font_name: str = "Microsoft YaHei") -> None:
    """给样式或 run 同时设置中西文字体，否则中文会回退成宋体。"""
    if hasattr(target, "font"):
        target.font.name = font_name
    element = target.element if hasattr(target, "element") else target._element
    rpr = element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    for attr in ("w:eastAsia", "w:ascii", "w:hAnsi"):
        rfonts.set(qn(attr), font_name)


def render_docx(
    path: str | Path,
    meta: dict[str, Any],
    paragraphs: list[tuple[float, str]],
    segments: list[Segment],
    include_timeline: bool = True,
) -> Path:
    doc = Document()

    normal = doc.styles["Normal"]
    normal.font.size = Pt(10.5)
    _set_east_asian_font(normal)
    normal.paragraph_format.line_spacing = 1.6
    normal.paragraph_format.space_after = Pt(6)

    for section in doc.sections:
        section.top_margin = section.bottom_margin = Pt(56)
        section.left_margin = section.right_margin = Pt(64)

    # 标题
    heading = doc.add_paragraph()
    heading.paragraph_format.space_after = Pt(2)
    run = heading.add_run(meta.get("title") or "转录结果")
    run.bold = True
    run.font.size = Pt(19)
    run.font.color.rgb = RGBColor(0x1F, 0x24, 0x30)

    # 元信息
    info_parts = [
        f"时长 {meta.get('duration_text', '--:--')}",
        meta.get("engine", ""),
    ]
    if meta.get("segment_count"):
        info_parts.append(f"共 {meta['segment_count']} 句")
    info_parts.append(f"生成于 {meta.get('created', '')}")

    info = doc.add_paragraph()
    info.paragraph_format.space_after = Pt(14)
    info_run = info.add_run(" · ".join(p for p in info_parts if p))
    info_run.font.size = Pt(8.5)
    info_run.font.color.rgb = RGBColor(0x8A, 0x90, 0x9C)

    # 文字稿
    h = doc.add_paragraph()
    h.paragraph_format.space_before = Pt(6)
    h.paragraph_format.space_after = Pt(6)
    h_run = h.add_run("文字稿")
    h_run.bold = True
    h_run.font.size = Pt(13)
    h_run.font.color.rgb = RGBColor(0x2C, 0x3A, 0x5E)

    if paragraphs:
        for _, text in paragraphs:
            p = doc.add_paragraph(text)
            p.paragraph_format.first_line_indent = Pt(21)  # 首行缩进两字
    else:
        p = doc.add_paragraph("（未识别到有效语音内容）")
        p.runs[0].font.color.rgb = RGBColor(0x8A, 0x90, 0x9C)

    # 时间轴
    if include_timeline and segments:
        h2 = doc.add_paragraph()
        h2.paragraph_format.space_before = Pt(18)
        h2.paragraph_format.space_after = Pt(6)
        h2_run = h2.add_run("时间轴")
        h2_run.bold = True
        h2_run.font.size = Pt(13)
        h2_run.font.color.rgb = RGBColor(0x2C, 0x3A, 0x5E)

        for seg in segments:
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(3)
            p.paragraph_format.line_spacing = 1.4

            stamp = p.add_run(f"[{clock(seg.start)}] ")
            stamp.font.size = Pt(8.5)
            stamp.font.color.rgb = RGBColor(0xA0, 0xA6, 0xB2)
            stamp.font.name = "Consolas"

            if seg.speaker:
                sp = p.add_run(f"说话人 {seg.speaker}：")
                sp.font.size = Pt(9)
                sp.font.color.rgb = RGBColor(0x4F, 0x46, 0xE5)

            body = p.add_run(seg.text)
            body.font.size = Pt(10)

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    return out


def build_all(
    job_id: str,
    source_name: str,
    out_dir: Path,
    duration: float,
    paragraphs: list[tuple[float, str]],
    segments: list[Segment],
    formats: list[str],
    engine: str,
    include_timeline: bool = True,
    note: str = "",
) -> dict[str, str]:
    """按配置生成所有格式，返回 {format: 绝对路径}。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_filename(source_name, fallback=f"transcript_{job_id[:6]}")

    meta = {
        "title": Path(source_name).stem or source_name,
        "source": source_name,
        "duration": duration,
        "duration_text": clock(duration),
        "segment_count": len(segments),
        "engine": engine,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": note,
    }

    outputs: dict[str, str] = {}

    if "md" in formats:
        p = out_dir / f"{stem}.md"
        p.write_text(
            render_markdown(meta, paragraphs, segments, include_timeline),
            encoding="utf-8",
        )
        outputs["md"] = str(p)

    if "txt" in formats:
        p = out_dir / f"{stem}.txt"
        p.write_text(
            render_txt(meta, paragraphs, segments, include_timeline),
            encoding="utf-8",
        )
        outputs["txt"] = str(p)

    if "docx" in formats:
        p = out_dir / f"{stem}.docx"
        render_docx(p, meta, paragraphs, segments, include_timeline)
        outputs["docx"] = str(p)

    return outputs
