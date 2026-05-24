#!/usr/bin/env bash
# Copyright (c) 2026 Peking University & Beijing Siliconheart Technology Co., Ltd.
# XEmail is licensed under Mulan PSL v2.

set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || { echo "[启动失败] 找不到项目目录: $PROJECT_DIR"; exit 1; }

if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
  PY="$PROJECT_DIR/.venv/bin/python"
  echo "[环境] 使用虚拟环境: $PY"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
  echo "[环境] 未发现 .venv，使用系统 Python: $PY"
else
  echo "[启动失败] 找不到 python3，请先安装 Python 3。"
  exit 1
fi

echo "================ XEmail 托盘模式启动 ================"
echo "项目目录 : $PROJECT_DIR"
echo "说明     : 关闭窗口将最小化到托盘（需 pystray+pillow）"
echo

XEMAIL_ENABLE_TRAY=1 "$PY" -m desktop
