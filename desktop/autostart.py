from __future__ import annotations

import subprocess
import sys
from pathlib import Path

LAUNCH_AGENT_LABEL = "org.xemail.desktop"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = LAUNCH_AGENTS_DIR / f"{LAUNCH_AGENT_LABEL}.plist"


def _run_launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def _plist_text(python_executable: str) -> str:
    py = python_executable.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LAUNCH_AGENT_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{py}</string>
    <string>-m</string>
    <string>desktop</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
</dict>
</plist>
"""


def ensure_supported() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("开机启动当前仅支持 macOS（LaunchAgent）。")


def enable_autostart(*, python_executable: str) -> Path:
    ensure_supported()
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(_plist_text(python_executable), encoding="utf-8")
    _run_launchctl("bootout", f"gui/{_uid()}/{LAUNCH_AGENT_LABEL}")
    _run_launchctl("bootstrap", f"gui/{_uid()}", str(PLIST_PATH))
    _run_launchctl("enable", f"gui/{_uid()}/{LAUNCH_AGENT_LABEL}")
    return PLIST_PATH


def disable_autostart() -> bool:
    ensure_supported()
    _run_launchctl("bootout", f"gui/{_uid()}/{LAUNCH_AGENT_LABEL}")
    removed = False
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
        removed = True
    return removed


def autostart_status() -> str:
    ensure_supported()
    if not PLIST_PATH.exists():
        return "disabled"
    result = _run_launchctl("print", f"gui/{_uid()}/{LAUNCH_AGENT_LABEL}")
    if result.returncode == 0:
        return "enabled"
    return "configured"


def _uid() -> str:
    return str(subprocess.check_output(["id", "-u"], text=True).strip())
