"""Install into the caller's venv when its requirements change, including upgrades."""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path


def ensure(requirements: Path) -> bool:
    marker = Path(sys.prefix) / ".requirements.sha256"
    wanted = hashlib.sha256(requirements.read_bytes()).hexdigest()
    if marker.is_file() and marker.read_text(encoding="ascii").strip() == wanted:
        return True
    print("安装或更新依赖，请稍候……", flush=True)
    command = [sys.executable, "-m", "pip", "install", "-q", "-r", str(requirements)]
    first = subprocess.run(command)
    if first.returncode:
        second = subprocess.run(command + ["-i", "https://pypi.tuna.tsinghua.edu.cn/simple"])
        if second.returncode:
            print("依赖安装失败，请检查网络后重新启动。", flush=True)
            return False
    marker.write_text(wanted, encoding="ascii")
    return True


if __name__ == "__main__":
    sys.exit(0 if ensure(Path(sys.argv[1]).resolve()) else 1)
