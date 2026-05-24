#!/usr/bin/env bash
# Run this WHILE the two Dock icons are visible.

echo "=== 1) Dock 里 persistent-apps（用户固定的快捷方式） ==="
defaults read com.apple.dock persistent-apps 2>/dev/null \
  | python3 -c '
import sys, re, json
text = sys.stdin.read()
# Extract any tile that mentions XEmail / python
chunks = re.split(r"\n        \{", text)
for i, c in enumerate(chunks):
    if "XEmail" in c or "xemail" in c.lower() or "python" in c.lower() or "exec" in c.lower():
        print(f"--- match #{i} ---")
        print(c[:800])
'

echo ""
echo "=== 2) Dock 里 recent-apps（最近用过的） ==="
defaults read com.apple.dock recent-apps 2>/dev/null \
  | grep -E "XEmail|xemail|python|exec" -B1 -A3 | head -40

echo ""
echo "=== 3) 所有 XEmail 相关的运行进程 PID + lsappinfo type ==="
for pid in $(pgrep -i xemail); do
  echo "--- PID $pid ---"
  lsappinfo info "$pid" 2>/dev/null | grep -E "(bundleID|bundle path|executable path|originalExec|type=|flavor)"
done

echo ""
echo "=== 4) Dock 进程是否在用旧的图标缓存 ==="
ls -la ~/Library/Caches/com.apple.iconservices.store 2>&1 | head -3
echo "(若存在且为 root/旧时间，缓存可能是问题)"
