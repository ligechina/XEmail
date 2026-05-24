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

# Double-click on macOS to start the XEmail backend in the background.
# - Activates .venv if present, falls back to system python3.
# - Refuses to start a second copy on the same port.
# - Detaches via nohup; the Terminal window that pops up can be closed
#   immediately and the server keeps running.

set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || { echo "[启动失败] 找不到项目目录: $PROJECT_DIR"; exit 1; }

PORT="${XEMAIL_PORT:-8000}"
HOST="${XEMAIL_HOST:-127.0.0.1}"
PID_FILE="$PROJECT_DIR/data/server.pid"
LOG_FILE="$PROJECT_DIR/data/server.log"
APP_ROOT="${XEMAIL_APP_DIR:-$HOME/Library/Application Support/XEmail}"
USER_VENV="$APP_ROOT/runtime/.venv"

mkdir -p "$PROJECT_DIR/data"

echo "================ XEmail 启动 ================"
echo "项目目录 : $PROJECT_DIR"
echo "监听地址 : http://$HOST:$PORT"
echo "日志文件 : $LOG_FILE"
echo

# Already running?
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
  RUNNING_PID="$(cat "$PID_FILE")"
  echo "[已在运行] 检测到 PID=${RUNNING_PID}，无需重复启动。"
  echo "如需停止，请双击 scripts/stop.command 或在系统管理页点「关闭后端」。"
  echo
  echo "浏览器即将打开 http://$HOST:$PORT ..."
  sleep 1
  open "http://$HOST:$PORT" >/dev/null 2>&1 || true
  echo
  echo "（本窗口可以直接关闭）"
  exit 0
fi

# Port already taken by something else?
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[端口冲突] 端口 $PORT 已被其它程序占用。"
  echo "解决方法：先用 stop.command 关闭旧实例，或修改 XEMAIL_PORT 环境变量后重试。"
  exit 1
fi

# Pick python: prefer writable project venv, else per-user venv.
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

# Sanity check: uvicorn importable?
if ! "$PY" -c "import uvicorn, fastapi" >/dev/null 2>&1; then
  echo "[依赖缺失] 正在安装 uvicorn / fastapi ..."
  "$PY" -m pip install -r "$PROJECT_DIR/requirements.txt" || {
    echo "[启动失败] 自动安装依赖失败，请手动执行："
    echo "           \"$PY\" -m pip install -r \"$PROJECT_DIR/requirements.txt\""
    exit 1
  }
fi

# Truncate previous log so we don't accumulate forever.
: > "$LOG_FILE"

echo "[启动中] 正在后台启动 uvicorn ..."
nohup "$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT" \
  >> "$LOG_FILE" 2>&1 &
NEW_PID=$!
disown "$NEW_PID" 2>/dev/null || true
echo "$NEW_PID" > "$PID_FILE"

# Wait briefly and confirm the process actually came up.
sleep 2
if ! kill -0 "$NEW_PID" 2>/dev/null; then
  echo "[启动失败] 进程已退出，请查看日志：$LOG_FILE"
  rm -f "$PID_FILE"
  exit 1
fi

echo "[完成] PID=$NEW_PID"
echo "      访问地址: http://$HOST:$PORT"
echo "      查看日志: tail -f \"$LOG_FILE\""
echo "      停止服务: 双击 scripts/stop.command 或在系统管理页点「关闭后端」"
echo
echo "浏览器即将打开 ..."
sleep 1
open "http://$HOST:$PORT" >/dev/null 2>&1 || true
echo
echo "（本窗口可以直接关闭，后端会继续运行）"
