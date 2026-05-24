#!/usr/bin/env bash
# Copyright (c) 2026 Peking University & Beijing Siliconheart Technology Co., Ltd.
# XEmail is licensed under Mulan PSL v2.
#
# Build installable artifacts:
# - macOS: pkg that installs /Applications/XEmail.app (bundled runtime + deps)
# - Windows: zip package with install/start scripts
#
# Usage:
#   bash scripts/package_installers.sh
#   bash scripts/package_installers.sh 20260524_010000

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="$ROOT_DIR/dist"
STAMP="${1:-$(date +%Y%m%d_%H%M%S)}"
WORK_DIR="$ROOT_DIR/.packaging_tmp/installers_$STAMP"

MAC_APP_NAME="XEmail.app"
MAC_APP_ID="com.xemail.app"
MAC_APP_VERSION="$STAMP"

MAC_APP_DIR="$WORK_DIR/mac/$MAC_APP_NAME"
MAC_CONTENTS_DIR="$MAC_APP_DIR/Contents"
MAC_MACOS_DIR="$MAC_CONTENTS_DIR/MacOS"
MAC_RES_DIR="$MAC_CONTENTS_DIR/Resources"
MAC_PKG_ROOT="$WORK_DIR/mac/pkg_root"
MAC_ICON_PNG="$WORK_DIR/mac/logo_1024.png"
MAC_TRAY_ICON_PNG="$MAC_RES_DIR/app/web/tray_icon.png"
MAC_ICON_ICNS="$MAC_RES_DIR/XEmail.icns"
MAC_PKG_OUT="$DIST_DIR/xemail-macos-installer-$STAMP.pkg"

WIN_ROOT="$WORK_DIR/win/XEmail"
WIN_ZIP_OUT="$DIST_DIR/xemail-windows-installer-$STAMP.zip"

mkdir -p "$DIST_DIR"
rm -rf "$WORK_DIR"
mkdir -p "$MAC_MACOS_DIR" "$MAC_RES_DIR" "$WIN_ROOT"

echo "==> [1/8] 准备 macOS App 内容目录"
rsync -a --delete \
  --exclude "dist" \
  --exclude "data" \
  --exclude ".git" \
  --exclude ".venv" \
  --exclude ".packaging_tmp" \
  --exclude "__pycache__" \
  --exclude "*.pyc" \
  --exclude ".DS_Store" \
  --exclude "._*" \
  "$ROOT_DIR/" "$MAC_RES_DIR/app/"
printf '%s\n' "$STAMP" > "$MAC_RES_DIR/app/VERSION"

echo "==> [2/8] 构建 macOS App 内置 Python 运行时"
python3 -m venv "$MAC_RES_DIR/runtime"
"$MAC_RES_DIR/runtime/bin/python" -m pip install --upgrade pip >/dev/null
"$MAC_RES_DIR/runtime/bin/python" -m pip install -r "$ROOT_DIR/requirements.txt"
# Make a hard link to the python binary whose filename matches Info.plist's
# CFBundleExecutable (XEmail). When bash execs this name, _NSGetExecutablePath
# returns ".../bin/XEmail", letting NSBundle.mainBundle() correctly resolve
# back to XEmail.app via the CFBundleExecutable-name match. Without this,
# macOS falls back to Python.framework's bundle for the Dock icon — that's
# the "white Python3" icon the user saw.
( cd "$MAC_RES_DIR/runtime/bin" && ln -f python XEmail )

echo "==> [3/8] 生成 App 启动器与 Info.plist"
cat > "$MAC_MACOS_DIR/XEmail" <<'EOF'
#!/bin/bash
set -euo pipefail
APP_CONTENTS="$(cd "$(dirname "$0")/.." && pwd)"
APP_BUNDLE="$(cd "$APP_CONTENTS/.." && pwd)"
RES_DIR="$APP_CONTENTS/Resources"
export XEMAIL_APP_DIR="${XEMAIL_APP_DIR:-$HOME/Library/Application Support/XEmail}"
# Identify the exec'd python process as belonging to this bundle so Cocoa
# applies XEmail's Info.plist (icon etc.) instead of resolving to
# Python.framework's bundle (which is what produced the white Python icon
# in the Dock).
export __CFBundleIdentifier="com.xemail.app"
# First-launch fix: on a fresh install LaunchServices may not have cached
# this bundle yet by the time bash execs into python. When that happens, CF
# cannot resolve __CFBundleIdentifier → the python process registers as a
# standalone app with no bundleID, while the .app keeps a "ghost" Dock entry
# of its own (two dock icons: XEmail + generic exec). Force a synchronous
# re-register so LS knows about us before we drop into Cocoa.
LSREG="/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister"
if [ -x "$LSREG" ]; then
  "$LSREG" -f "$APP_BUNDLE" >/dev/null 2>&1 || true
fi
cd "$RES_DIR/app"
# Exec via the bin/XEmail hard-link (created at packaging time) so the
# running binary's filename matches CFBundleExecutable=XEmail. This is what
# lets NSBundle.mainBundle() correctly resolve to XEmail.app.
exec "$RES_DIR/runtime/bin/XEmail" -m desktop
EOF
chmod +x "$MAC_MACOS_DIR/XEmail"

cat > "$MAC_CONTENTS_DIR/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>
  <string>XEmail</string>
  <key>CFBundleDisplayName</key>
  <string>XEmail</string>
  <key>CFBundleIdentifier</key>
  <string>$MAC_APP_ID</string>
  <key>CFBundleVersion</key>
  <string>$MAC_APP_VERSION</string>
  <key>CFBundleShortVersionString</key>
  <string>$MAC_APP_VERSION</string>
  <key>CFBundleExecutable</key>
  <string>XEmail</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleIconFile</key>
  <string>XEmail</string>
  <key>LSMinimumSystemVersion</key>
  <string>11.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
</dict>
</plist>
EOF

echo "==> [4/8] 生成 App 图标 (icns)"
# Use macOS's built-in qlmanage instead of cairosvg: Homebrew cairo is often
# x86_64-only and fails to load on Apple Silicon arm64 Python.
QL_OUT_DIR="$WORK_DIR/mac/icon_ql"
mkdir -p "$QL_OUT_DIR" "$(dirname "$MAC_ICON_PNG")" "$(dirname "$MAC_TRAY_ICON_PNG")"
qlmanage -t -s 1024 -o "$QL_OUT_DIR" "$ROOT_DIR/web/logo.svg" >/dev/null
mv "$QL_OUT_DIR/logo.svg.png" "$MAC_ICON_PNG"
# Pillow already lives in the runtime venv from step 2, so reuse it for ICNS.
"$MAC_RES_DIR/runtime/bin/python" -c "from PIL import Image; Image.open('$MAC_ICON_PNG').save('$MAC_ICON_ICNS', format='ICNS')"
cp "$MAC_ICON_PNG" "$MAC_TRAY_ICON_PNG"

echo "==> [5/8] 打包 macOS pkg"
mkdir -p "$MAC_PKG_ROOT/Applications"
cp -R "$MAC_APP_DIR" "$MAC_PKG_ROOT/Applications/"
pkgbuild \
  --root "$MAC_PKG_ROOT" \
  --identifier "$MAC_APP_ID" \
  --version "$MAC_APP_VERSION" \
  --install-location "/" \
  "$MAC_PKG_OUT"

echo "==> [6/8] 准备 Windows 安装包内容"
rsync -a --delete \
  --exclude "dist" \
  --exclude "data" \
  --exclude ".git" \
  --exclude ".venv" \
  --exclude ".packaging_tmp" \
  --exclude "__pycache__" \
  --exclude "*.pyc" \
  --exclude ".DS_Store" \
  --exclude "._*" \
  "$ROOT_DIR/" "$WIN_ROOT/"
printf '%s\n' "$STAMP" > "$WIN_ROOT/VERSION"

cat > "$WIN_ROOT/install_windows.bat" <<'EOF'
@echo off
setlocal
set "APP_DIR=%~dp0"
cd /d "%APP_DIR%"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
)
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
start "" cmd /c "call scripts\start_windows.bat"
echo.
echo XEmail 已安装并尝试启动。
echo 首次打开请访问 http://127.0.0.1:8000
pause
EOF

cat > "$WIN_ROOT/README_WINDOWS_INSTALL.txt" <<'EOF'
XEmail Windows 安装说明
=======================

1) 双击运行 install_windows.bat
2) 等待依赖安装完成
3) 打开浏览器访问 http://127.0.0.1:8000

停止服务：
- 运行 scripts\stop_windows.bat
EOF

cat > "$WIN_ROOT/scripts/start_windows.bat" <<'EOF'
@echo off
setlocal
set "APP_DIR=%~dp0.."
cd /d "%APP_DIR%"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
start "" python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
EOF

cat > "$WIN_ROOT/scripts/stop_windows.bat" <<'EOF'
@echo off
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000" ^| findstr LISTENING') do taskkill /PID %%a /F >nul 2>nul
echo XEmail 后端已尝试停止（端口 8000）。
EOF

echo "==> [7/8] 打包 Windows zip"
(
  cd "$WORK_DIR/win"
  zip -r "$WIN_ZIP_OUT" "XEmail" -x "*/__pycache__/*" "*.pyc" "*/.DS_Store" "*/._*"
)

echo "==> [8/8] 生成摘要"
echo "macOS:   $MAC_PKG_OUT"
echo "Windows: $WIN_ZIP_OUT"
shasum -a 256 "$MAC_PKG_OUT" "$WIN_ZIP_OUT"
