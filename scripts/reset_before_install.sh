#!/usr/bin/env bash
# Cleanly remove any prior XEmail.app install and force LaunchServices to
# forget its cached icon/bundle association. Run BEFORE double-clicking the
# new .pkg, then verify the Dock icon is no longer the generic Python icon.

set -u

echo "==> 1) 杀掉一切 XEmail 残留进程"
pkill -9 -f "XEmail.app/Contents" 2>/dev/null || true
pkill -9 -f "XEmail -m desktop" 2>/dev/null || true
pkill -9 -f "XEmail -m uvicorn" 2>/dev/null || true
lsof -i :8000 2>/dev/null | awk '/LISTEN/ {print $2}' | xargs -r kill -9 2>/dev/null || true
sleep 1

echo "==> 2) 卸载旧的 /Applications/XEmail.app"
if [ -d "/Applications/XEmail.app" ]; then
  sudo rm -rf "/Applications/XEmail.app"
  echo "    removed."
else
  echo "    (not installed)"
fi

echo "==> 3) 让 LaunchServices 忘掉 com.xemail.app 的缓存"
LSREG="/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister"
"$LSREG" -u "/Applications/XEmail.app" 2>/dev/null || true
"$LSREG" -kill -r -domain local -domain system -domain user

echo "==> 4) 重启 Dock 以刷新图标缓存"
killall Dock
sleep 1

echo "==> 5) done。现在双击新 pkg 安装："
ls -lt dist/xemail-macos-installer-*.pkg 2>/dev/null | head -1
