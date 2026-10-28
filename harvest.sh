#!/usr/bin/env sh
# panharvest —— macOS / Linux 命令行入口（Windows 用 harvest.bat）
#   ./harvest.sh "葬送的芙莉莲 漫画 第1-10卷 高清"
#   ./harvest.sh doctor | selftest | serve | search "…"
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

exec "$PY" -m panharvest "$@"
