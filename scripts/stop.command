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

# Double-click on macOS to stop the XEmail backend started by start.command.
# Falls back to scanning for a uvicorn process on the configured port if
# the PID file is missing or stale.

set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || { echo "[关闭失败] 找不到项目目录: $PROJECT_DIR"; exit 1; }

PORT="${XEMAIL_PORT:-8000}"
PID_FILE="$PROJECT_DIR/data/server.pid"

echo "================ XEmail 关闭 ================"

PID=""
if [ -f "$PID_FILE" ]; then
  CANDIDATE="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$CANDIDATE" ] && kill -0 "$CANDIDATE" 2>/dev/null; then
    PID="$CANDIDATE"
  else
    echo "[提示] PID 文件指向的进程已不存在，将清理 PID 文件。"
    rm -f "$PID_FILE"
  fi
fi

if [ -z "$PID" ]; then
  # Fall back: anything listening on the port?
  PID="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -n 1 || true)"
  if [ -n "$PID" ]; then
    echo "[提示] 通过端口 ${PORT} 找到进程 PID=${PID}。"
  fi
fi

if [ -z "$PID" ]; then
  echo "[已关闭] 没有检测到正在运行的 XEmail 后端。"
  echo
  echo "（本窗口可以直接关闭）"
  exit 0
fi

echo "[关闭中] 向 PID=$PID 发送 SIGTERM ..."
kill "$PID" 2>/dev/null || true

# Give it up to ~6s to exit gracefully.
for _ in 1 2 3 4 5 6; do
  if ! kill -0 "$PID" 2>/dev/null; then break; fi
  sleep 1
done

if kill -0 "$PID" 2>/dev/null; then
  echo "[强制] 进程仍在运行，发送 SIGKILL ..."
  kill -9 "$PID" 2>/dev/null || true
  sleep 1
fi

if kill -0 "$PID" 2>/dev/null; then
  echo "[关闭失败] 进程 $PID 无法终止，请手动处理。"
  exit 1
fi

rm -f "$PID_FILE"
echo "[完成] XEmail 已停止。"
echo
echo "（本窗口可以直接关闭）"
