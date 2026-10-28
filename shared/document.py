"""One Markdown document for web/notes and editable Word math and tables."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlsplit

CONVERSION_LOCK = threading.Lock()


def write_docx(markdown: str, destination: Path, assets_root: Path) -> None:
    import pypandoc
    root = assets_root.resolve()
    bundled = Path(pypandoc.__file__).parent / "files" / ("pandoc.exe" if os.name == "nt" else "pandoc")
    binary = str(bundled) if bundled.is_file() else shutil.which("pandoc")
    if not binary:
        raise RuntimeError("缺少 Word 转换组件，请重新安装 pypandoc_binary")
    def convert(value: str, arguments: list[str]) -> str:
        with CONVERSION_LOCK:
            result = subprocess.run([binary, *arguments, "+RTS", "-M512m", "-RTS"],
                                    input=value.encode("utf-8"), capture_output=True, timeout=120,
                                    creationflags=0x08000000 if os.name == "nt" else 0)
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[:500])
        return result.stdout.decode("utf-8")
    tree = json.loads(convert(markdown, ["--from=markdown+tex_math_dollars-raw_html-raw_tex", "--to=json"]))

    def walk(value):
        if isinstance(value, list):
            return [walk(item) for item in value]
        if not isinstance(value, dict):
            return value
        kind, content = value.get("t"), value.get("c")
        if kind == "Image":
            target = content[-1][0]
            path = (root / target).resolve()
            try:
                path.relative_to(root)
                safe = not urlsplit(target).scheme and path.is_file() and path.stat().st_size <= 5 * 1024 * 1024
            except ValueError:
                safe = False
            if safe:
                content[-1][0] = str(path)
            else:
                return {"t": "Str", "c": "[配图未缓存或路径无效，请在应用中查看来源]"}
        if kind == "Link" and urlsplit(content[-1][0]).scheme not in ("", "https", "http"):
            return {"t": "Span", "c": [["", [], []], walk(content[1])]}
        return {key: walk(item) for key, item in value.items()}

    clean = walk(tree)
    destination.parent.mkdir(parents=True, exist_ok=True)
    convert(json.dumps(clean), ["--from=json", "--to=docx", "--output=" + str(destination),
                               "--resource-path=" + str(root)])
