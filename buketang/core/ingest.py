"""讲义解析：把各种格式统一成「带页码的文本 + 可按页渲染的图」。

统一策略：
- PDF 直接用 PyMuPDF 提文本
- PPT / PPTX / DOC / DOCX 先用 Office COM 转成 PDF，再走 PDF 流程
  这样文本页码和截图页码天然一致，不会出现「文字说第 12 页、图却是第 9 页」
- MD / TXT 按长度切伪页

Office 不可用时降级为纯文本提取（无截图），不会让整个流程失败。
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

try:
    import pymupdf  # PyMuPDF 2.x
except ImportError:      # 兼容旧版包名
    import fitz as pymupdf  # type: ignore

PDF_EXT = {".pdf"}
PPT_EXT = {".ppt", ".pptx"}
DOC_EXT = {".doc", ".docx"}
TEXT_EXT = {".md", ".markdown", ".txt"}
SUPPORTED = PDF_EXT | PPT_EXT | DOC_EXT | TEXT_EXT


class IngestError(RuntimeError):
    pass


# ---------------- Office -> PDF ----------------

def _abs(p: str | Path) -> str:
    return str(Path(p).resolve())


def office_to_pdf(src: str | Path, dst: str | Path) -> bool:
    """用本机 Office 把 PPT/DOC 转成 PDF。失败返回 False，由调用方降级。"""
    if os.name != "nt":
        return False
    try:
        import win32com.client as win32  # noqa: WPS433
    except ImportError:
        return False

    src_abs, dst_abs = _abs(src), _abs(dst)
    ext = Path(src).suffix.lower()

    try:
        if ext in PPT_EXT:
            app = win32.Dispatch("PowerPoint.Application")
            try:
                pres = None
                for with_window in (False, True):   # 有些版本不接受 WithWindow=False
                    try:
                        pres = app.Presentations.Open(
                            src_abs, ReadOnly=True, Untitled=False, WithWindow=with_window
                        )
                        break
                    except Exception:  # noqa: BLE001
                        continue
                if pres is None:
                    return False
                pres.SaveAs(dst_abs, 32)            # ppSaveAsPDF
                pres.Close()
            finally:
                app.Quit()

        elif ext in DOC_EXT:
            app = win32.Dispatch("Word.Application")
            try:
                app.Visible = False
                app.DisplayAlerts = 0
                doc = app.Documents.Open(src_abs, ReadOnly=True, AddToRecentFiles=False)
                doc.SaveAs(dst_abs, FileFormat=17)  # wdFormatPDF
                doc.Close(False)
            finally:
                app.Quit()
        else:
            return False
    except Exception:  # noqa: BLE001  COM 失败不致命，走降级
        return False

    return Path(dst).exists()


# ---------------- 文本提取 ----------------

def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_pdf(path: str | Path) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    with pymupdf.open(str(path)) as doc:
        for i, page in enumerate(doc, start=1):
            try:
                text = page.get_text("text") or ""
            except Exception:  # noqa: BLE001
                text = ""
            pages.append({"index": i, "text": _clean(text)})
    return pages


def parse_pptx_text(path: str | Path) -> list[dict[str, Any]]:
    """COM 不可用时的降级方案：只能拿文本，没有截图。"""
    try:
        from pptx import Presentation
    except ImportError as exc:
        raise IngestError("未安装 python-pptx，无法解析 PPT") from exc

    pages: list[dict[str, Any]] = []
    prs = Presentation(str(path))
    for i, slide in enumerate(prs.slides, start=1):
        chunks: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = shape.text_frame.text.strip()
                if t:
                    chunks.append(t)
            if getattr(shape, "has_table", False):
                try:
                    for row in shape.table.rows:
                        cells = [c.text.strip() for c in row.cells]
                        if any(cells):
                            chunks.append(" | ".join(cells))
                except Exception:  # noqa: BLE001
                    pass
        pages.append({"index": i, "text": _clean("\n".join(chunks))})
    return pages


def parse_docx_text(path: str | Path) -> list[dict[str, Any]]:
    try:
        from docx import Document
    except ImportError as exc:
        raise IngestError("未安装 python-docx，无法解析 Word") from exc

    doc = Document(str(path))
    chunks: list[str] = []
    for para in doc.paragraphs:
        t = para.text.strip()
        if t:
            chunks.append(t)
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                chunks.append(" | ".join(cells))

    return _paginate_text("\n".join(chunks), page_chars=1800)


def parse_text_file(path: str | Path) -> list[dict[str, Any]]:
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    return _paginate_text(raw, page_chars=1800)


def _paginate_text(text: str, page_chars: int = 1800) -> list[dict[str, Any]]:
    """把长文本按段落聚合成伪页，尽量在标题处断开。"""
    text = _clean(text)
    if not text:
        return [{"index": 1, "text": ""}]

    blocks = re.split(r"\n\s*\n", text)
    pages: list[dict[str, Any]] = []
    buf: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buf, size
        if buf:
            pages.append({"index": len(pages) + 1, "text": "\n\n".join(buf).strip()})
            buf, size = [], 0

    for block in blocks:
        if size and size + len(block) > page_chars:
            flush()
        buf.append(block)
        size += len(block) + 2

    flush()
    return pages or [{"index": 1, "text": ""}]


# ---------------- 页面渲染 ----------------

def render_pages(pdf_path: str | Path, page_numbers: list[int],
                 out_dir: str | Path, dpi: int = 130, quality: int = 82
                 ) -> dict[int, str]:
    """按需渲染指定页码为 JPEG，返回 {页码: 图片路径}。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict[int, str] = {}

    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception:  # noqa: BLE001
        return result

    try:
        for num in sorted(set(p for p in page_numbers if p and p > 0)):
            if num > doc.page_count:
                continue
            out_path = out_dir / f"p{num:03d}.jpg"
            if out_path.exists():
                result[num] = str(out_path)
                continue
            try:
                page = doc[num - 1]
                pix = page.get_pixmap(dpi=dpi)
                try:
                    data = pix.tobytes("jpeg", jpg_quality=quality)
                except TypeError:
                    data = pix.tobytes("png")
                out_path.write_bytes(data)
                result[num] = str(out_path)
            except Exception:  # noqa: BLE001
                continue
    finally:
        doc.close()

    return result


# ---------------- 入口 ----------------

def ingest(src: str | Path, work_dir: str | Path) -> dict[str, Any]:
    """解析一个讲义文件，返回它的结构化描述。"""
    src = Path(src)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    ext = src.suffix.lower()
    if ext not in SUPPORTED:
        raise IngestError(f"不支持的文件格式：{ext}")

    warnings: list[str] = []
    pdf_path: str | None = None
    pages: list[dict[str, Any]] = []

    if ext in PDF_EXT:
        pdf_path = str(src)
        pages = parse_pdf(src)

    elif ext in PPT_EXT | DOC_EXT:
        converted = work_dir / "source.pdf"
        if office_to_pdf(src, converted):
            pdf_path = str(converted)
            pages = parse_pdf(converted)
        else:
            warnings.append("未能调用本机 Office 转换，已降级为纯文本解析（无讲义截图）")
            pages = parse_pptx_text(src) if ext in PPT_EXT else parse_docx_text(src)

    else:  # md / txt
        pages = parse_text_file(src)

    total_chars = sum(len(p["text"]) for p in pages)
    blank_pages = [p["index"] for p in pages if len(p["text"]) < 20]

    if pages and len(blank_pages) == len(pages):
        warnings.append(
            "这份讲义几乎提取不到文字，可能是扫描件/纯图片版式。"
            "截图仍会保留，但内容分析只能依赖课堂录音。"
        )
    elif blank_pages:
        warnings.append(f"有 {len(blank_pages)} 页几乎无文字（可能是封面或纯图页）")

    return {
        "filename": src.name,
        "ext": ext,
        "path": str(src),
        "pdf_path": pdf_path,
        "page_count": len(pages),
        "chars": total_chars,
        "pages": pages,
        "can_shot": bool(pdf_path),
        "blank_pages": blank_pages,
        "warnings": warnings,
    }


def build_material_text(pages: list[dict[str, Any]], max_chars: int = 0) -> str:
    """把分页文本拼成带页码标记的字符串，供 LLM 引用页码。"""
    parts: list[str] = []
    used = 0
    for p in pages:
        text = p["text"].strip()
        if not text:
            continue
        block = f"【第{p['index']}页】\n{text}"
        if max_chars and used + len(block) > max_chars:
            parts.append(f"……（后续 {len(pages) - p['index'] + 1} 页因篇幅省略）")
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)
