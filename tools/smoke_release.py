"""发布包冒烟测试：解压 → 起服务 → 打接口 → 收工。

发布前跑一遍，确认「别人下下来解压后真的能用」：

    python tools/smoke_release.py                 # 测 dist/ 下所有 zip
    python tools/smoke_release.py dist/xxx.zip    # 测指定的包

只验证「装得上、起得来、静态资源能取到、版本号对」，
不依赖任何 Key，也不会碰你本机的 data/。
"""
from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.version import __version__  # noqa: E402

PASS, FAIL = [], []


def check(name: str, got, want) -> None:
    ok = got == want if not callable(want) else bool(want(got))
    if ok:
        PASS.append(name)
        print(f"  ok   {name}")
    else:
        FAIL.append(f"{name}: 期望 {want!r}，实际 {got!r}")
        print(f"  FAIL {name}: 期望 {want!r}，实际 {got!r}")


def free_port(start: int = 8810) -> int:
    for offset in range(60):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def fetch(url: str, timeout: float = 4.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception:  # noqa: BLE001
        return None, b""


def wait_health(base: str, timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        status, body = fetch(f"{base}/api/health", 1.5)
        if status == 200 and b'"ok":true' in body.replace(b" ", b""):
            return True
        time.sleep(0.4)
    return False


def apps_in(root: Path) -> list[Path]:
    """找出包里所有可启动的 app.py（合集包会有两个）。"""
    hits = []
    for p in sorted(root.rglob("app.py")):
        if (p.parent / "core").is_dir() and (p.parent / "web").is_dir():
            hits.append(p)
    return hits


def smoke(zip_path: Path) -> None:
    print(f"\n== {zip_path.name}")
    tmp = Path(tempfile.mkdtemp(prefix="rel_smoke_"))
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(tmp)

    top = [p for p in tmp.iterdir() if p.is_dir()]
    if not top:
        check("包里有顶层目录", False, True)
        return
    root = top[0]
    print(f"   解压到 {root.name}/")

    starts = list(root.glob("start*.bat")) + list(root.glob("start*.sh"))
    check("带了启动脚本", len(starts), lambda n: n is not None and n >= 1)
    check("带了 README", (root / "README.md").exists(), True)
    check("带了 LICENSE", (root / "LICENSE").exists(), True)
    check("带了第三方许可说明", (root / "THIRD_PARTY_NOTICES.md").exists(), True)
    check("带了 requirements.txt", (root / "requirements.txt").exists(), True)
    check("没把 data/ 打进来", any(p.name == "data" for p in root.rglob("data")), False)
    check("没把 models/ 打进来", any(p.name == "models" for p in root.rglob("models")), False)

    targets = apps_in(root)
    check("能找到可启动应用", len(targets), lambda n: n is not None and n >= 1)

    # 换行符错了，双击就是「闪一下就没了」或者「$'\r': command not found」
    bad_bat = [p.relative_to(root).as_posix() for p in root.rglob("*.bat")
               if b"\r\n" not in p.read_bytes()]
    check(".bat 是 CRLF 换行", bad_bat, [])
    bad_sh = [p.relative_to(root).as_posix() for p in root.rglob("*.sh")
              if b"\r\n" in p.read_bytes()]
    check(".sh 是 LF 换行", bad_sh, [])

    # README 是下载后第一眼看到的东西：截图不能裂，链接不能指到包外，也不能指向包里不存在的应用
    readme = root / "README.md"
    if readme.exists():
        md = readme.read_text(encoding="utf-8", errors="replace")
        links = re.findall(r"\]\(([^)\s]+)\)", md)
        rel = [l for l in links if not l.startswith(("http", "#", "mailto"))]
        broken = [l for l in rel if not (root / l.split("#")[0]).exists()]
        check("README 里的链接都在包内", broken, [])
        if len(targets) == 1:
            peer = {"buketang": "语音转录工坊", "mp3-transcriber": "不听课"}.get(targets[0].parent.name)
            check("单应用包不再提到隔壁应用", (("隔壁" in md) or (peer in md) if peer else False), False)

    for entry in targets:
        name = entry.parent.name
        port = free_port()
        env = dict(os.environ)
        env["PORT"] = str(port)
        env["PYTHONIOENCODING"] = "utf-8"
        env["BUKETANG_DATA_DIR" if name == "buketang" else "TRANSCRIBER_DATA_DIR"] = str(tmp / f"data_{name}")
        # 日志写文件而不是 PIPE：管道没人读会填满，把服务卡死在 write 上
        log = tmp / f"server_{name}.log"
        fh = log.open("wb")
        proc = subprocess.Popen(
            [sys.executable, str(entry), "--no-browser"],
            cwd=str(root), env=env,
            stdout=fh, stderr=subprocess.STDOUT,
        )
        fh.close()
        base = f"http://127.0.0.1:{port}"
        try:
            up = wait_health(base)
            check(f"{name} 起得来", up, True)
            if not up:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
                print("   ── 启动输出 ──")
                print(log.read_text(encoding="utf-8", errors="replace")[-1200:])
                continue

            status, body = fetch(f"{base}/")
            check(f"{name} 首页 200", status, 200)
            check(f"{name} 首页是个页面", b"<html" in body.lower(), True)

            # 共用静态资源：theme.css 在 shared/web 里，走的是双目录查找
            status, body = fetch(f"{base}/static/theme.css")
            check(f"{name} 能取到共用 theme.css", (status, len(body) > 500), lambda v: v == (200, True))
            status, body = fetch(f"{base}/static/app.js")
            check(f"{name} 能取到自己的 app.js", status, 200)

            status, body = fetch(f"{base}/api/health")
            ver = re.search(rb'"version":"([^"]+)"', body or b"")
            check(f"{name} 版本号一致", ver.group(1).decode() if ver else None, __version__)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="发布包冒烟测试")
    ap.add_argument("zips", nargs="*", help="zip 路径；不填则测 dist/ 下全部")
    args = ap.parse_args()

    zips = [Path(p).resolve() for p in args.zips] or sorted((ROOT / "dist").glob("*.zip"))
    if not zips:
        raise SystemExit("dist/ 里没有 zip，先跑 python tools/package.py")

    for z in zips:
        smoke(z)

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  -", f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
