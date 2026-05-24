#!/usr/bin/env bash
# Diagnostic for "two XEmail Dock icons" / "Dock icon click won't reopen window".
# Run AFTER you've launched /Applications/XEmail.app and are in the bad state.

echo "=== 1) XEmail 相关进程树 ==="
ps -axwwo pid,ppid,user,command | grep -i -E "(XEmail|xemail|uvicorn|webview)" | grep -v grep

echo ""
echo "=== 2) LaunchServices 注册了哪些 app 实例 ==="
lsappinfo list | grep -i -E "(XEmail|com.xemail|python|bash)" -B1 -A6

echo ""
echo "=== 3) 当前 Dock 里实际显示的 app 与对应 PID ==="
osascript -e 'tell application "System Events" to get {name, unix id} of (every application process whose visible is true)'

echo ""
echo "=== 4) 这些 PID 各自的可执行文件路径与 bundle ==="
for pid in $(pgrep -i "XEmail|python|uvicorn"); do
  echo "--- PID $pid ---"
  ps -o pid,command -p "$pid" 2>/dev/null
  lsappinfo info -only bundlepath "$pid" 2>/dev/null
  lsappinfo info -only executablepath "$pid" 2>/dev/null
  lsappinfo info -only bundleid "$pid" 2>/dev/null
  echo ""
done
