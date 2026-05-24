from __future__ import annotations

import argparse
import os
import sys

from desktop import app
from desktop.autostart import autostart_status, disable_autostart, enable_autostart


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m desktop")
    parser.add_argument(
        "--enable-autostart",
        action="store_true",
        help="在 macOS 上启用开机启动。",
    )
    parser.add_argument(
        "--disable-autostart",
        action="store_true",
        help="在 macOS 上关闭开机启动。",
    )
    parser.add_argument(
        "--autostart-status",
        action="store_true",
        help="查看开机启动状态。",
    )
    parser.add_argument(
        "--enable-tray",
        action="store_true",
        help="启用托盘模式（关闭窗口后最小化到托盘）。",
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    if args.enable_autostart:
        path = enable_autostart(python_executable=app.pick_python_executable())
        print(f"已启用开机启动: {path}")
        return
    if args.disable_autostart:
        removed = disable_autostart()
        if removed:
            print("已关闭开机启动并移除 LaunchAgent。")
        else:
            print("开机启动已关闭（未检测到本地 LaunchAgent 文件）。")
        return
    if args.autostart_status:
        print(f"开机启动状态: {autostart_status()}")
        return

    if args.enable_tray:
        os.environ["XEMAIL_ENABLE_TRAY"] = "1"

    app.main()

if __name__ == "__main__":
    main()
