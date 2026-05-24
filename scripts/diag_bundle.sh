#!/usr/bin/env bash
# Run this WHILE XEmail is open (two dock icons visible).

XPID=$(pgrep -f "XEmail -m desktop" | head -1)
if [ -z "$XPID" ]; then
  echo "XEmail 没在运行 — 请先打开 XEmail 再跑。"
  exit 1
fi
echo "Running XEmail PID: $XPID"
echo ""

echo "=== A) Python 进程的环境变量（关注 __CF* 是否真的存在） ==="
ps eww -o command= -p "$XPID" \
  | tr ' ' '\n' \
  | grep -E "^(__|XEMAIL|PYTHON|CF|LC_|LS)" \
  | sort -u
echo ""

echo "=== B) lsappinfo 看到的 bundle info ==="
lsappinfo info "$XPID" 2>&1 | grep -E "(bundle|executable|UnbundledOther|Identifier|originalExec)"
echo ""

echo "=== C) 让 Python 自己报 NSBundle 看到了什么 ==="
PYBIN=/Applications/XEmail.app/Contents/Resources/runtime/bin/python3
"$PYBIN" - <<'PY'
import os
print("env __CFBundleIdentifier =", repr(os.environ.get("__CFBundleIdentifier")))
print("env __CFBundleHelperPath =", repr(os.environ.get("__CFBundleHelperPath")))
try:
    from Foundation import NSBundle
    mb = NSBundle.mainBundle()
    print("NSBundle.mainBundle().bundlePath() =", mb.bundlePath())
    print("NSBundle.mainBundle().bundleIdentifier() =", mb.bundleIdentifier())
    print("NSBundle.mainBundle().executablePath() =", mb.executablePath())
except Exception as e:
    print("PyObjC import failed:", e)
PY
echo ""

echo "=== D) Info.plist 里有没有 LSEnvironment ==="
/usr/libexec/PlistBuddy -c "Print :LSEnvironment" /Applications/XEmail.app/Contents/Info.plist 2>&1 | head -10
