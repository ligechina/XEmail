#!/usr/bin/env python3
"""XEmail desktop launcher.

Starts a local FastAPI backend and opens it in a desktop webview window.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import urlopen

try:
    import fcntl
except ImportError:  # pragma: no cover - only relevant on non-POSIX.
    fcntl = None  # type: ignore[assignment]

PROJECT_DIR = Path(__file__).resolve().parent.parent
LEGACY_DATA_DIR = PROJECT_DIR / "data"


def _default_app_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "XEmail"
    return Path.home() / ".xemail"


APP_ROOT = Path(os.environ.get("XEMAIL_APP_DIR", "")).expanduser() if os.environ.get("XEMAIL_APP_DIR") else _default_app_root()
RUNTIME_DIR = APP_ROOT / "runtime"
LOG_FILE = RUNTIME_DIR / "desktop-backend.log"
LOCK_FILE = RUNTIME_DIR / "desktop.lock"
LAUNCHER_CONFIG_FILE = RUNTIME_DIR / "launcher_config.json"
BUILD_VERSION_FILE = PROJECT_DIR / "VERSION"

HOST = os.environ.get("XEMAIL_HOST", "127.0.0.1")
PORT = int(os.environ.get("XEMAIL_PORT", "8000"))
HEALTH_TIMEOUT_SECONDS = 20.0
HEALTH_POLL_INTERVAL_SECONDS = 0.3


def _read_stored_tray_setting(data_dir: Path) -> bool:
    cfg_file = data_dir / "config.json"
    if not cfg_file.exists():
        return True
    try:
        raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    except Exception:
        return True
    return bool(((raw.get("system") or {}).get("desktop") or {}).get("enable_tray", True))


def _resolve_enable_tray_for_data_dir(data_dir: Path) -> bool:
    env_val = os.environ.get("XEMAIL_ENABLE_TRAY", "").strip().lower()
    if env_val:
        return env_val in {"1", "true", "yes", "on"}
    return _read_stored_tray_setting(data_dir)


def _load_launcher_config() -> dict:
    if not LAUNCHER_CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(LAUNCHER_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_launcher_config(config: dict) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCHER_CONFIG_FILE.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _default_backend_data_dir() -> Path:
    return APP_ROOT / "data"


def _read_build_version() -> str:
    try:
        text = BUILD_VERSION_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return "dev"
    return text or "dev"


def _clear_webview_http_cache() -> None:
    # WKWebView (started with private_mode=False) keeps a persistent HTTP
    # NetworkCache under ~/Library/Caches/<AppName>/WebKit/. After a pkg
    # upgrade that ships new web/ assets, that cache can keep painting the
    # OLD index.html even though the backend now serves fresh files —
    # exactly the "I don't see the new button" symptom. We delete just the
    # HTTP cache; cookies / localStorage live under
    # ~/Library/WebKit/<AppName>/WebsiteData and are left intact so the user
    # stays logged in.
    if sys.platform != "darwin":
        return
    caches_root = Path.home() / "Library" / "Caches"
    # The cache folder is keyed by the bundle name; cover the names this app
    # is known to register under.
    for name in ("XEmail", "com.xemail.app"):
        webkit_dir = caches_root / name / "WebKit"
        for sub in ("NetworkCache", "CacheStorage"):
            target = webkit_dir / sub
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)


def _maybe_clear_webview_cache_on_upgrade() -> None:
    # Only clear on a version change so normal launches stay fast. The
    # cleared-version stamp lives in launcher_config.json next to the
    # data-dir prompt stamp.
    cfg = _load_launcher_config()
    build_version = _read_build_version()
    if cfg.get("webcache_cleared_version") == build_version:
        return
    _clear_webview_http_cache()
    cfg["webcache_cleared_version"] = build_version
    _save_launcher_config(cfg)


def _choose_data_dir_via_nsopenpanel(default_dir: Path) -> Optional[Path]:
    # Show a native macOS "choose folder" dialog inside our own process
    # instead of shelling out to osascript. Returns the chosen path, or
    # None if the user cancels. Raises on internal errors so the caller
    # can fall back to tkinter.
    if sys.platform != "darwin":
        return None
    from AppKit import (  # type: ignore
        NSApplication,
        NSApplicationActivationPolicyRegular,
        NSOpenPanel,
    )
    from Foundation import NSURL  # type: ignore

    app = NSApplication.sharedApplication()
    # Make sure the panel comes to the front and we have a Dock presence
    # before the modal loop starts.
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    app.activateIgnoringOtherApps_(True)

    panel = NSOpenPanel.openPanel()
    panel.setCanChooseDirectories_(True)
    panel.setCanChooseFiles_(False)
    panel.setAllowsMultipleSelection_(False)
    panel.setCanCreateDirectories_(True)
    panel.setTitle_("请选择 XEmail 用户数据目录")
    panel.setPrompt_("选择")
    panel.setMessage_("请选择 XEmail 的用户数据存放目录")
    try:
        panel.setDirectoryURL_(NSURL.fileURLWithPath_(str(default_dir)))
    except Exception:
        pass

    response = panel.runModal()
    # NSModalResponseOK == 1. Hardcoded to avoid importing yet another
    # symbol on platforms where the constant moves around.
    if response != 1:
        return None
    url = panel.URL()
    if url is None:
        return None
    path = url.path()
    if not path:
        return None
    return Path(str(path)).expanduser()


def _choose_data_dir_interactive(default_dir: Path) -> Optional[Path]:
    # Local desktop UX: first launch asks where user data should live.
    if sys.platform == "darwin":
        # Was previously spawning `osascript -e 'choose folder...'`. That
        # subprocess showed a folder picker reliably, but on first launch
        # the act of running it (right before our own Cocoa init) caused
        # macOS to register the post-picker python process as a separate
        # app — leaving a second "exec" Dock icon alongside the real
        # XEmail icon. Replacing it with an in-process NSOpenPanel keeps
        # everything inside one AppKit context.
        result = _choose_data_dir_via_nsopenpanel(default_dir)
        if result is not None:
            return result
        # `None` means the panel was cancelled. Match the previous behavior
        # of aborting startup when the user explicitly cancels.
        raise RuntimeError("未选择数据目录，已取消启动。")

    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
    except Exception:
        return None

    root = tk.Tk()
    root.withdraw()
    root.update_idletasks()
    try:
        answer = messagebox.askyesnocancel(
            "XEmail 数据目录",
            (
                "请选择 XEmail 的用户数据存放目录。\n\n"
                f"默认目录：{default_dir}\n\n"
                "是：使用默认目录\n"
                "否：手动选择目录\n"
                "取消：退出启动"
            ),
            icon="question",
        )
        if answer is None:
            raise RuntimeError("用户已取消启动。")
        if answer:
            return default_dir

        selected = filedialog.askdirectory(
            title="选择 XEmail 用户数据目录",
            initialdir=str(default_dir.parent),
            mustexist=True,
        )
        if not selected:
            raise RuntimeError("未选择数据目录，已取消启动。")
        return Path(selected).expanduser()
    finally:
        root.destroy()


def _resolve_backend_data_dir() -> Path:
    # Highest priority: explicit environment override.
    env_override = os.environ.get("XEMAIL_DATA_DIR", "").strip()
    if env_override:
        return Path(env_override).expanduser()

    cfg = _load_launcher_config()
    build_version = _read_build_version()
    configured = str(cfg.get("data_dir") or "").strip()
    prompted = bool(cfg.get("data_dir_prompted", False))
    prompted_version = str(cfg.get("data_dir_prompted_version") or "").strip()
    already_prompted_this_version = prompted and prompted_version == build_version

    if configured and already_prompted_this_version:
        return Path(configured).expanduser()

    default_dir = Path(configured).expanduser() if configured else _default_backend_data_dir()
    chosen = _choose_data_dir_interactive(default_dir)
    final = chosen or default_dir
    cfg["data_dir"] = str(final)
    cfg["data_dir_prompted"] = True
    cfg["data_dir_prompted_version"] = build_version
    _save_launcher_config(cfg)
    return final


def _pick_python() -> str:
    venv_python = PROJECT_DIR / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def pick_python_executable() -> str:
    return _pick_python()


def _is_tcp_port_busy(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


def _is_backend_healthy(base_url: str) -> bool:
    health_url = f"{base_url}/health"
    try:
        with urlopen(health_url, timeout=1.0) as resp:
            return resp.status == 200
    except URLError:
        return False
    except TimeoutError:
        return False


def _running_backend_data_dir(base_url: str) -> Optional[str]:
    # Returns whatever `data_dir` the running backend reports via /health,
    # or None if it didn't (older builds) / health failed. Used to decide
    # whether an already-running backend can be reused after the user picks
    # a data directory in the launcher.
    try:
        with urlopen(f"{base_url}/health", timeout=1.0) as resp:
            if resp.status != 200:
                return None
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    value = payload.get("data_dir") if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value:
        return None
    return value


def _normalize_dir(path_str: str) -> str:
    # Resolve to an absolute, symlink-free path so two equivalent spellings
    # (e.g. "/Users/.../XEmail/data" vs "/Users/.../XEmail/data/") compare
    # equal. We don't require the dir to exist — `resolve(strict=False)`.
    try:
        return str(Path(path_str).expanduser().resolve(strict=False))
    except Exception:
        return path_str.rstrip("/")


def _kill_listener_on_port(host: str, port: int, *, timeout: float = 5.0) -> bool:
    # Best-effort: SIGTERM (then SIGKILL) whichever process is bound to
    # (host, port). Used when the launcher detects an orphan backend left
    # over from a previous install — that backend is still pointing at the
    # old XEMAIL_DATA_DIR and would happily serve requests against the
    # wrong users.json, causing "user/password error" after an upgrade.
    try:
        result = subprocess.run(
            ["lsof", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    pids = [int(tok) for tok in result.stdout.split() if tok.isdigit()]
    if not pids:
        return not _is_tcp_port_busy(host, port)

    import signal as _signal

    for pid in pids:
        try:
            os.kill(pid, _signal.SIGTERM)
        except ProcessLookupError:
            continue
        except Exception:
            continue

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _is_tcp_port_busy(host, port):
            return True
        time.sleep(0.1)

    for pid in pids:
        try:
            os.kill(pid, _signal.SIGKILL)
        except Exception:
            continue

    deadline = time.time() + 2.0
    while time.time() < deadline:
        if not _is_tcp_port_busy(host, port):
            return True
        time.sleep(0.1)
    return not _is_tcp_port_busy(host, port)


@dataclass
class BackendHandle:
    process: Optional[subprocess.Popen] = None
    reused_existing: bool = False

    def stop(self) -> None:
        if self.reused_existing or self.process is None:
            return

        if self.process.poll() is not None:
            return

        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)


class InstanceLock:
    def __init__(self, lock_file: Path):
        self._lock_file = lock_file
        self._fp = None

    def acquire(self) -> None:
        self._lock_file.parent.mkdir(parents=True, exist_ok=True)
        self._fp = self._lock_file.open("a+", encoding="utf-8")
        self._fp.seek(0)
        if fcntl is None:
            return
        try:
            fcntl.flock(self._fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fp.close()
            self._fp = None
            raise RuntimeError("检测到 XEmail 桌面版已在运行。") from exc

    def release(self) -> None:
        if self._fp is None:
            return
        if fcntl is not None:
            fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
        self._fp.close()
        self._fp = None


class TrayController:
    def __init__(self, *, enabled: bool):
        self.enabled = enabled
        self._icon = None
        self._quitting = False
        self._window = None
        self._quit_app_cb = None
        self._lock = threading.Lock()
        self._native_status_item = None
        self._native_target = None
        self._native_menu = None
        self._backend_handle: Optional[BackendHandle] = None

    def attach_backend(self, backend: "BackendHandle") -> None:
        # Used by the force-exit fallback so we don't orphan the uvicorn child.
        self._backend_handle = backend

    @property
    def is_quitting(self) -> bool:
        with self._lock:
            return self._quitting

    def start(self, *, window, quit_app_cb) -> None:
        self._window = window
        self._quit_app_cb = quit_app_cb
        if not self.enabled:
            return
        if sys.platform == "darwin" and self._start_native_macos_tray():
            return
        try:
            import pystray  # type: ignore
            from PIL import Image, ImageDraw  # type: ignore
        except ImportError:
            print(
                "[XEmail Desktop] 托盘依赖缺失，已跳过托盘模式。"
                "可安装: pip install pystray pillow",
                file=sys.stderr,
            )
            self.enabled = False
            return

        image = None
        for icon_path in self._candidate_icon_paths():
            if not icon_path.exists():
                continue
            try:
                image = Image.open(icon_path).convert("RGBA")
                # Keep tray icon visually aligned with macOS menu bar icons.
                if sys.platform == "darwin":
                    image = image.resize((18, 18))
                break
            except Exception:
                continue

        if image is None:
            # Final fallback if icon assets are unavailable.
            image = Image.new("RGBA", (64, 64), (39, 39, 43, 255))
            draw = ImageDraw.Draw(image)
            draw.rectangle((8, 16, 56, 48), fill=(70, 130, 180, 255), outline=(230, 230, 230, 255), width=2)
            draw.polygon([(8, 16), (32, 34), (56, 16)], fill=(120, 170, 220, 255))

        menu = pystray.Menu(
            pystray.MenuItem("显示窗口", self._menu_show, default=True),
            pystray.MenuItem("隐藏窗口", self._menu_hide),
            pystray.MenuItem("退出 XEmail", self._menu_quit),
        )
        self._icon = pystray.Icon("xemail", image, "XEmail", menu)
        self._icon.run_detached()

    def _candidate_icon_paths(self) -> list[Path]:
        paths: list[Path] = []
        exe = Path(sys.executable).resolve()
        for parent in exe.parents:
            if parent.name == "Contents":
                paths.append(parent / "Resources" / "app" / "web" / "tray_icon.png")
                paths.append(parent / "Resources" / "XEmail.icns")
                break
        paths.extend(
            [
                PROJECT_DIR / "web" / "tray_icon.png",
                PROJECT_DIR / "XEmail.icns",
                PROJECT_DIR / "web" / "logo.png",
                PROJECT_DIR / "web" / "favicon.png",
            ]
        )
        return paths

    def _start_native_macos_tray(self) -> bool:
        try:
            import objc  # type: ignore
            from AppKit import (  # type: ignore
                NSImage,
                NSStatusBar,
                NSVariableStatusItemLength,
                NSObject,
                NSMakeSize,
                NSMenu,
                NSMenuItem,
            )
        except Exception:
            return False

        controller = self

        # PyObjC: declare explicit ObjC method signatures via `objc.selector` so
        # the action dispatcher can reliably resolve the selectors `onShow:`,
        # `onHide:`, `onQuit:`. Without explicit signatures some PyObjC versions
        # silently fail to register the method as a valid action target.
        def _make_action(py_callable):
            return objc.selector(py_callable, signature=b"v@:@")

        class _TrayTarget(NSObject):
            _controller = None

            def initWithController_(self, c):
                self = objc.super(_TrayTarget, self).init()
                if self is None:
                    return None
                self._controller = c
                return self

            def onShow_(self, _sender):
                self._controller._menu_show(None, None)

            def onHide_(self, _sender):
                self._controller._menu_hide(None, None)

            def onQuit_(self, _sender):
                self._controller._menu_quit(None, None)

            onShow_ = _make_action(onShow_)
            onHide_ = _make_action(onHide_)
            onQuit_ = _make_action(onQuit_)

        status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        if status_item is None:
            return False

        target = _TrayTarget.alloc().initWithController_(controller)
        button = status_item.button()
        if button is not None:
            icon_set = False
            for p in self._candidate_icon_paths():
                if not p.exists():
                    continue
                try:
                    ns_img = NSImage.alloc().initWithContentsOfFile_(str(p))
                    if ns_img is None:
                        continue
                    ns_img.setSize_(NSMakeSize(18.0, 18.0))
                    ns_img.setTemplate_(False)
                    button.setImage_(ns_img)
                    icon_set = True
                    break
                except Exception:
                    continue
            if not icon_set:
                button.setTitle_("XEmail")
            button.setToolTip_("XEmail")

        menu = NSMenu.alloc().init()
        # autoenablesItems must be False, otherwise AppKit auto-disables items
        # whose target doesn't claim to validate them — which silently makes
        # clicks a no-op (a likely culprit for "menu shows but quit does
        # nothing").
        menu.setAutoenablesItems_(False)

        show_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "显示窗口", "onShow:", ""
        )
        show_item.setTarget_(target)
        show_item.setEnabled_(True)

        hide_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "隐藏窗口", "onHide:", ""
        )
        hide_item.setTarget_(target)
        hide_item.setEnabled_(True)

        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "退出 XEmail", "onQuit:", ""
        )
        quit_item.setTarget_(target)
        quit_item.setEnabled_(True)

        menu.addItem_(show_item)
        menu.addItem_(hide_item)
        menu.addItem_(NSMenuItem.separatorItem())
        menu.addItem_(quit_item)

        # Standard macOS menubar-app pattern: attach the menu directly to the
        # status item so AppKit handles both left- and right-click reliably.
        # The previous attempt to keep the "click shows window" UX via
        # popUpStatusItemMenu_ + a custom button action turned out to drop the
        # menu items' actions on some macOS versions, which is why the Quit
        # item appeared to do nothing.
        status_item.setMenu_(menu)

        self._native_status_item = status_item
        self._native_target = target
        self._native_menu = menu
        return True

    def stop(self) -> None:
        if self._native_status_item is not None and sys.platform == "darwin":
            try:
                from AppKit import NSStatusBar  # type: ignore

                NSStatusBar.systemStatusBar().removeStatusItem_(self._native_status_item)
            except Exception:
                pass
            self._native_status_item = None
            self._native_target = None

        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None

    def request_close_to_tray(self) -> bool:
        if self.is_quitting:
            return False
        # Just hide the window. We deliberately do NOT call minimize(): on
        # macOS minimize creates a separate window thumbnail in the Dock,
        # which is what the user saw as "the close button minimizes to a
        # different Dock slot". Plain hide() preserves the standard pattern
        # of "close window, app keeps running, single Dock icon with active
        # dot underneath".
        return self._invoke_window_api("hide")

    def _invoke_window_api(self, action: str) -> bool:
        fn = getattr(self._window, action, None)
        if fn is None:
            return False
        # pystray callbacks are on a background thread; pywebview window ops on
        # macOS must be dispatched onto the Cocoa main loop.
        if sys.platform == "darwin":
            try:
                from PyObjCTools import AppHelper  # type: ignore

                AppHelper.callAfter(fn)
                return True
            except Exception:
                pass
        try:
            fn()
            return True
        except Exception:
            return False

    def _menu_show(self, _icon, _item) -> None:
        if sys.platform == "darwin":
            try:
                from AppKit import NSApplication  # type: ignore

                NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            except Exception:
                pass
        self._invoke_window_api("show")
        self._invoke_window_api("restore")

    def _menu_hide(self, _icon, _item) -> None:
        self._invoke_window_api("hide")

    def _menu_quit(self, _icon, _item) -> None:
        with self._lock:
            self._quitting = True

        # 1) Kill the backend uvicorn child SYNCHRONOUSLY first. On macOS the
        #    parent process exiting does NOT take orphaned children with it, so
        #    if we skip this the user sees "the app didn't quit" because the
        #    backend keeps holding port 8000.
        self._kill_backend_now()

        # 2) Start the hard-exit watchdog before anything that might hang.
        threading.Thread(target=self._force_exit_fallback, daemon=True).start()

        # 3) Best-effort graceful UI shutdown.
        try:
            self.stop()
        except Exception:
            pass

        quit_invoked = False
        if self._quit_app_cb is not None:
            if sys.platform == "darwin":
                try:
                    from PyObjCTools import AppHelper  # type: ignore

                    AppHelper.callAfter(self._quit_app_cb)
                    quit_invoked = True
                except Exception:
                    pass
            if not quit_invoked:
                try:
                    self._quit_app_cb()
                    quit_invoked = True
                except Exception:
                    pass
        if not quit_invoked:
            self._invoke_window_api("destroy")

        if sys.platform == "darwin":
            try:
                from AppKit import NSApplication  # type: ignore
                from PyObjCTools import AppHelper  # type: ignore

                AppHelper.callAfter(NSApplication.sharedApplication().terminate_, None)
            except Exception:
                pass

    def _kill_backend_now(self) -> None:
        backend = self._backend_handle
        if backend is None:
            return
        proc = getattr(backend, "process", None)
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=0.4)
        except Exception:
            pass
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass

    def _force_exit_fallback(self) -> None:
        # Give the graceful path ~0.8s; if we're still alive, the Cocoa run
        # loop is wedged (pywebview's WKWebView shutdown sometimes refuses to
        # return from terminate:). Kill children, then SIGKILL ourselves —
        # belt-and-braces because the user already saw a softer exit not work.
        time.sleep(0.8)
        try:
            self._kill_backend_now()
        except Exception:
            pass
        try:
            import signal

            os.kill(os.getpid(), signal.SIGKILL)
        except Exception:
            pass
        os._exit(0)


_macos_helper_refs: list = []  # keep PyObjC observers alive for the app's lifetime
_macos_reopen_delegate_installed = False


def _force_kill_after(delay_seconds: float) -> None:
    # Watchdog for AppKit's terminate: — WKWebView teardown occasionally wedges
    # there and the process otherwise hangs forever with the Dock icon's dot
    # still lit. Mirrors TrayController._force_exit_fallback for the
    # Dock-right-click-Quit / Cmd-Q paths which go through WillTerminate
    # instead of the tray Quit handler.
    time.sleep(delay_seconds)
    try:
        import signal

        os.kill(os.getpid(), signal.SIGKILL)
    except Exception:
        pass
    os._exit(0)


def _called_from_app_terminate() -> bool:
    # True if our window.events.closing handler is firing because pywebview's
    # applicationShouldTerminate_ is asking each window whether the app can
    # quit. In that path we want to return True (let the app quit) instead
    # of the default red-button behavior (hide to tray).
    import traceback

    for frame in traceback.extract_stack():
        name = frame.name or ""
        if "applicationShouldTerminate" in name:
            return True
    return False


def _kill_backend_sync(backend) -> None:
    if backend is None:
        return
    proc = getattr(backend, "process", None)
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=0.4)
    except Exception:
        pass
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass


def _install_macos_dock_reopen_handler(window) -> None:
    """Show the pywebview window again when the user clicks the Dock icon.

    NSApplication posts NSApplicationDidBecomeActive whenever the app
    foregrounds, including the "user clicked our Dock icon" case. If our
    window is currently hidden (because the user closed it), bring it back.
    """
    if sys.platform != "darwin":
        return
    try:
        import objc  # type: ignore
        from AppKit import NSApplication  # type: ignore
        from Foundation import NSNotificationCenter, NSObject  # type: ignore
    except Exception:
        return

    class _ReopenObserver(NSObject):
        _window = None

        def initWithWindow_(self, w):
            self = objc.super(_ReopenObserver, self).init()
            if self is None:
                return None
            self._window = w
            return self

        def onActivate_(self, _notification):
            w = self._window
            if w is None:
                return
            try:
                w.show()
            except Exception:
                pass
            try:
                w.restore()
            except Exception:
                pass

        onActivate_ = objc.selector(onActivate_, signature=b"v@:@")

    observer = _ReopenObserver.alloc().initWithWindow_(window)
    NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
        observer,
        "onActivate:",
        "NSApplicationDidBecomeActiveNotification",
        NSApplication.sharedApplication(),
    )
    _macos_helper_refs.append(observer)


def _patch_pywebview_app_delegate(window) -> None:
    # We need two things on macOS that pywebview's AppDelegate doesn't give us:
    #   - applicationShouldHandleReopen:hasVisibleWindows: → bring back the
    #     hidden window when the user clicks the Dock icon (the canonical
    #     hook; NSApplicationDidBecomeActive doesn't fire when the app
    #     stayed foregrounded, e.g. after red-button close).
    # We do this by adding the method directly to pywebview's existing
    # AppDelegate class (objc.classAddMethods), NOT by swapping the NSApp
    # delegate. Swapping was producing a second Dock entry (the "exec"-icon
    # ghost next to the proper XEmail icon) — macOS evidently treats
    # setDelegate_ on a custom NSObject as "this is a different app".
    #
    # We do NOT patch applicationShouldTerminate_ here: pywebview already
    # has one, and class_addMethod won't replace existing methods. The Quit
    # veto problem is handled in _on_closing by walking the call stack to
    # detect when we're being invoked from inside applicationShouldTerminate.
    global _macos_reopen_delegate_installed
    if sys.platform != "darwin":
        return
    if _macos_reopen_delegate_installed:
        return
    try:
        import objc  # type: ignore
        from AppKit import NSApplication  # type: ignore
        from webview.platforms.cocoa import BrowserView  # type: ignore
    except Exception:
        return

    cls = BrowserView.AppDelegate
    win_ref = window

    def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, _has_visible):
        try:
            win_ref.show()
        except Exception:
            pass
        try:
            win_ref.restore()
        except Exception:
            pass
        try:
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        except Exception:
            pass
        return True

    reopen_method = objc.selector(
        applicationShouldHandleReopen_hasVisibleWindows_,
        signature=objc._C_NSBOOL + b"@:@" + objc._C_NSBOOL,
    )
    try:
        objc.classAddMethods(cls, [reopen_method])
    except Exception:
        pass
    _macos_reopen_delegate_installed = True


def _install_macos_terminate_cleanup(backend) -> None:
    """Make terminate: (Cmd-Q / Dock right-click → Quit / OS quit) actually quit.

    Two failure modes in the default path:
      - macOS does not reap our uvicorn child when it kills us, so port 8000
        stays held by an orphan after quitting.
      - WKWebView teardown inside terminate: occasionally wedges; AppKit
        never reaches exit(0) and the user sees "Quit does nothing".

    We synchronously kill the backend in WillTerminate (~0.4s budget) and arm
    a SIGKILL watchdog. The tray Quit path already has its own watchdog
    (TrayController._force_exit_fallback); this covers every other terminate
    path.
    """
    if sys.platform != "darwin":
        return
    try:
        import objc  # type: ignore
        from AppKit import NSApplication  # type: ignore
        from Foundation import NSNotificationCenter, NSObject  # type: ignore
    except Exception:
        return

    class _TerminateObserver(NSObject):
        _backend = None

        def initWithBackend_(self, b):
            self = objc.super(_TerminateObserver, self).init()
            if self is None:
                return None
            self._backend = b
            return self

        def onWillTerminate_(self, _notification):
            try:
                _kill_backend_sync(self._backend)
            except Exception:
                pass
            threading.Thread(
                target=_force_kill_after, args=(1.5,), daemon=True
            ).start()

        onWillTerminate_ = objc.selector(onWillTerminate_, signature=b"v@:@")

    observer = _TerminateObserver.alloc().initWithBackend_(backend)
    NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
        observer,
        "onWillTerminate:",
        "NSApplicationWillTerminateNotification",
        NSApplication.sharedApplication(),
    )
    _macos_helper_refs.append(observer)


def _set_macos_activation_policy_regular() -> None:
    # Behave like a normal Mac app: dock icon + Cmd-Tab switcher entry.
    # We set this explicitly because pywebview's import path can leave the
    # activation policy in an unexpected state on some macOS versions.
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyRegular  # type: ignore

        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyRegular
        )
    except Exception:
        pass


def _install_macos_main_menu() -> None:
    # Install a standard application menu bar with an Edit menu. Without this,
    # AppKit has nowhere to route Cmd-C / Cmd-X / Cmd-V / Cmd-A key equivalents,
    # so the user cannot copy selected text from the email content pane (or any
    # other webview content). pywebview does not install one by default.
    if sys.platform != "darwin":
        return
    try:
        from AppKit import (  # type: ignore
            NSApplication,
            NSEventModifierFlagCommand,
            NSEventModifierFlagShift,
            NSMenu,
            NSMenuItem,
        )
    except Exception:
        return

    app = NSApplication.sharedApplication()
    if app.mainMenu() is not None:
        # Some pywebview versions / future updates may install one — don't
        # overwrite, just make sure an Edit menu is present.
        existing = app.mainMenu()
        for i in range(existing.numberOfItems()):
            sub = existing.itemAtIndex_(i).submenu()
            if sub is not None and sub.title() in ("Edit", "编辑"):
                return

    main_menu = NSMenu.alloc().init()

    # First menu slot is the application menu; macOS replaces the title with
    # the bundle name at display time, but we still need to provide submenu
    # items so Quit picks up its Cmd-Q shortcut.
    app_item = NSMenuItem.alloc().init()
    main_menu.addItem_(app_item)
    app_menu = NSMenu.alloc().initWithTitle_("XEmail")
    hide_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "隐藏 XEmail", "hide:", "h"
    )
    app_menu.addItem_(hide_item)
    hide_others = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "隐藏其他", "hideOtherApplications:", "h"
    )
    hide_others.setKeyEquivalentModifierMask_(
        NSEventModifierFlagCommand | NSEventModifierFlagShift
    )
    app_menu.addItem_(hide_others)
    app_menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "显示全部", "unhideAllApplications:", ""
    ))
    app_menu.addItem_(NSMenuItem.separatorItem())
    app_menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "退出 XEmail", "terminate:", "q"
    ))
    app_item.setSubmenu_(app_menu)

    # Edit menu — the actual reason we install a menu bar. The selectors
    # below are the standard AppKit responder-chain actions; WKWebView
    # implements them natively for text selection / form fields.
    edit_item = NSMenuItem.alloc().init()
    main_menu.addItem_(edit_item)
    edit_menu = NSMenu.alloc().initWithTitle_("编辑")
    edit_menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "撤销", "undo:", "z"
    ))
    redo_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "重做", "redo:", "Z"
    )
    redo_item.setKeyEquivalentModifierMask_(
        NSEventModifierFlagCommand | NSEventModifierFlagShift
    )
    edit_menu.addItem_(redo_item)
    edit_menu.addItem_(NSMenuItem.separatorItem())
    edit_menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "剪切", "cut:", "x"
    ))
    edit_menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "复制", "copy:", "c"
    ))
    edit_menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "粘贴", "paste:", "v"
    ))
    edit_menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "全选", "selectAll:", "a"
    ))
    edit_item.setSubmenu_(edit_menu)

    app.setMainMenu_(main_menu)


def _set_macos_app_icon(icon_candidates: Optional[list[Path]] = None) -> None:
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSImage  # type: ignore
    except Exception:
        return
    candidates = icon_candidates or []
    if not candidates:
        exe = Path(sys.executable).resolve()
        for parent in exe.parents:
            if parent.name == "Contents":
                candidates.append(parent / "Resources" / "app" / "web" / "tray_icon.png")
                candidates.append(parent / "Resources" / "XEmail.icns")
                break
        candidates.extend(
            [
                PROJECT_DIR / "web" / "tray_icon.png",
                PROJECT_DIR / "XEmail.icns",
                PROJECT_DIR / "web" / "logo.png",
            ]
        )
    for p in candidates:
        if not p.exists():
            continue
        try:
            img = NSImage.alloc().initWithContentsOfFile_(str(p))
            if img is None:
                continue
            NSApplication.sharedApplication().setApplicationIconImage_(img)
            return
        except Exception:
            continue


def _activate_window_and_focus_input(window) -> None:
    # Accessory apps (LSUIElement=1) don't auto-become the active app, so on
    # first launch the webview can appear behind everything else and refuse
    # keyboard input. Force-activate the app and make pywebview's NSWindow
    # the key window.
    if sys.platform == "darwin":
        try:
            from AppKit import NSApplication  # type: ignore

            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        except Exception:
            pass

        # Reach into pywebview's Cocoa window if we can, and force it key.
        # pywebview's internal attribute names vary across versions, so we try
        # a few — none of these failing should break anything.
        try:
            from PyObjCTools import AppHelper  # type: ignore

            def _raise_native_window():
                candidate_attrs = ("native", "_browser", "browser")
                native = None
                for attr in candidate_attrs:
                    obj = getattr(window, attr, None)
                    if obj is None:
                        continue
                    win_attr = getattr(obj, "window", None)
                    if callable(win_attr):
                        try:
                            native = win_attr()
                        except Exception:
                            native = None
                    else:
                        native = win_attr
                    if native is not None:
                        break
                if native is not None:
                    try:
                        native.makeKeyAndOrderFront_(None)
                    except Exception:
                        pass

            AppHelper.callAfter(_raise_native_window)
        except Exception:
            pass

    script = """
    (() => {
      const fields = Array.from(document.querySelectorAll('input:not([disabled])'));
      const target = fields.find((el) => {
        const style = window.getComputedStyle(el);
        return style.display !== 'none' && style.visibility !== 'hidden';
      });
      if (target) {
        target.focus();
        target.click();
        target.select?.();
      }
      window.focus?.();
      return true;
    })();
    """
    for _ in range(6):
        try:
            window.evaluate_js(script)
        except Exception:
            pass
        time.sleep(0.2)


def _has_user_state(data_dir: Path) -> bool:
    # After the JSON→SQLite migration, real users have only `xemail.db`. The
    # JSON names are kept in this probe so the desktop launcher can still
    # detect "you have data from a pre-SQLite install in the legacy location"
    # and copy it forward; the backend's storage layer then migrates it on
    # first import.
    return any(
        (data_dir / name).exists()
        for name in ("xemail.db", "config.json", "users.json", "emails.json", "prompts.json")
    )


def _maybe_migrate_legacy_data(target_data_dir: Path) -> None:
    if _has_user_state(target_data_dir):
        return
    if not LEGACY_DATA_DIR.exists() or not _has_user_state(LEGACY_DATA_DIR):
        return

    target_data_dir.mkdir(parents=True, exist_ok=True)
    ignore_names = {"server.pid", "server.log", "desktop-backend.log", ".gitkeep"}

    for src in LEGACY_DATA_DIR.iterdir():
        if src.name in ignore_names:
            continue
        dst = target_data_dir / src.name
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)


def _start_backend(base_url: str, *, env: dict[str, str]) -> BackendHandle:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    # If something is already serving this port and responds to /health,
    # reuse it — UNLESS it's bound to a different XEMAIL_DATA_DIR than the
    # one the user just chose. That mismatch case is the upgrade-install
    # bug: an orphan uvicorn from the previous version is still on port
    # 8000, pointing at the old data dir, so login fails against the
    # user's actual users.json. In that case kill the orphan and start
    # a fresh backend bound to the chosen directory.
    if _is_tcp_port_busy(HOST, PORT):
        if _is_backend_healthy(base_url):
            intended = _normalize_dir(env.get("XEMAIL_DATA_DIR", ""))
            running = _running_backend_data_dir(base_url)
            running_norm = _normalize_dir(running) if running else ""
            if not intended or not running_norm or intended == running_norm:
                return BackendHandle(reused_existing=True)
            # Mismatch: stale backend from a previous install/version.
            if not _kill_listener_on_port(HOST, PORT):
                raise RuntimeError(
                    f"端口 {PORT} 上有另一个 XEmail 后端 (data_dir={running})，"
                    f"无法切换到所选目录 ({intended})。请手动结束该进程后重试。"
                )
        else:
            raise RuntimeError(
                f"端口 {PORT} 已被占用，但不是可用的 XEmail 后端。"
                "请先释放端口或设置 XEMAIL_PORT。"
            )

    python_bin = _pick_python()
    log_fp = LOG_FILE.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            python_bin,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            HOST,
            "--port",
            str(PORT),
        ],
        cwd=str(PROJECT_DIR),
        env=env,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
    )
    log_fp.close()

    deadline = time.time() + HEALTH_TIMEOUT_SECONDS
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"后端启动失败（退出码 {process.returncode}）。请查看日志：{LOG_FILE}"
            )
        if _is_backend_healthy(base_url):
            return BackendHandle(process=process, reused_existing=False)
        time.sleep(HEALTH_POLL_INTERVAL_SECONDS)

    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
    raise RuntimeError(f"后端启动超时。请查看日志：{LOG_FILE}")


class JsApi:
    """Methods exposed to the embedded UI via `window.pywebview.api.*`. Used
    by the attachment-download flow to surface a native folder picker —
    HTML5 has no cross-platform "pick a directory" affordance, and the
    backend can't pop a native dialog because it runs outside the AppKit
    event loop. So the launcher (which IS the AppKit app) takes the call,
    runs the dialog, and returns the chosen path."""

    def __init__(self) -> None:
        self._window = None

    def set_window(self, window) -> None:
        self._window = window

    def choose_save_folder(self, default_dir: Optional[str] = None) -> Optional[str]:
        # Prefer pywebview's portable folder-dialog API; it dispatches to
        # the right native UI on each platform (NSOpenPanel on macOS,
        # tkinter on others). We accept a default starting directory so
        # the dialog opens where users expect (their Downloads folder).
        import webview  # type: ignore
        win = self._window
        if win is None:
            return None
        start_dir = default_dir or str(Path.home() / "Downloads")
        try:
            result = win.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=start_dir,
                allow_multiple=False,
            )
        except Exception:
            return None
        if not result:
            return None
        if isinstance(result, (list, tuple)):
            return str(result[0]) if result else None
        return str(result)


def run() -> None:
    try:
        import webview  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "缺少 pywebview 依赖。请先执行：pip install -r requirements.txt"
        ) from exc

    base_url = f"http://{HOST}:{PORT}"
    backend_data_dir = _resolve_backend_data_dir()
    _maybe_migrate_legacy_data(backend_data_dir)
    backend_env = dict(os.environ)
    backend_env["XEMAIL_DATA_DIR"] = str(backend_data_dir)

    lock = InstanceLock(LOCK_FILE)
    tray = TrayController(enabled=_resolve_enable_tray_for_data_dir(backend_data_dir))
    # Behave as a normal Mac app: dock icon present, standard activation.
    # We deliberately do NOT call setApplicationIconImage_ here: the bundle's
    # Info.plist already declares CFBundleIconFile=XEmail.icns, and that
    # multi-resolution icns is what macOS should use for the Dock. Overriding
    # at runtime with a single-resolution PNG was producing a second, oversized
    # Dock entry with a white border alongside the real icon.
    _set_macos_activation_policy_regular()
    _install_macos_main_menu()
    # Drop any stale WKWebView HTTP cache from a previous version so the
    # freshly-installed web/ assets actually paint (no more "the new button
    # isn't there" after an upgrade).
    _maybe_clear_webview_cache_on_upgrade()
    lock.acquire()
    try:
        backend = _start_backend(base_url, env=backend_env)
        try:
            js_api = JsApi()
            window = webview.create_window(
                title="XEmail",
                url=f"{base_url}/login",
                min_size=(1360, 820),
                width=1560,
                height=980,
                js_api=js_api,
            )
            js_api.set_window(window)
            def _on_window_loaded():
                if sys.platform == "darwin":
                    try:
                        from PyObjCTools import AppHelper  # type: ignore

                        AppHelper.callAfter(_patch_pywebview_app_delegate, window)
                    except Exception:
                        pass
                threading.Thread(
                    target=_activate_window_and_focus_input,
                    args=(window,),
                    daemon=True,
                ).start()

            window.events.loaded += _on_window_loaded

            def _graceful_quit() -> None:
                # Tray quit must shut down the backend subprocess too —
                # otherwise the uvicorn child keeps holding the port and
                # the user observes "the app didn't quit".
                try:
                    backend.stop()
                except Exception:
                    pass
                try:
                    window.destroy()
                except Exception:
                    pass

            tray.attach_backend(backend)
            tray.start(window=window, quit_app_cb=_graceful_quit)

            # Standard Mac app behavior: clicking the window's red close
            # button hides the window but keeps the app running, leaving a
            # single Dock icon with the active dot underneath. The app is
            # quit only via Cmd-Q / Dock right-click → Quit / tray Quit.
            #
            # pywebview routes its applicationShouldTerminate_ THROUGH this
            # same closing event (cocoa.py:1310 should_close), so if we
            # always return False here, Cmd-Q / Dock-Quit get vetoed too.
            # Detect that path by stack-walking and let it through.
            def _on_closing(*_args):
                if tray.is_quitting:
                    return True
                if sys.platform == "darwin" and _called_from_app_terminate():
                    return True
                tray.request_close_to_tray()
                return False

            window.events.closing += _on_closing
            window.events.closed += lambda: backend.stop()

            # When the user clicks the Dock icon while the window is hidden,
            # bring it back to the front.
            _install_macos_dock_reopen_handler(window)
            # When macOS sends a normal terminate: (Cmd-Q, Dock right-click
            # Quit, OS shutdown) make sure we stop the uvicorn child first
            # and arm a SIGKILL watchdog in case WKWebView teardown wedges.
            _install_macos_terminate_cleanup(backend)

            # private_mode=False + an explicit storage_path persists
            # cookies / localStorage to disk so the user's logged-in state
            # survives quitting the app. Without this, WKWebView throws
            # the session cookie away on every restart and the user lands
            # back on the login screen.
            webview_storage = APP_ROOT / "webview_storage"
            try:
                webview_storage.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            webview.start(
                private_mode=False,
                storage_path=str(webview_storage),
            )
        finally:
            tray.stop()
            backend.stop()
    finally:
        lock.release()


def main() -> None:
    try:
        run()
    except Exception as exc:
        print(f"[XEmail Desktop] 启动失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
