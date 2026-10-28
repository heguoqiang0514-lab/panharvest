#!/usr/bin/env sh
# 打开本地网页界面（默认 http://127.0.0.1:8765/）
set -eu

cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "[x] 没找到 Python。装 Python 3.10+ 后重试。" >&2
  exit 1
fi

echo "Starting local UI, the browser will open automatically ..."
exec "$PY" -m panharvest serve
