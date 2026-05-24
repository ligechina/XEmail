#!/usr/bin/env bash
# Copyright (c) 2026 Peking University & Beijing Siliconheart Technology Co., Ltd.
# XEmail is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#          http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || { echo "[启动失败] 找不到项目目录: $PROJECT_DIR"; exit 1; }

APP_ROOT="${XEMAIL_APP_DIR:-$HOME/Library/Application Support/XEmail}"
USER_VENV="$APP_ROOT/runtime/.venv"

if [ -x "$PROJECT_DIR/.venv/bin/python" ] && [ -w "$PROJECT_DIR/.venv" ]; then
  PY="$PROJECT_DIR/.venv/bin/python"
  echo "[环境] 使用项目虚拟环境: $PY"
elif command -v python3 >/dev/null 2>&1; then
  if [ ! -x "$USER_VENV/bin/python" ]; then
    echo "[环境] 创建用户虚拟环境: $USER_VENV"
    mkdir -p "$APP_ROOT/runtime"
    python3 -m venv "$USER_VENV" || { echo "[启动失败] 无法创建虚拟环境"; exit 1; }
  fi
  PY="$USER_VENV/bin/python"
  echo "[环境] 使用用户虚拟环境: $PY"
else
  echo "[启动失败] 找不到 python3，请先安装 Python 3。"
  exit 1
fi

if ! "$PY" -c "import uvicorn, fastapi, webview" >/dev/null 2>&1; then
  echo "[依赖缺失] 正在安装 uvicorn / fastapi / pywebview ..."
  "$PY" -m pip install -r "$PROJECT_DIR/requirements.txt" || {
    echo "[启动失败] 自动安装依赖失败，请手动执行："
    echo "           \"$PY\" -m pip install -r \"$PROJECT_DIR/requirements.txt\""
    exit 1
  }
fi

echo "================ XEmail 桌面版启动 ================"
echo "项目目录 : $PROJECT_DIR"
echo "启动方式 : Python + pywebview（自动拉起 FastAPI）"
if [ -n "${XEMAIL_APP_DIR:-}" ]; then
  echo "应用目录 : $XEMAIL_APP_DIR"
fi
echo

"$PY" -m desktop
