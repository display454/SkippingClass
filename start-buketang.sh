#!/usr/bin/env bash
# 启动「不听课」（在仓库根目录建一个共用的 .venv）
set -e
cd "$(dirname "$0")"

PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$cand" >/dev/null 2>&1 &&
     "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$cand"; break
  fi
done

if [ -z "$PY" ]; then
  echo "[x] 需要 Python 3.10 或更高版本。" >&2
  exit 1
fi

VPY=".venv/bin/python"
if [ ! -x "$VPY" ]; then
  echo "[1/2] 创建虚拟环境 .venv ..."
  "$PY" -m venv .venv
fi

"$VPY" "shared/bootstrap.py" requirements.txt

exec "$VPY" buketang/app.py
