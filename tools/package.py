"""打发布包：把三个 zip 和校验和一次做出来。

    python tools/package.py                 # 默认输出到 dist/
    python tools/package.py --out D:/rel    # 换输出目录
    python tools/package.py --no-suite      # 只出两个单应用包

三个包：
    tingke-suite-v1.0.0.zip        两个应用 + 共用层（一份 .venv 跑两个）
    buketang-v1.0.0.zip            只要「不听课」
    mp3-transcriber-v1.0.0.zip     只要「语音转录工坊」

一律排除：data/ 运行时数据、models/ 端侧模型、__pycache__、.venv、日志。
单应用包里的 README 会把截图路径补上前缀，解压后点开图片不会裂。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.version import __version__  # noqa: E402

APPS = {
    "buketang": "不听课",
    "mp3-transcriber": "语音转录工坊",
}

# 应用目录名与根启动脚本名不一定一致（mp3-transcriber → start-transcriber）
LAUNCHER = {
    "buketang": "start-buketang",
    "mp3-transcriber": "start-transcriber",
}

SKIP_DIRS = {
    "__pycache__", ".git", ".venv", "venv", "env",
    "data", "models", "dist", "build", "restore_backups",
    "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".workbuddy", "_demo_data",
}
SKIP_FILES = {".DS_Store", "Thumbs.db"}
SKIP_EXT = {".pyc", ".pyo", ".log", ".zip"}

COMMON_FILES = ["README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md", ".gitignore"]


def wanted(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in SKIP_DIRS for part in rel.parts):
        return False
    if path.name in SKIP_FILES or path.suffix.lower() in SKIP_EXT:
        return False
    if path.name.startswith("~$"):
        return False
    return True


def add_file(zf: zipfile.ZipFile, src: Path, arc: str) -> None:
    if src.suffix.lower() == ".bat":
        # cmd.exe 对只有 LF 的 batch 会挑食（label 定位、括号块都可能出错），
        # 而仓库在类 Unix 上 checkout 出来就是 LF，所以出包时统一转回 CRLF
        text = src.read_text(encoding="utf-8").replace("\r\n", "\n")
        zf.writestr(arc, text.replace("\n", "\r\n").encode("utf-8"))
    else:
        zf.write(src, arc)


def add_tree(zf: zipfile.ZipFile, base: Path, prefix: str) -> int:
    """把 base 下的文件按 prefix 前缀写进 zip，返回文件数。"""
    count = 0
    for path in sorted(base.rglob("*")):
        if not path.is_file() or not wanted(path):
            continue
        add_file(zf, path, (Path(prefix) / path.relative_to(base)).as_posix())
        count += 1
    return count


def add_text(zf: zipfile.ZipFile, text: str, arc: str) -> None:
    zf.writestr(arc, text)


def fix_links(md: str, app: str) -> str:
    """单应用包里应用在子目录，README 的 assets/ 要补前缀。"""
    return re.sub(r"\]\((assets/)", rf"]({app}/\1", md)


# 单应用包里没有「隔壁那个应用」，提到它的句子得改掉，否则下载的人照着做会撞墙
STANDALONE_FIXES: dict[str, list[tuple[str, str]]] = {
    "buketang": [
        (
            "**FFmpeg**（录音转码切片必需，与语音转录工坊共用）",
            "**FFmpeg**（录音转码切片必需）",
        ),
        (
            "录音转写复用的是隔壁 **语音转录工坊** 的引擎配置——同一个模型没必要配两遍。"
            "在那边配好火山 / OpenAI / 百炼 / 本地任意一家，这边保存后立即生效。\n\n"
            "设置抽屉里会显示当前继承到的引擎和是否就绪。"
            "想完全离线，就把那边切成 **本地 SenseVoice**（见下一节）。",
            "这个包是单独装的，默认直接用 **本地 SenseVoice**（端侧、免费、可离线），"
            "零配置就能开始转写。\n\n"
            "想换成火山 / OpenAI / 百炼的云端识别，就在设置抽屉里自己填一次，配好立即生效；"
            "设置里也会显示当前引擎和是否就绪。",
        ),
        (
            "**报「语音引擎还没配好」** — 去隔壁 **语音转录工坊** 的设置里把引擎密钥填上，"
            "或者切成「本地 SenseVoice」，这边自动生效。",
            "**报「语音引擎还没配好」** — 打开设置 → 语音引擎，切成「本地 SenseVoice」"
            "（端点一次「下载模型」，230MB，之后纯 CPU 跑），或者填一家云端引擎的密钥。",
        ),
    ],
    "mp3-transcriber": [
        (
            "偏好存在浏览器本地，和「不听课」共用。",
            "偏好存在浏览器本地。",
        ),
    ],
}


def standalone(md: str, app: str) -> str:
    """单应用包的 README：补图片前缀，拉平跨目录链接，并把跨应用的指路改成本地说法。"""
    out = fix_links(md, app)
    # 仓库里 README 在子目录，写的是 ../LICENSE；打平到包根后要改成 ./LICENSE
    out = re.sub(r"\]\(\.\./([A-Za-z0-9_.-]+)\)", r"](\1)", out)
    for old, new in STANDALONE_FIXES.get(app, []):
        if old not in out:
            print(f"  ! {app} README 里没找到这段，跨应用表述可能已过期：{old[:24]}…")
            continue
        out = out.replace(old, new)
    return out


def build_suite(out: Path) -> Path:
    name = f"tingke-suite-v{__version__}"
    target = out / f"{name}.zip"
    top = name
    n = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        n += add_tree(zf, ROOT / "shared", f"{top}/shared")
        for app in APPS:
            n += add_tree(zf, ROOT / app, f"{top}/{app}")
        for f in COMMON_FILES:
            src = ROOT / f
            if src.exists():
                add_file(zf, src, f"{top}/{f}")
                n += 1
        for f in ("requirements.txt", "start-buketang.bat", "start-buketang.sh",
                  "start-transcriber.bat", "start-transcriber.sh"):
            src = ROOT / f
            if src.exists():
                add_file(zf, src, f"{top}/{f}")
                n += 1
    print(f"  {target.name}  {target.stat().st_size / 1024 / 1024:.1f} MB  {n} 个文件")
    return target


def build_single(app: str, out: Path) -> Path:
    name = f"{app}-v{__version__}"
    target = out / f"{name}.zip"
    top = name
    n = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        n += add_tree(zf, ROOT / "shared", f"{top}/shared")
        n += add_tree(zf, ROOT / app, f"{top}/{app}")

        # 顶层放一份 README（图片路径补前缀）和启动脚本，解压后直接双击
        readme = ROOT / app / "README.md"
        if readme.exists():
            add_text(zf, standalone(readme.read_text(encoding="utf-8"), app), f"{top}/README.md")
            n += 1
        for f in ("LICENSE", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md"):
            src = ROOT / f
            if src.exists():
                add_file(zf, src, f"{top}/{f}")
                n += 1
        req = ROOT / app / "requirements.txt"
        if req.exists():
            zf.write(req, f"{top}/requirements.txt")
            n += 1
        for suffix, launch in (("bat", "start.bat"), ("sh", "start.sh")):
            src = ROOT / f"{LAUNCHER[app]}.{suffix}"
            if src.exists():
                add_file(zf, src, f"{top}/{launch}")
                n += 1
    print(f"  {target.name}  {target.stat().st_size / 1024 / 1024:.1f} MB  {n} 个文件")
    return target


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description="打发布包")
    ap.add_argument("--out", default=str(ROOT / "dist"), help="输出目录")
    ap.add_argument("--no-suite", action="store_true", help="不打合集包")
    ap.add_argument("--clean", action="store_true", help="先清空输出目录里的旧 zip")
    args = ap.parse_args()

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.clean:
        for old in out.glob("*.zip"):
            old.unlink()

    print(f"版本 {__version__} → {out}")
    made = []
    if not args.no_suite:
        made.append(build_suite(out))
    for app in APPS:
        made.append(build_single(app, out))

    lines = [f"{sha256(p)}  {p.name}" for p in made]
    sums = out / "SHA256SUMS.txt"
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n  {sums.name}")
    for line in lines:
        print("   ", line)


if __name__ == "__main__":
    main()
