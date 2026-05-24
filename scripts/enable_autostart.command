#!/usr/bin/env bash
# Copyright (c) 2026 Peking University & Beijing Siliconheart Technology Co., Ltd.
# XEmail is licensed under Mulan PSL v2.

set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || { echo "[失败] 找不到项目目录: $PROJECT_DIR"; exit 1; }

if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
  PY="$PROJECT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  echo "[失败] 找不到 python3，请先安装 Python 3。"
  exit 1
fi

"$PY" -m desktop --enable-autostart
echo
echo "（本窗口可以直接关闭）"
