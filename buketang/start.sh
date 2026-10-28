#!/usr/bin/env bash
# 语音转录工坊 / 不听课 · macOS & Linux 启动脚本
set -e
cd "$(dirname "$0")"

PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$cand" >/dev/null 2>&1 &&
     "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$cand"
    break
  fi
done

if [ -z "$PY" ]; then
  echo "[x] 需要 Python 3.10 或更高版本，没有找到可用的解释器。" >&2
  echo "    macOS:  brew install python@3.12" >&2
  echo "    Ubuntu: sudo apt install python3 python3-venv" >&2
  exit 1
fi

VPY=".venv/bin/python"

if [ ! -x "$VPY" ]; then
  echo "[1/2] 创建虚拟环境 .venv ..."
  "$PY" -m venv .venv
fi

"$VPY" "../shared/bootstrap.py" requirements.txt

if ! "$VPY" -c "import sys; sys.path.insert(0, '..'); from shared.audio_utils import find_ffmpeg; sys.exit(0 if find_ffmpeg() else 1)"; then
  echo "    [提示] 没检测到 ffmpeg：macOS 用 brew install ffmpeg，Ubuntu 用 sudo apt install ffmpeg"
fi

exec "$VPY" app.py
