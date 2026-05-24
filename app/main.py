# Copyright (c) 2026 Peking University & Beijing Siliconheart Technology Co., Ltd.
# XEmail is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#          http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import json
import mimetypes
import os
import queue
import signal
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from app.models import (
    Account,
    AccountCreate,
    AccountUpdate,
    AdminUserCreate,
    Attachment,
    AuthStatus,
    ClassifyUnsortedResult,
    Contact,
    ContactCreate,
    ContactUpdate,
    DesktopSettingsStatus,
    DesktopSettingsUpdate,
    Experience,
    ExperienceCreate,
    ExperienceUpdate,
    ImportanceToggleRequest,
    ImportanceToggleResult,
    ComposeDraftRequest,
    ComposeDraftResult,
    ReplyGenerationRequest,
    ReplyGenerationResult,
    ReplySummaryResult,
    ConfigPayload,
    DraftPayload,
    DraftRecord,
    EmailRecord,
    EmailUpdate,
    FixedRule,
    FixedRuleCompileRequest,
    FixedRuleCompileResponse,
    FixedRuleCreate,
    FixedRuleReorder,
    FixedRuleUpdate,
    FixedRuleValidateRequest,
    FixedRuleValidateResponse,
    LlmConfigStatus,
    LlmConfigUpdate,
    LlmFieldConfig,
    PasswordReset,
    PromptsView,
    ReceiveResult,
    RecategorizeRequest,
    RecategorizeResult,
    SendEmailRequest,
    SendResult,
    SentRecord,
    SentUpdate,
    SyncSettings,
    SystemModeStatus,
    SystemModeUpdate,
    SystemPromptUpdate,
    User,
    UserLogin,
    UserPrompt,
    UserPromptCreate,
    UserPromptUpdate,
    UserRegister,
)
from app.services.auth import (
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    current_user,
    hash_password,
    make_session_token,
    optional_user,
    require_admin,
    verify_password,
)

from app.services.email_client import (
    UNCLASSIFIED,
    classify_email_record,
    dedupe_by_message_id,
    diagnose_email_connection,
    imap_append_sent,
    imap_expunge_uid,
    imap_move_uid,
    imap_set_flags,
    repair_email_received_times,
    receive_emails,
    send_email,
)
from app.storage import (
    DEFAULT_FOLDERS,
    UNCLASSIFIED_FOLDER,
    add_account,
    add_contact,
    add_experience,
    add_fixed_rule,
    add_prompt,
    add_user,
    assign_orphan_accounts_to,
    clear_account_sync_state,
    copy_attachments_folder,
    delete_account,
    delete_attachment_file,
    delete_attachments_folder,
    delete_contact,
    delete_experience,
    delete_fixed_rule,
    delete_prompt,
    delete_user,
    find_contact_by_email,
    get_account,
    get_account_sync_state,
    get_attachment_path,
    get_contact,
    get_experience,
    get_fixed_rule,
    get_prompt,
    get_user,
    get_user_active_account_id,
    get_user_by_username,
    has_any_user,
    list_accounts,
    list_attachments_meta,
    list_contacts_for_account,
    list_experiences_for_account,
    list_fixed_rules_for_account,
    list_prompts_for_account,
    list_users,
    move_attachments_folder,
    read_drafts,
    read_desktop_settings,
    read_emails,
    import_folders_for_account,
    import_prompts_for_account,
    read_field_config_for_account,
    read_folders,
    read_sent,
    read_system_mode,
    read_system_spam_prompt,
    reorder_fixed_rules,
    save_attachment_bytes,
    set_user_active_account,
    update_account,
    update_account_sync_state_entry,
    update_contact,
    update_experience,
    update_fixed_rule,
    update_prompt,
    update_user,
    write_drafts,
    write_desktop_settings,
    write_emails,
    write_field_config_for_account,
    write_folders,
    write_llm_api_key,
    write_sent,
    write_system_mode,
    write_system_spam_prompt,
)
from app.services.spam_filter import (
    DEFAULT_SYSTEM_PROMPT,
    _MODEL as LLM_MODEL_NAME,
    distill_category_experience,
    distill_experience,
    generate_compose_draft,
    generate_reply,
    is_api_key_configured,
    is_predominantly_english,
    summarize_email_for_reply,
)

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

app = FastAPI(title="XEmail", version="0.1.0")


class ConditionalGZipMiddleware(GZipMiddleware):
    """GZip everything EXCEPT request paths listed in `skip_prefixes`.

    Why: vanilla GZipMiddleware compresses by size only, with no regard for
    content type. Binary attachments (PDF / JPG / ZIP / Office docs) are
    already compressed, so re-gzipping them wastes CPU and — critically —
    causes the middleware to drop the upstream `Content-Length` and switch
    to chunked transfer encoding. On the production deployment this made
    Chrome stall in the `.crdownload` "incomplete" state because the
    response body no longer matched its declared length.

    Skipping the attachment endpoint preserves the original
    `Content-Length` + raw bytes, so downloads finish cleanly. JSON / HTML
    responses (the actual bandwidth win) keep their gzip.
    """

    def __init__(self, app, *, minimum_size: int = 500, compresslevel: int = 9, skip_prefixes=()):
        super().__init__(app, minimum_size=minimum_size, compresslevel=compresslevel)
        self.skip_prefixes = tuple(skip_prefixes)

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if any(path.startswith(p) for p in self.skip_prefixes):
                await self.app(scope, receive, send)
                return
        await super().__call__(scope, receive, send)


# Gzip everything >= 500B EXCEPT:
#   1. attachment downloads — pre-compressed binaries; double-compressing
#      wastes CPU and breaks Content-Length (see ConditionalGZip docstring).
#   2. NDJSON streaming endpoints — Starlette's GZipMiddleware backs onto
#      `gzip.GzipFile.write()`, which buffers in zlib's internal block
#      until a compression boundary is hit. That defeats real-time
#      streaming: per-email "classified" events get pooled and arrive in
#      one burst at the end, so the user sees no progress mid-fetch. The
#      JSON lines are small (a few hundred bytes each) anyway — gzip
#      barely helps. Keep them uncompressed and the browser sees each
#      line as soon as the server writes it.
app.add_middleware(
    ConditionalGZipMiddleware,
    minimum_size=500,
    skip_prefixes=(
        "/api/attachments/",
        "/api/receive/stream",
        "/api/classify-unsorted/stream",
        "/api/reclassify-all/stream",
        "/api/debug/repair-email-times/stream",
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
WEB_INDEX = WEB_DIR / "index.html"
WEB_SETTINGS = WEB_DIR / "settings.html"


def _humanize_email_error(prefix: str, exc: Exception) -> str:
    raw = str(exc)
    lower = raw.lower()

    if "unsafe login" in lower:
        return (
            f"{prefix}失败：邮箱服务商拦截了本次客户端登录（Unsafe Login）。"
            "请在邮箱网页端开启 IMAP/SMTP，使用客户端授权码（非网页登录密码），"
            "并完成安全验证后重试。"
        )

    if "authentication failed" in lower or "login failed" in lower:
        return (
            f"{prefix}失败：账号认证未通过。请检查邮箱地址、授权码/密码是否正确，"
            "并确认 IMAP/SMTP 已开启。"
        )

    if "timed out" in lower or "timeout" in lower:
        return f"{prefix}失败：连接邮箱服务器超时，请检查网络或服务器地址与端口配置。"

    if "name or service not known" in lower or "nodename nor servname provided" in lower:
        return f"{prefix}失败：邮箱服务器地址无法解析，请检查 SMTP/IMAP 主机名是否正确。"

    if "connection refused" in lower:
        return f"{prefix}失败：邮箱服务器拒绝连接，请检查端口、SSL/STARTTLS 配置是否匹配。"

    return f"{prefix}失败: {raw}"


@app.get("/")
def home() -> FileResponse:
    if not WEB_INDEX.exists():
        raise HTTPException(status_code=404, detail="Web page not found.")
    return FileResponse(WEB_INDEX)


@app.get("/settings")
def settings_page() -> FileResponse:
    if not WEB_SETTINGS.exists():
        raise HTTPException(status_code=404, detail="Settings page not found.")
    return FileResponse(WEB_SETTINGS)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


WEB_LOGIN = WEB_DIR / "login.html"
WEB_ADMIN = WEB_DIR / "admin.html"
WEB_CONTACTS = WEB_DIR / "contacts.html"


@app.get("/login")
def login_page() -> FileResponse:
    if not WEB_LOGIN.exists():
        raise HTTPException(status_code=404, detail="Login page not found.")
    return FileResponse(WEB_LOGIN)


@app.get("/admin")
def admin_page() -> FileResponse:
    # Auth check happens client-side via /api/auth/me; this just serves the HTML.
    if not WEB_ADMIN.exists():
        raise HTTPException(status_code=404, detail="Admin page not found.")
    return FileResponse(WEB_ADMIN)


@app.get("/contacts")
def contacts_page() -> FileResponse:
    if not WEB_CONTACTS.exists():
        raise HTTPException(status_code=404, detail="Contacts page not found.")
    return FileResponse(WEB_CONTACTS)


WEB_FAVICON = WEB_DIR / "favicon.svg"
WEB_LOGO = WEB_DIR / "logo.svg"


@app.get("/favicon.svg")
def favicon_svg() -> FileResponse:
    if not WEB_FAVICON.exists():
        raise HTTPException(status_code=404, detail="favicon not found")
    return FileResponse(WEB_FAVICON, media_type="image/svg+xml")


@app.get("/favicon.ico")
def favicon_ico() -> FileResponse:
    # Browsers still probe /favicon.ico; we just serve the SVG so any tab,
    # bookmark, or PWA install sees the same artwork.
    if not WEB_FAVICON.exists():
        raise HTTPException(status_code=404, detail="favicon not found")
    return FileResponse(WEB_FAVICON, media_type="image/svg+xml")


@app.get("/logo.svg")
def logo_svg() -> FileResponse:
    if not WEB_LOGO.exists():
        raise HTTPException(status_code=404, detail="logo not found")
    return FileResponse(WEB_LOGO, media_type="image/svg+xml")


WEB_I18N = WEB_DIR / "i18n.js"
WEB_RUNTIME_STATUS_JS = WEB_DIR / "runtime_status.js"
WEB_RUNTIME_STATUS_CSS = WEB_DIR / "runtime_status.css"
BUILD_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


@app.get("/i18n.js")
def i18n_js() -> FileResponse:
    if not WEB_I18N.exists():
        raise HTTPException(status_code=404, detail="i18n bundle not found")
    return FileResponse(WEB_I18N, media_type="application/javascript")


@app.get("/runtime_status.js")
def runtime_status_js() -> FileResponse:
    if not WEB_RUNTIME_STATUS_JS.exists():
        raise HTTPException(status_code=404, detail="runtime status js not found")
    return FileResponse(WEB_RUNTIME_STATUS_JS, media_type="application/javascript")


@app.get("/runtime_status.css")
def runtime_status_css() -> FileResponse:
    if not WEB_RUNTIME_STATUS_CSS.exists():
        raise HTTPException(status_code=404, detail="runtime status css not found")
    return FileResponse(WEB_RUNTIME_STATUS_CSS, media_type="text/css")


# -------- auth endpoints --------

def _set_session_cookie(resp: Response, user_id: str) -> None:
    resp.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=make_session_token(user_id),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(resp: Response) -> None:
    resp.delete_cookie(SESSION_COOKIE_NAME, path="/")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.get("/api/auth/status", response_model=AuthStatus)
def auth_status(user: Optional[User] = Depends(optional_user)) -> AuthStatus:
    return AuthStatus(initialized=has_any_user(), current_user=user)


@app.get("/api/auth/me", response_model=User)
def auth_me(user: User = Depends(current_user)) -> User:
    return user


@app.post("/api/auth/setup", response_model=User)
def auth_setup(payload: UserRegister, response: Response) -> User:
    """First-run wizard: create the bootstrap admin. Only allowed when the
    users table is empty, so it cannot be re-triggered after the fact."""
    if has_any_user():
        raise HTTPException(status_code=409, detail="系统已初始化，无法重复设置管理员。")
    if get_user_by_username(payload.username):
        raise HTTPException(status_code=409, detail="用户名已存在。")
    user_dict = {
        "username": payload.username.strip(),
        "password_hash": hash_password(payload.password),
        "role": "admin",
        "active_account_id": None,
        "created_at": _now_iso(),
    }
    uid = add_user(user_dict)
    # Stamp ownership on any legacy accounts so the bootstrap admin owns them.
    assign_orphan_accounts_to(uid)
    _set_session_cookie(response, uid)
    return User(**{**user_dict, "id": uid})


@app.post("/api/auth/register", response_model=User)
def auth_register(payload: UserRegister, response: Response) -> User:
    """Self-service registration for a normal user. Disallowed before setup so
    the system always has an admin first."""
    if not has_any_user():
        raise HTTPException(status_code=409, detail="系统尚未初始化，请先设置管理员。")
    if get_user_by_username(payload.username):
        raise HTTPException(status_code=409, detail="用户名已存在。")
    user_dict = {
        "username": payload.username.strip(),
        "password_hash": hash_password(payload.password),
        "role": "normal",
        "active_account_id": None,
        "created_at": _now_iso(),
    }
    uid = add_user(user_dict)
    _set_session_cookie(response, uid)
    return User(**{**user_dict, "id": uid})


@app.post("/api/auth/login", response_model=User)
def auth_login(payload: UserLogin, response: Response) -> User:
    record = get_user_by_username(payload.username)
    if not record or not verify_password(payload.password, record.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="用户名或密码错误。")
    _set_session_cookie(response, record["id"])
    return User(
        id=record["id"],
        username=record["username"],
        role=record.get("role", "normal"),
        active_account_id=record.get("active_account_id"),
        created_at=record.get("created_at", ""),
    )


@app.post("/api/auth/logout")
def auth_logout(response: Response) -> Dict[str, str]:
    _clear_session_cookie(response)
    return {"status": "ok"}


# -------- user management (admin) --------

@app.get("/api/users", response_model=List[User])
def admin_list_users(_: User = Depends(require_admin)) -> List[User]:
    return [
        User(
            id=u["id"],
            username=u["username"],
            role=u.get("role", "normal"),
            active_account_id=u.get("active_account_id"),
            created_at=u.get("created_at", ""),
        )
        for u in list_users()
    ]


@app.post("/api/users", response_model=User)
def admin_create_user(
    payload: AdminUserCreate, _: User = Depends(require_admin)
) -> User:
    if get_user_by_username(payload.username):
        raise HTTPException(status_code=409, detail="用户名已存在。")
    user_dict = {
        "username": payload.username.strip(),
        "password_hash": hash_password(payload.password),
        "role": payload.role,
        "active_account_id": None,
        "created_at": _now_iso(),
    }
    uid = add_user(user_dict)
    return User(**{**user_dict, "id": uid})


@app.delete("/api/users/{user_id}")
def admin_delete_user(
    user_id: str, admin: User = Depends(require_admin)
) -> Dict[str, str]:
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="不能删除当前登录的管理员自己。")
    if not get_user(user_id):
        raise HTTPException(status_code=404, detail="用户不存在。")
    delete_user(user_id)
    return {"status": "ok"}


@app.post("/api/users/{user_id}/password")
def admin_reset_password(
    user_id: str, payload: PasswordReset, _: User = Depends(require_admin)
) -> Dict[str, str]:
    if not get_user(user_id):
        raise HTTPException(status_code=404, detail="用户不存在。")
    update_user(user_id, {"password_hash": hash_password(payload.new_password)})
    return {"status": "ok"}


async def _read_uploaded_json(file: UploadFile, max_bytes: int) -> Any:
    """Shared upload-and-parse for the per-account import endpoints. Raises
    HTTPException with a UI-friendly Chinese message on any input problem."""
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空。")
    if len(raw) > max_bytes:
        mb = max_bytes // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"文件过大（>{mb} MB）。")
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"不是合法的 JSON：{exc}")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="文件不是 UTF-8 编码。")


@app.post("/api/accounts/{account_id}/import/prompts")
async def import_prompts_for_account_endpoint(
    account_id: str,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
) -> Dict[str, Any]:
    """Import prompts / fixed rules / experiences / field config from a
    prompts.json file and attach them to THIS account. Replaces the
    account's existing entries; other accounts and the global system
    prompt are untouched. Only the account owner (or admin) may import."""
    _assert_owner_or_admin(account_id, user)
    data = await _read_uploaded_json(file, max_bytes=5 * 1024 * 1024)
    try:
        summary = import_prompts_for_account(account_id, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok", "summary": summary}


@app.post("/api/accounts/{account_id}/import/folders")
async def import_folders_for_account_endpoint(
    account_id: str,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
) -> Dict[str, Any]:
    """Import a folder list and attach it to THIS account, replacing the
    account's existing folder list. Accepts either the standard
    `{account_id: [...]}` export shape (every value is flattened + deduped
    into the target account) or a bare `[...]` list. Other accounts'
    folder lists are untouched."""
    _assert_owner_or_admin(account_id, user)
    data = await _read_uploaded_json(file, max_bytes=1 * 1024 * 1024)
    try:
        summary = import_folders_for_account(account_id, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok", "summary": summary}


@app.get("/api/system/mode", response_model=SystemModeStatus)
def get_system_mode(_: User = Depends(current_user)) -> SystemModeStatus:
    """Read-only for every authenticated user; the main page uses this to
    decide whether to show debug-only controls."""
    return SystemModeStatus(mode=read_system_mode())


@app.put("/api/system/mode", response_model=SystemModeStatus)
def set_system_mode(
    payload: SystemModeUpdate, _: User = Depends(require_admin)
) -> SystemModeStatus:
    try:
        mode = write_system_mode(payload.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return SystemModeStatus(mode=mode)


def _read_build_version() -> str:
    try:
        text = BUILD_VERSION_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return "dev"
    return text or "dev"


@app.get("/api/system/build")
def get_build_info(_: User = Depends(current_user)) -> Dict[str, str]:
    return {"version": _read_build_version()}


def _desktop_autostart_status() -> tuple[bool, bool]:
    if sys.platform != "darwin":
        return (False, False)
    try:
        from desktop.autostart import autostart_status
    except Exception:
        return (True, False)

    status = autostart_status()
    return (True, status == "enabled")


@app.get("/api/system/desktop", response_model=DesktopSettingsStatus)
def get_desktop_settings(_: User = Depends(require_admin)) -> DesktopSettingsStatus:
    stored = read_desktop_settings()
    supported, enabled = _desktop_autostart_status()
    return DesktopSettingsStatus(
        enable_tray=bool(stored.get("enable_tray", False)),
        autostart_supported=supported,
        autostart_enabled=enabled,
    )


@app.put("/api/system/desktop", response_model=DesktopSettingsStatus)
def update_desktop_settings(
    payload: DesktopSettingsUpdate, _: User = Depends(require_admin)
) -> DesktopSettingsStatus:
    # Persist tray preference for the desktop launcher (effective next start).
    stored = write_desktop_settings(enable_tray=payload.enable_tray)

    supported, enabled = _desktop_autostart_status()
    if payload.autostart_enabled is not None:
        if not supported:
            raise HTTPException(status_code=400, detail="当前系统不支持开机启动开关。")
        try:
            from desktop.autostart import disable_autostart, enable_autostart
            from desktop.app import pick_python_executable

            if payload.autostart_enabled:
                enable_autostart(python_executable=pick_python_executable())
            else:
                disable_autostart()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"开机启动设置失败: {exc}")
        supported, enabled = _desktop_autostart_status()

    return DesktopSettingsStatus(
        enable_tray=bool(stored.get("enable_tray", False)),
        autostart_supported=supported,
        autostart_enabled=enabled,
    )


@app.post("/api/system/shutdown")
def shutdown_server(_: User = Depends(require_admin)) -> Dict[str, str]:
    """Gracefully stop the backend process.

    Returns 200 immediately, then raises SIGTERM on our own pid from a
    short-delayed background thread so the response actually flushes to
    the browser before uvicorn tears the socket down. Matches the contract
    of scripts/stop.command (SIGTERM, then SIGKILL after grace period)."""

    def _kill_later() -> None:
        try:
            # Small delay so the HTTP response can flush to the client.
            threading.Event().wait(0.4)
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception:
            # Last resort if SIGTERM somehow didn't take.
            os._exit(0)

    threading.Thread(target=_kill_later, daemon=True).start()
    return {"status": "shutting_down"}


# -------- spam classification prompts --------

def _decorate_prompt(p: Dict) -> UserPrompt:
    """Look up the creator's username so the UI can label each row."""
    author = get_user(p.get("user_id") or "")
    return UserPrompt(
        id=p["id"],
        account_id=p.get("account_id") or "",
        user_id=p.get("user_id") or "",
        username=(author or {}).get("username", "(已删除)"),
        name=p.get("name") or "",
        text=p.get("text") or "",
        target_folder=p.get("target_folder") or None,
        created_at=p.get("created_at") or "",
        updated_at=p.get("updated_at"),
    )


def _decorate_fixed_rule(r: Dict) -> FixedRule:
    author = get_user(r.get("user_id") or "")
    program = r.get("program") if isinstance(r.get("program"), dict) else {}
    refs = r.get("refs") if isinstance(r.get("refs"), list) else []
    return FixedRule(
        id=r["id"],
        account_id=r.get("account_id") or "",
        user_id=r.get("user_id") or "",
        username=(author or {}).get("username", "(已删除)"),
        name=r.get("name") or "",
        nl_text=r.get("nl_text") or "",
        explanation=r.get("explanation") or "",
        program=program,
        code_preview=r.get("code_preview") or "",
        refs=refs,
        target_folder=r.get("target_folder") or "",
        created_at=r.get("created_at") or "",
        updated_at=r.get("updated_at"),
    )


def _build_name_lookup(
    account_id: str,
    *,
    exclude_rule_id: Optional[str] = None,
    exclude_prompt_id: Optional[str] = None,
) -> Dict[str, str]:
    """Map every named rule/prompt in this account to its raw NL text. Used
    when compiling a rule whose NL may contain @{name} references."""
    if not account_id:
        return {}
    lookup: Dict[str, str] = {}
    for r in list_fixed_rules_for_account(account_id):
        if r.get("id") == exclude_rule_id:
            continue
        nm = (r.get("name") or "").strip()
        if nm:
            lookup[nm] = r.get("nl_text") or ""
    for p in list_prompts_for_account(account_id):
        if p.get("id") == exclude_prompt_id:
            continue
        nm = (p.get("name") or "").strip()
        if nm:
            lookup[nm] = p.get("text") or ""
    return lookup


def _existing_names(
    account_id: str,
    *,
    exclude_rule_id: Optional[str] = None,
    exclude_prompt_id: Optional[str] = None,
) -> set:
    return set(
        _build_name_lookup(
            account_id,
            exclude_rule_id=exclude_rule_id,
            exclude_prompt_id=exclude_prompt_id,
        ).keys()
    )


def _validate_name_format(name: str) -> None:
    """400 on invalid format; empty is allowed (caller may auto-derive)."""
    from app.services.rule_program import NAME_RE

    if not name:
        return
    if not NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="名字必须以字母开头，只允许字母/数字/_/-，长度 1-48。",
        )


def _resolve_name(
    proposed: str,
    *,
    account_id: str,
    fallback_text: str,
    exclude_rule_id: Optional[str] = None,
    exclude_prompt_id: Optional[str] = None,
) -> str:
    """Pick the final name for a rule/prompt. Empty input → auto-slug from
    `fallback_text`. Non-empty input is validated and checked for collision."""
    from app.services.rule_program import slugify_name

    existing = _existing_names(
        account_id,
        exclude_rule_id=exclude_rule_id,
        exclude_prompt_id=exclude_prompt_id,
    )
    name = (proposed or "").strip()
    if not name:
        return slugify_name(fallback_text or "rule", existing, fallback_prefix="rule")
    _validate_name_format(name)
    if name in existing:
        raise HTTPException(
            status_code=400,
            detail=f"名字 “{name}” 已被同一账号下的另一条规则或提示占用。",
        )
    return name


def _field_config_for_account(account_id: str) -> LlmFieldConfig:
    stored = read_field_config_for_account(account_id) if account_id else None
    if not stored:
        return LlmFieldConfig()
    # Tolerate older / partial records by relying on Pydantic defaults.
    return LlmFieldConfig(**stored)


def _decorate_experience(x: Dict) -> Experience:
    author = get_user(x.get("user_id") or "")
    return Experience(
        id=x["id"],
        account_id=x.get("account_id") or "",
        user_id=x.get("user_id") or "",
        username=(author or {}).get("username", "(已删除)"),
        text=x.get("text") or "",
        source=x.get("source") or "manual",
        source_email_id=x.get("source_email_id"),
        created_at=x.get("created_at") or "",
        updated_at=x.get("updated_at"),
    )


@app.get("/api/spam-prompts", response_model=PromptsView)
def list_spam_prompts(user: User = Depends(current_user)) -> PromptsView:
    """Returns everything needed to render the classification panel for the
    current user's active account: system prompt, user prompts (with their
    target folders), fixed rules, distilled experiences, field-inclusion
    config, and the folder list that target dropdowns are populated from."""
    active_id = get_user_active_account_id(user.id) or ""
    system_override = read_system_spam_prompt()
    return PromptsView(
        system=system_override or DEFAULT_SYSTEM_PROMPT,
        system_is_default=not system_override,
        items=[
            _decorate_prompt(p)
            for p in list_prompts_for_account(active_id)
        ],
        fixed_rules=[
            _decorate_fixed_rule(r)
            for r in list_fixed_rules_for_account(active_id)
        ],
        experiences=[
            _decorate_experience(x)
            for x in list_experiences_for_account(active_id)
        ],
        field_config=_field_config_for_account(active_id),
        available_folders=read_folders(active_id) if active_id else [],
    )


@app.put("/api/spam-prompts/field-config", response_model=LlmFieldConfig)
def update_field_config(
    payload: LlmFieldConfig, user: User = Depends(current_user)
) -> LlmFieldConfig:
    """The active account's owner (or any admin) decides which fields are
    sent to Qwen for classification."""
    active_id = _active_account_id_for(user)
    write_field_config_for_account(active_id, payload.model_dump())
    return payload


@app.put("/api/spam-prompts/system")
def update_system_prompt(
    payload: SystemPromptUpdate, _: User = Depends(require_admin)
) -> Dict[str, str]:
    """Admin-only override of the built-in spam-detection prompt. Passing an
    empty string resets the override and falls back to DEFAULT_SYSTEM_PROMPT."""
    text = (payload.text or "").strip()
    write_system_spam_prompt(text or None)
    return {"status": "ok"}


@app.get("/api/llm-config", response_model=LlmConfigStatus)
def get_llm_config(_: User = Depends(current_user)) -> LlmConfigStatus:
    """Returns whether the DeepSeek API key has been configured. Never echoes
    the key itself — the UI only needs to know if a prompt is required."""
    return LlmConfigStatus(
        configured=is_api_key_configured(),
        model=LLM_MODEL_NAME,
    )


@app.put("/api/llm-config", response_model=LlmConfigStatus)
def update_llm_config(
    payload: LlmConfigUpdate, _: User = Depends(require_admin)
) -> LlmConfigStatus:
    """Admin-only: store the DeepSeek API key used for classification."""
    key = (payload.api_key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="API Key 不能为空")
    try:
        write_llm_api_key(key)
    except ModuleNotFoundError as exc:
        # Most common production trip-wire: the venv is missing `cryptography`.
        # Surface the actionable fix instead of a generic 500.
        raise HTTPException(
            status_code=500,
            detail=(
                "服务器缺少加密依赖（cryptography），无法安全落盘。请在服务器执行："
                "pip install -r requirements.txt 后重启服务。"
                f"原始错误：{exc}"
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"保存 API Key 失败：{exc.__class__.__name__}: {exc}",
        )
    return LlmConfigStatus(configured=True, model=LLM_MODEL_NAME)


# Sentinel target_folder value meaning "applies to all emails / all folders".
# Stored verbatim; the classifier and prompt builder treat it as general
# guidance rather than a concrete folder destination.
ALL_FOLDERS_SENTINEL = "*"


def _normalize_target_folder(
    raw: Optional[str], account_id: str, *, required: bool
) -> Optional[str]:
    """Validate that the supplied folder name exists on the active account,
    OR equals the "全部" sentinel ("*"). Returns None when the input is
    empty AND optional; raises otherwise."""
    folder = (raw or "").strip()
    if not folder:
        if required:
            raise HTTPException(status_code=400, detail="必须指定目标文件夹。")
        return None
    if folder == ALL_FOLDERS_SENTINEL:
        return ALL_FOLDERS_SENTINEL
    if folder not in read_folders(account_id):
        raise HTTPException(status_code=400, detail=f"未知文件夹: {folder}")
    return folder


@app.post("/api/spam-prompts", response_model=UserPrompt)
def create_user_prompt(
    payload: UserPromptCreate, user: User = Depends(current_user)
) -> UserPrompt:
    """A user adds a classification rule to their currently-active account.
    Admins can do this too — the prompt is attributed to whoever submitted it."""
    from app.services.rule_program import expand_refs

    active_id = _active_account_id_for(user)
    target_folder = _normalize_target_folder(
        payload.target_folder, active_id, required=False
    )
    name = _resolve_name(
        payload.name,
        account_id=active_id,
        fallback_text=payload.text,
    )
    # Catch cycles / missing refs in the prompt text, so that a rule later
    # @-referencing this prompt won't fail in a confusing place.
    lookup = _build_name_lookup(active_id)
    try:
        expand_refs(payload.text, lookup, self_name=name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    record = add_prompt(
        {
            "account_id": active_id,
            "user_id": user.id,
            "name": name,
            "text": payload.text.strip(),
            "target_folder": target_folder,
            "created_at": _now_iso(),
            "updated_at": None,
        }
    )
    return _decorate_prompt(record)


def _assert_can_edit_prompt(prompt: Dict, user: User) -> None:
    if user.role == "admin":
        return
    if prompt.get("user_id") != user.id:
        raise HTTPException(status_code=403, detail="只能修改/删除自己创建的提示。")


@app.put("/api/spam-prompts/{prompt_id}", response_model=UserPrompt)
def edit_user_prompt(
    prompt_id: str,
    payload: UserPromptUpdate,
    user: User = Depends(current_user),
) -> UserPrompt:
    from app.services.rule_program import expand_refs

    existing = get_prompt(prompt_id)
    if not existing:
        raise HTTPException(status_code=404, detail="提示不存在。")
    _assert_can_edit_prompt(existing, user)
    account_id = existing.get("account_id") or ""
    target_folder = _normalize_target_folder(
        payload.target_folder,
        account_id,
        required=False,
    )
    new_name = existing.get("name") or ""
    fields: Dict = {
        "text": payload.text.strip(),
        "target_folder": target_folder,
        "updated_at": _now_iso(),
    }
    if payload.name is not None:
        new_name = _resolve_name(
            payload.name,
            account_id=account_id,
            fallback_text=payload.text,
            exclude_prompt_id=prompt_id,
        )
        fields["name"] = new_name
    # Cycle / missing-ref check against the latest account state, excluding
    # this prompt's old name so renaming + self-reference is detected as a
    # cycle the same way.
    lookup = _build_name_lookup(account_id, exclude_prompt_id=prompt_id)
    try:
        expand_refs(payload.text, lookup, self_name=new_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    updated = update_prompt(prompt_id, fields)
    return _decorate_prompt(updated)


@app.delete("/api/spam-prompts/{prompt_id}")
def remove_user_prompt(
    prompt_id: str, user: User = Depends(current_user)
) -> Dict[str, str]:
    existing = get_prompt(prompt_id)
    if not existing:
        raise HTTPException(status_code=404, detail="提示不存在。")
    _assert_can_edit_prompt(existing, user)
    delete_prompt(prompt_id)
    return {"status": "ok"}


# -------- experiences --------

def _assert_can_edit_experience(experience: Dict, user: User) -> None:
    if user.role == "admin":
        return
    if experience.get("user_id") != user.id:
        raise HTTPException(
            status_code=403, detail="只能修改/删除自己创建的经验。"
        )


@app.post("/api/experiences", response_model=Experience)
def create_experience(
    payload: ExperienceCreate, user: User = Depends(current_user)
) -> Experience:
    active_id = _active_account_id_for(user)
    record = add_experience(
        {
            "account_id": active_id,
            "user_id": user.id,
            "text": payload.text.strip(),
            "source": payload.source or "manual",
            "source_email_id": payload.source_email_id,
            "created_at": _now_iso(),
            "updated_at": None,
        }
    )
    return _decorate_experience(record)


@app.put("/api/experiences/{experience_id}", response_model=Experience)
def edit_experience(
    experience_id: str,
    payload: ExperienceUpdate,
    user: User = Depends(current_user),
) -> Experience:
    existing = get_experience(experience_id)
    if not existing:
        raise HTTPException(status_code=404, detail="经验不存在。")
    _assert_can_edit_experience(existing, user)
    updated = update_experience(
        experience_id,
        {"text": payload.text.strip(), "updated_at": _now_iso()},
    )
    return _decorate_experience(updated)


@app.delete("/api/experiences/{experience_id}")
def remove_experience(
    experience_id: str, user: User = Depends(current_user)
) -> Dict[str, str]:
    existing = get_experience(experience_id)
    if not existing:
        raise HTTPException(status_code=404, detail="经验不存在。")
    _assert_can_edit_experience(existing, user)
    delete_experience(experience_id)
    return {"status": "ok"}


@app.post(
    "/api/emails/{email_id}/importance-with-reason",
    response_model=ImportanceToggleResult,
)
def toggle_importance_with_reason(
    email_id: str,
    payload: ImportanceToggleRequest,
    user: User = Depends(current_user),
) -> ImportanceToggleResult:
    """Atomic combo-action triggered by the「为何重要 / 为何取消重要」dialog:
    flip the email's important flag AND distill the user's free-text
    reason (combined with the email's own content) into a one-line
    experience that future classifications will respect.

    If the LLM call fails we still flip the flag and persist the user's
    raw reason verbatim as the experience — so the user's effort is never
    wasted on a transient API hiccup.
    """
    if payload.email_id != email_id:
        raise HTTPException(status_code=400, detail="email_id 不一致。")
    emails = read_emails()
    target = next((e for e in emails if e.get("id") == email_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="邮件不存在。")
    _assert_record_belongs_to_user(target, user)

    target["important"] = bool(payload.mark_important)
    # Newly-marked important always starts as "待处理"; unmarking 重要 implicitly
    # drops the 已处理 state too. Mirrors EmailUpdate's behaviour so both paths
    # converge.
    if not target["important"]:
        target["handled"] = False
    else:
        target["handled"] = False
    write_emails(emails)

    distilled = distill_experience(
        direction="mark" if payload.mark_important else "unmark",
        user_reason=payload.reason,
        from_email=target.get("from_email") or "",
        to_email=target.get("to_email") or "",
        subject=target.get("subject") or "",
        body=target.get("body") or "",
    )
    if not distilled:
        # Fall back to the user's own words so the lesson isn't lost.
        prefix = "重要邮件特征：" if payload.mark_important else "不应被标为重要的邮件："
        distilled = (prefix + payload.reason.strip())[:240]

    active_id = _active_account_id_for(user)
    record = add_experience(
        {
            "account_id": active_id,
            "user_id": user.id,
            "text": distilled,
            "source": "important-mark" if payload.mark_important else "important-unmark",
            "source_email_id": email_id,
            "created_at": _now_iso(),
            "updated_at": None,
        }
    )

    return ImportanceToggleResult(
        email=EmailRecord(**target),
        experience=_decorate_experience(record),
    )


@app.post(
    "/api/emails/{email_id}/recategorize-with-reason",
    response_model=RecategorizeResult,
)
def recategorize_with_reason(
    email_id: str,
    payload: RecategorizeRequest,
    background: BackgroundTasks,
    user: User = Depends(current_user),
) -> RecategorizeResult:
    """Atomic combo-action triggered by the「移动到 X · 为什么？」dialog:
    move the email to the new category AND distill the user's reason into
    a one-line experience the classifier will respect for future mail.

    Mirrors the importance flow — if the LLM distillation fails we keep
    the move and save the user's raw reason verbatim as the experience so
    their input is never wasted on a transient API hiccup.
    """
    if payload.email_id != email_id:
        raise HTTPException(status_code=400, detail="email_id 不一致。")

    new_cat = (payload.new_category or "").strip()
    if not new_cat:
        raise HTTPException(status_code=400, detail="分类不能为空。")

    emails = read_emails()
    target = next((e for e in emails if e.get("id") == email_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="邮件不存在。")
    _assert_record_belongs_to_user(target, user)

    if new_cat not in read_folders(target.get("account_id") or ""):
        raise HTTPException(status_code=400, detail=f"未知文件夹: {new_cat}")

    old_cat = target.get("category") or ""
    if new_cat == old_cat:
        raise HTTPException(status_code=400, detail="邮件已在该分类中。")

    target["category"] = new_cat
    write_emails(emails)

    # Best-effort IMAP MOVE for accounts opted into folder sync — same path
    # the normal /update endpoint takes when category changes.
    target_account_id = target.get("account_id") or ""
    owner_acc = get_account(target_account_id)
    owner_sync = SyncSettings(**((owner_acc or {}).get("sync") or {}))
    if owner_sync.sync_folders:
        background.add_task(
            _bg_sync_move,
            target_account_id,
            target.get("imap_mailbox"),
            target.get("imap_uid"),
            new_cat,
        )

    distilled = distill_category_experience(
        from_category=old_cat,
        to_category=new_cat,
        user_reason=payload.reason,
        from_email=target.get("from_email") or "",
        to_email=target.get("to_email") or "",
        subject=target.get("subject") or "",
        body=target.get("body") or "",
    )
    if not distilled:
        # Fall back to the user's own words so the lesson isn't lost.
        distilled = (
            f"应归入「{new_cat}」的邮件：{payload.reason.strip()}"
        )[:240]

    active_id = _active_account_id_for(user)
    record = add_experience(
        {
            "account_id": active_id,
            "user_id": user.id,
            "text": distilled,
            "source": "recategorize",
            "source_email_id": email_id,
            "created_at": _now_iso(),
            "updated_at": None,
        }
    )

    return RecategorizeResult(
        email=EmailRecord(**target),
        experience=_decorate_experience(record),
    )


@app.post(
    "/api/emails/{email_id}/generate-reply",
    response_model=ReplyGenerationResult,
)
def generate_email_reply(
    email_id: str,
    payload: ReplyGenerationRequest,
    user: User = Depends(current_user),
) -> ReplyGenerationResult:
    """Compose-window auto-reply: read the original email + the active
    account's signature, ask DeepSeek for a polite reply matching the
    user's intent and the original email's language, and return the
    generated text. The frontend splices the result into the compose
    body in place of whatever's above the「-------- 原邮件 --------」block."""
    target = next(
        (e for e in read_emails() if e.get("id") == email_id), None
    )
    if target is None:
        raise HTTPException(status_code=404, detail="邮件不存在。")
    _assert_record_belongs_to_user(target, user)

    acc = _active_account_for(user)
    settings = acc.get("settings") or {}
    signature = (settings.get("signature") or "").strip()

    try:
        reply_text = generate_reply(
            original_from=target.get("from_email") or "",
            original_to=target.get("to_email") or "",
            original_subject=target.get("subject") or "",
            original_body=target.get("body") or "",
            intent=payload.intent,
            signature=signature,
            language=payload.language,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if not reply_text:
        raise HTTPException(status_code=502, detail="模型未返回有效回复，请重试。")

    return ReplyGenerationResult(reply_text=reply_text)


@app.get(
    "/api/emails/{email_id}/reply-summary",
    response_model=ReplySummaryResult,
)
def get_reply_summary(
    email_id: str, user: User = Depends(current_user)
) -> ReplySummaryResult:
    """Reply-window pre-flight summary. Returns a 2-line Chinese summary
    (邮件大意 / 对方诉求) for English mail to help the user draft a reply.
    For non-English mail returns an empty summary so the frontend can
    just hide the panel — no LLM call is wasted."""
    target = next(
        (e for e in read_emails() if e.get("id") == email_id), None
    )
    if target is None:
        raise HTTPException(status_code=404, detail="邮件不存在。")
    _assert_record_belongs_to_user(target, user)

    body = target.get("body") or ""
    subject = target.get("subject") or ""
    # Heuristic detection on the joined subject+body — short subjects in
    # Chinese with English-quoted content shouldn't trigger; long English
    # bodies should.
    if not is_predominantly_english(subject + "\n" + body):
        return ReplySummaryResult(is_english=False, summary="")

    try:
        summary = summarize_email_for_reply(
            from_email=target.get("from_email") or "",
            subject=subject,
            body=body,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return ReplySummaryResult(is_english=True, summary=summary)


@app.post("/api/compose-draft", response_model=ComposeDraftResult)
def generate_new_email_draft(
    payload: ComposeDraftRequest, user: User = Depends(current_user)
) -> ComposeDraftResult:
    """Compose-window auto-generate: draft a brand-new email body from
    the user's intent + the active account's signature. Sibling of
    `/api/emails/{id}/generate-reply` but with no original email to
    quote. Used by the「✨ 自动生成」panel in the compose window."""
    acc = _active_account_for(user)
    settings = acc.get("settings") or {}
    signature = (settings.get("signature") or "").strip()
    try:
        body_text = generate_compose_draft(
            intent=payload.intent,
            signature=signature,
            language=payload.language,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if not body_text:
        raise HTTPException(status_code=502, detail="模型未返回有效内容，请重试。")
    return ComposeDraftResult(body_text=body_text)


# -------- fixed (programmatic) classification rules --------

def _assert_can_edit_rule(rule: Dict, user: User) -> None:
    """Same permission rule as LLM prompts: author or admin only."""
    if user.role == "admin":
        return
    if rule.get("user_id") != user.id:
        raise HTTPException(
            status_code=403, detail="只能修改/删除自己创建的固定规则。"
        )


@app.post("/api/fixed-rules/compile", response_model=FixedRuleCompileResponse)
def compile_fixed_rule(
    payload: FixedRuleCompileRequest, user: User = Depends(current_user)
) -> FixedRuleCompileResponse:
    """Translate the natural-language rule into an AST program for review.
    Resolves @{name} references against this account's other rules/prompts
    (excluding the rule currently being edited) and rejects cycles. Nothing
    is persisted — the client follows up with POST /api/fixed-rules once the
    user confirms."""
    from app.services.rule_program import compile_from_nl

    active_id = _active_account_id_for(user)
    target_folder = _normalize_target_folder(
        payload.target_folder, active_id, required=True
    )
    _validate_name_format(payload.name)

    lookup = _build_name_lookup(
        active_id, exclude_rule_id=payload.editing_id
    )
    try:
        result = compile_from_nl(
            payload.nl_text,
            name_lookup=lookup,
            self_name=payload.name.strip(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return FixedRuleCompileResponse(
        nl_text=payload.nl_text.strip(),
        target_folder=target_folder,
        explanation=result["explanation"],
        code_preview=result["code_preview"],
        program=result["program"],
        name=payload.name.strip(),
        expanded_nl=result["expanded_nl"],
        refs=result["refs"],
    )


@app.post("/api/fixed-rules/validate", response_model=FixedRuleValidateResponse)
def validate_fixed_rule_program(
    payload: FixedRuleValidateRequest, _: User = Depends(current_user)
) -> FixedRuleValidateResponse:
    """Check a user-edited AST without saving. Returns the same error list
    /create would have raised, plus a refreshed pseudo-code preview so the
    UI can keep the "see what it does" panel in sync with the edits."""
    from app.services.rule_program import render_pseudo_code, validate_program

    errs = validate_program(payload.program)
    return FixedRuleValidateResponse(
        valid=not errs,
        errors=errs,
        code_preview=render_pseudo_code(payload.program) if not errs else "",
    )


@app.post("/api/fixed-rules", response_model=FixedRule)
def create_fixed_rule(
    payload: FixedRuleCreate, user: User = Depends(current_user)
) -> FixedRule:
    """Persist a user-confirmed AST rule. The client must have gone through
    /api/fixed-rules/compile so the program/explanation match what the user
    reviewed. Server re-validates the AST and the @{ref} graph (cycles,
    missing names) before saving."""
    from app.services.rule_program import expand_refs, parse_refs, validate_program

    active_id = _active_account_id_for(user)
    target_folder = _normalize_target_folder(
        payload.target_folder, active_id, required=True
    )
    errs = validate_program(payload.program)
    if errs:
        raise HTTPException(
            status_code=400, detail="生成的程序不合法：" + "；".join(errs)
        )

    name = _resolve_name(
        payload.name,
        account_id=active_id,
        fallback_text=payload.nl_text,
    )

    # Re-check refs server-side using the latest account state — guards
    # against TOCTOU between /compile and /create.
    lookup = _build_name_lookup(active_id)
    try:
        expand_refs(payload.nl_text, lookup, self_name=name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    refs = parse_refs(payload.nl_text)
    record = add_fixed_rule(
        {
            "account_id": active_id,
            "user_id": user.id,
            "name": name,
            "nl_text": payload.nl_text.strip(),
            "explanation": payload.explanation.strip(),
            "code_preview": payload.code_preview.strip(),
            "program": payload.program,
            "refs": refs,
            "target_folder": target_folder,
            "created_at": _now_iso(),
            "updated_at": None,
        }
    )
    return _decorate_fixed_rule(record)


@app.put("/api/fixed-rules/{rule_id}", response_model=FixedRule)
def edit_fixed_rule(
    rule_id: str,
    payload: FixedRuleUpdate,
    user: User = Depends(current_user),
) -> FixedRule:
    from app.services.rule_program import expand_refs, parse_refs, validate_program

    existing = get_fixed_rule(rule_id)
    if not existing:
        raise HTTPException(status_code=404, detail="规则不存在。")
    _assert_can_edit_rule(existing, user)

    fields: Dict = {}
    new_name = existing.get("name") or ""
    if payload.name is not None:
        new_name = _resolve_name(
            payload.name,
            account_id=existing.get("account_id") or "",
            fallback_text=(payload.nl_text or existing.get("nl_text") or ""),
            exclude_rule_id=rule_id,
        )
        fields["name"] = new_name

    new_nl = existing.get("nl_text") or ""
    if payload.nl_text is not None:
        new_nl = payload.nl_text.strip()
        fields["nl_text"] = new_nl
    if payload.explanation is not None:
        fields["explanation"] = payload.explanation.strip()
    if payload.code_preview is not None:
        fields["code_preview"] = payload.code_preview.strip()
    if payload.program is not None:
        errs = validate_program(payload.program)
        if errs:
            raise HTTPException(
                status_code=400,
                detail="生成的程序不合法：" + "；".join(errs),
            )
        fields["program"] = payload.program
    if payload.target_folder is not None:
        fields["target_folder"] = _normalize_target_folder(
            payload.target_folder,
            existing.get("account_id") or "",
            required=True,
        )

    # Cycle / missing-ref re-check against current account state.
    lookup = _build_name_lookup(
        existing.get("account_id") or "", exclude_rule_id=rule_id
    )
    try:
        expand_refs(new_nl, lookup, self_name=new_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if payload.refs is not None:
        fields["refs"] = list(payload.refs)
    elif payload.nl_text is not None:
        fields["refs"] = parse_refs(new_nl)

    fields["updated_at"] = _now_iso()
    updated = update_fixed_rule(rule_id, fields)
    return _decorate_fixed_rule(updated)


@app.post("/api/fixed-rules/reorder", response_model=List[FixedRule])
def reorder_rules(
    payload: FixedRuleReorder, user: User = Depends(current_user)
) -> List[FixedRule]:
    """Replace the active account's fixed-rule order. Top of the list = top
    priority — that's the order email_client.classify_email_record walks
    them in. Any rule id missing from the payload is appended in its current
    relative position so a forgotten id can't silently drop off."""
    active_id = _active_account_id_for(user)
    try:
        new_list = reorder_fixed_rules(active_id, payload.rule_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return [_decorate_fixed_rule(r) for r in new_list]


@app.delete("/api/fixed-rules/{rule_id}")
def remove_fixed_rule(
    rule_id: str, user: User = Depends(current_user)
) -> Dict[str, str]:
    existing = get_fixed_rule(rule_id)
    if not existing:
        raise HTTPException(status_code=404, detail="规则不存在。")
    _assert_can_edit_rule(existing, user)
    delete_fixed_rule(rule_id)
    return {"status": "ok"}


# -------- reclassify "未分类" emails on demand --------

def _classification_context(account_id: str) -> Dict:
    """Bundle every piece of state the classifier needs for one account.

    Fixed rules whose target is the "全部" sentinel (`*`) are forwarded to
    the LLM as extra general guidance, since they have no folder to route
    to. Their AST still runs at the fixed-rule stage, but it can never
    return early — see classify_email_record."""
    fixed_rules = list_fixed_rules_for_account(account_id)
    user_prompts: List[Dict] = [
        {"text": p.get("text") or "", "target_folder": p.get("target_folder")}
        for p in list_prompts_for_account(account_id)
        if (p.get("text") or "").strip()
    ]
    for rule in fixed_rules:
        if (rule.get("target_folder") or "").strip() == ALL_FOLDERS_SENTINEL:
            nl = (rule.get("nl_text") or "").strip()
            if nl:
                user_prompts.append({"text": nl, "target_folder": None})
    # Distilled experiences are surfaced to the LLM as additional general
    # guidance — same channel as target-less prompts. The user can curate
    # them via the right-sidebar 经验 section.
    for exp in list_experiences_for_account(account_id):
        text = (exp.get("text") or "").strip()
        if text:
            user_prompts.append({"text": "经验: " + text, "target_folder": None})
    return {
        "system_prompt": read_system_spam_prompt(),
        "user_prompts_with_targets": user_prompts,
        "fixed_rules": fixed_rules,
        "available_folders": read_folders(account_id),
        "field_config": _field_config_for_account(account_id).model_dump(),
    }


@app.post("/api/classify-unsorted", response_model=ClassifyUnsortedResult)
def classify_unsorted(user: User = Depends(current_user)) -> ClassifyUnsortedResult:
    """Retry classification on every email currently sitting in 未分类 for
    the current user's active account. Emails that still can't be sorted
    stay in 未分类."""
    active_id = _active_account_id_for(user)
    ctx = _classification_context(active_id)

    emails = read_emails()
    classified = 0
    remaining = 0
    total = 0
    for rec in emails:
        if rec.get("account_id") != active_id:
            continue
        if (rec.get("category") or "") != UNCLASSIFIED:
            continue
        total += 1
        category, important, reason = classify_email_record(
            from_email=rec.get("from_email") or "",
            to_email=rec.get("to_email") or "",
            cc_email=rec.get("cc_email") or "",
            subject=rec.get("subject") or "",
            body=rec.get("body") or "",
            attachments=[a.get("filename", "") for a in (rec.get("attachments") or [])],
            **ctx,
        )
        # Importance: only ever flip OFF→ON automatically. Manual user marks
        # via the UI are preserved across re-classification runs.
        if important and not rec.get("important"):
            rec["important"] = True
        if category and category != UNCLASSIFIED:
            rec["category"] = category
            rec["spam_reason"] = reason
            classified += 1
        else:
            # Keep tombstone but refresh the reason so the user can see why.
            rec["spam_reason"] = reason
            remaining += 1

    write_emails(emails)
    return ClassifyUnsortedResult(
        classified=classified, remaining=remaining, total=total
    )


@app.post("/api/classify-unsorted/stream")
def classify_unsorted_stream(user: User = Depends(current_user)) -> StreamingResponse:
    """Retry classification on every 未分类 email for the active account and
    stream progress as NDJSON so the frontend can show real-time status."""
    active_id = _active_account_id_for(user)
    ctx = _classification_context(active_id)

    q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=256)
    SENTINEL: Dict[str, Any] = {"__sentinel__": True}
    state: Dict[str, Any] = {
        "classified": 0,
        "remaining": 0,
        "total": 0,
        "skipped": 0,
        "error": None,
    }

    def progress(ev: Dict[str, Any]) -> None:
        try:
            q.put(ev, timeout=30)
        except Exception:
            pass

    def worker() -> None:
        try:
            emails = read_emails()
            scope = []
            for rec in emails:
                if rec.get("account_id") != active_id:
                    continue
                if (rec.get("category") or "") != UNCLASSIFIED:
                    continue
                scope.append(rec)

            total = len(scope)
            state["total"] = total
            progress({"type": "connected"})
            progress({"type": "planned", "total": total})

            import traceback as _tb
            classified = 0
            remaining = 0
            skipped = 0
            for idx, rec in enumerate(scope):
                try:
                    category, important, reason = classify_email_record(
                        from_email=rec.get("from_email") or "",
                        to_email=rec.get("to_email") or "",
                        cc_email=rec.get("cc_email") or "",
                        subject=rec.get("subject") or "",
                        body=rec.get("body") or "",
                        attachments=[
                            a.get("filename", "")
                            for a in (rec.get("attachments") or [])
                        ],
                        **ctx,
                    )

                    if important and not rec.get("important"):
                        rec["important"] = True
                    if category and category != UNCLASSIFIED:
                        rec["category"] = category
                        rec["spam_reason"] = reason
                        classified += 1
                    else:
                        rec["spam_reason"] = reason
                        remaining += 1

                    progress(
                        {
                            "type": "classified",
                            "index": idx + 1,
                            "total": total,
                            "subject": rec.get("subject") or "",
                            "from": rec.get("from_email") or "",
                            "category": rec.get("category") or UNCLASSIFIED,
                            "reason": reason,
                        }
                    )
                except Exception as per_email_exc:
                    import logging as _logging

                    _logging.getLogger(__name__).warning(
                        "classify-unsorted: skipping email %s (idx %d/%d): %s\n%s",
                        rec.get("id") or "?",
                        idx + 1,
                        total,
                        per_email_exc,
                        _tb.format_exc(),
                    )
                    skipped += 1
                    remaining += 1
                    progress(
                        {
                            "type": "skipped",
                            "index": idx + 1,
                            "total": total,
                            "uid": rec.get("imap_uid") or rec.get("id") or "",
                            "reason": f"{type(per_email_exc).__name__}: {per_email_exc}",
                        }
                    )

            write_emails(emails)
            state["classified"] = classified
            state["remaining"] = remaining
            state["skipped"] = skipped
        except Exception as exc:
            state["error"] = f"智能分类失败: {exc.__class__.__name__}: {exc}"
        finally:
            q.put(SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    def line(ev: Dict[str, Any]) -> bytes:
        return (json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8")

    def gen():
        while True:
            ev = q.get()
            if ev is SENTINEL:
                break
            yield line(ev)

        if state["error"]:
            yield line({"type": "error", "message": state["error"]})
            yield line({"type": "done", "fetched": 0, "stored": 0})
            return

        total = state["total"]
        classified = state["classified"]
        remaining = state["remaining"]
        skipped = state["skipped"]
        yield line(
            {
                "type": "saved",
                "fetched": total,
                "stored": classified,
                "remaining": remaining,
                "skipped": skipped,
            }
        )
        yield line(
            {
                "type": "done",
                "fetched": total,
                "stored": classified,
                "remaining": remaining,
                "skipped": skipped,
            }
        )

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/reclassify-all/stream")
def reclassify_all_stream(user: User = Depends(current_user)) -> StreamingResponse:
    """Re-run the full classification pipeline against every locally-stored
    email for the active account — without re-fetching from IMAP. Streams
    progress in the same NDJSON shape as /api/receive/stream so the UI can
    reuse the existing receive progress renderer.

    Useful when a user iterates on rules/prompts and wants to retest against
    the existing inbox without burning IMAP fetches.
    """
    active_id = _active_account_id_for(user)
    ctx = _classification_context(active_id)

    q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=256)
    SENTINEL: Dict[str, Any] = {"__sentinel__": True}
    state: Dict[str, Any] = {"changed": 0, "total": 0, "error": None}

    def progress(ev: Dict[str, Any]) -> None:
        try:
            q.put(ev, timeout=30)
        except Exception:
            pass

    def worker() -> None:
        try:
            emails = read_emails()
            scope = [e for e in emails if e.get("account_id") == active_id]
            others = [e for e in emails if e.get("account_id") != active_id]
            total = len(scope)
            state["total"] = total
            progress({"type": "connected"})
            progress({"type": "planned", "total": total})

            import traceback as _tb
            changed = 0
            for idx, rec in enumerate(scope):
                old_cat = rec.get("category") or ""
                # Per-user policy for the「重新分类」button: this is a clean
                # re-run, not an additive retry. Reset both the category
                # and the 重要 flag before classifying so the LLM verdict
                # is fully authoritative — manual user marks (⭐, prior
                # category) are intentionally cleared. Use 智能分类 for the
                # additive retry that preserves manual marks.
                rec["category"] = UNCLASSIFIED
                rec["important"] = False

                try:
                    category, important, reason = classify_email_record(
                        from_email=rec.get("from_email") or "",
                        to_email=rec.get("to_email") or "",
                        cc_email=rec.get("cc_email") or "",
                        subject=rec.get("subject") or "",
                        body=rec.get("body") or "",
                        attachments=[
                            a.get("filename", "")
                            for a in (rec.get("attachments") or [])
                        ],
                        **ctx,
                    )
                    rec["category"] = category or UNCLASSIFIED
                    rec["important"] = bool(important)
                    rec["spam_reason"] = reason
                    if rec["category"] != old_cat:
                        changed += 1
                    progress(
                        {
                            "type": "classified",
                            "index": idx + 1,
                            "total": total,
                            "subject": rec.get("subject") or "",
                            "from": rec.get("from_email") or "",
                            "category": rec["category"],
                            "reason": reason,
                        }
                    )
                except Exception as per_email_exc:
                    # One bad email mustn't abort a 1000-email reclassify.
                    # Leave the record in UNCLASSIFIED, log, surface a
                    # 'skipped' progress event, and move on.
                    import logging as _logging
                    _logging.getLogger(__name__).warning(
                        "reclassify-all: skipping email %s (idx %d/%d): %s\n%s",
                        rec.get("id") or "?",
                        idx + 1,
                        total,
                        per_email_exc,
                        _tb.format_exc(),
                    )
                    progress(
                        {
                            "type": "skipped",
                            "index": idx + 1,
                            "total": total,
                            "uid": rec.get("imap_uid") or rec.get("id") or "",
                            "reason": f"{type(per_email_exc).__name__}: {per_email_exc}",
                        }
                    )

            write_emails(others + scope)
            state["changed"] = changed
        except Exception as exc:
            state["error"] = f"重新分类失败: {exc.__class__.__name__}: {exc}"
        finally:
            q.put(SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    def line(ev: Dict[str, Any]) -> bytes:
        return (json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8")

    def gen():
        while True:
            ev = q.get()
            if ev is SENTINEL:
                break
            yield line(ev)
        if state["error"]:
            yield line({"type": "error", "message": state["error"]})
            yield line({"type": "done", "fetched": 0, "stored": 0})
            return
        total = state["total"]
        changed = state["changed"]
        # Reuse the receive stream's "saved"/"done" event names so the
        # frontend handler doesn't need a separate code path.
        yield line({"type": "saved", "fetched": total, "stored": changed})
        yield line({"type": "done", "fetched": total, "stored": changed})

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _visible_accounts(user: User) -> List[Dict]:
    """Email accounts the user is allowed to use AS THEMSELVES on the main
    client (/settings, /). This is strictly owner-scoped — even admins only
    see their own mailboxes here. Cross-user mailbox management lives in
    /admin (see _admin_visible_accounts_for_user)."""
    return [a for a in list_accounts() if a.get("owner_user_id") == user.id]


def _user_owns_account(user: User, account: Dict) -> bool:
    """Strict ownership — never overridden by admin. Used to gate the
    'activate' action: activating someone else's mailbox would let one
    person log in to another person's email, which the spec forbids."""
    return account.get("owner_user_id") == user.id


def _user_can_access_account(user: User, account: Dict) -> bool:
    """Edit / delete / read-metadata gate. Owner always; admin yes too,
    but only for management actions (rename, delete, list). Activate is
    NOT one of these — see _user_owns_account."""
    if user.role == "admin":
        return True
    return _user_owns_account(user, account)


def _assert_owner_or_admin(account_id: str, user: User) -> Dict:
    acc = get_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在。")
    if not _user_can_access_account(user, acc):
        raise HTTPException(status_code=403, detail="无权访问该账号。")
    return acc


def _assert_owner_only(account_id: str, user: User) -> Dict:
    """Admin override does NOT apply — strict owner-only. Used by activate
    so even an admin cannot point their own session at someone else's
    mailbox."""
    acc = get_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在。")
    if not _user_owns_account(user, acc):
        raise HTTPException(status_code=403, detail="该账号不属于你，无法激活。")
    return acc


def _active_account_for(user: User) -> Dict:
    """Resolve the active email account for this user.

    Strict ownership: 'active' is the mailbox THIS user is operating their
    own client against. We use _user_owns_account (not the admin-can-access
    variant) so an admin's session never silently lands in someone else's
    inbox just because they were never assigned an active account.
    """
    acc_id = get_user_active_account_id(user.id)
    if acc_id:
        acc = get_account(acc_id)
        if acc and _user_owns_account(user, acc):
            return acc
    # Fall back to the first OWNED account.
    owned = _visible_accounts(user)
    if not owned:
        raise HTTPException(status_code=400, detail="请先添加并激活一个邮箱账号。")
    set_user_active_account(user.id, owned[0]["id"])
    return owned[0]


def _active_account_id_for(user: User) -> str:
    return _active_account_for(user)["id"]


def _active_settings_for(user: User) -> Dict:
    return _active_account_for(user).get("settings") or {}


@app.get("/api/config")
def get_config(user: User = Depends(current_user)) -> Dict:
    """Returns the active account's config slice for the current user. Kept
    for backward compatibility with older clients; new code should use
    /api/accounts."""
    acc = _active_account_for(user)
    return {
        "settings": acc.get("settings", {}),
        "sync": acc.get("sync", {}),
    }


@app.post("/api/config")
def save_config(
    payload: ConfigPayload, user: User = Depends(current_user)
) -> Dict[str, str]:
    """Backward-compatible: updates the active account for the current user,
    or creates a brand-new owned account if none exists yet."""
    settings_dict = payload.settings.model_dump()
    owned = _visible_accounts(user)
    sync_dict = (
        payload.sync
        or SyncSettings(**(owned[0].get("sync") if owned else {} or {}))
    ).model_dump()

    if not owned:
        acc_id = add_account(
            {
                "label": settings_dict.get("sender_email") or "默认账号",
                "settings": settings_dict,
                "sync": sync_dict,
                "owner_user_id": user.id,
            }
        )
        set_user_active_account(user.id, acc_id)
    else:
        target = _active_account_for(user)
        update_account(
            target["id"],
            {"settings": settings_dict, "sync": sync_dict},
        )
    return {"status": "ok"}


# -------- accounts --------


@app.get("/api/accounts")
def api_list_accounts(user: User = Depends(current_user)) -> Dict:
    return {
        "active_account_id": get_user_active_account_id(user.id),
        "accounts": _visible_accounts(user),
    }


@app.get("/api/accounts/{account_id}", response_model=Account)
def api_get_account(
    account_id: str, user: User = Depends(current_user)
) -> Account:
    """Single-account read. Admin OR owner; used by /admin when editing
    another user's account and by /settings in ?edit_account= mode."""
    acc = _assert_owner_or_admin(account_id, user)
    return Account(**acc)


@app.get("/api/users/{user_id}/accounts")
def admin_list_user_accounts(
    user_id: str, _: User = Depends(require_admin)
) -> Dict:
    """Admin-only: list email accounts owned by a specific user. Powers
    the per-user mailbox section on /admin. Active-account id is included
    for read-only display ('当前激活: X')."""
    target = get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在。")
    owned = [a for a in list_accounts() if a.get("owner_user_id") == user_id]
    return {
        "owner": {"id": target["id"], "username": target.get("username", "")},
        "active_account_id": target.get("active_account_id"),
        "accounts": owned,
    }


@app.post("/api/accounts", response_model=Account)
def api_create_account(
    payload: AccountCreate, user: User = Depends(current_user)
) -> Account:
    settings_dict = payload.settings.model_dump()
    acc_id = add_account(
        {
            "label": (payload.label or "").strip()
            or settings_dict.get("sender_email")
            or "新账号",
            "settings": settings_dict,
            "sync": (payload.sync or SyncSettings()).model_dump(),
            "owner_user_id": user.id,
        }
    )
    if not get_user_active_account_id(user.id):
        set_user_active_account(user.id, acc_id)
    return Account(**get_account(acc_id))


@app.put("/api/accounts/{account_id}", response_model=Account)
def api_update_account(
    account_id: str, payload: AccountUpdate, user: User = Depends(current_user)
) -> Account:
    _assert_owner_or_admin(account_id, user)
    fields: Dict = {}
    if payload.label is not None:
        fields["label"] = payload.label.strip() or "未命名账号"
    if payload.settings is not None:
        fields["settings"] = payload.settings.model_dump()
    if payload.sync is not None:
        fields["sync"] = payload.sync.model_dump()
    try:
        updated = update_account(account_id, fields)
    except ValueError:
        raise HTTPException(status_code=404, detail="账号不存在。")
    return Account(**updated)


@app.delete("/api/accounts/{account_id}")
def api_delete_account(
    account_id: str, _: User = Depends(require_admin)
) -> Dict[str, str]:
    """Per spec, only admins can delete email accounts."""
    if get_account(account_id) is None:
        raise HTTPException(status_code=404, detail="账号不存在。")
    delete_account(account_id)
    # Clear stale active_account_id references on any user.
    for u in list_users():
        if u.get("active_account_id") == account_id:
            update_user(u["id"], {"active_account_id": None})
    return {"status": "ok"}


@app.post("/api/accounts/{account_id}/activate")
def api_activate_account(
    account_id: str, user: User = Depends(current_user)
) -> Dict[str, str]:
    # Owner-only intentionally. Activate means "operate my client against
    # this mailbox" — letting one user (even an admin) point themselves at
    # someone else's inbox would be impersonation.
    _assert_owner_only(account_id, user)
    set_user_active_account(user.id, account_id)
    return {"status": "ok"}


def _sync_settings_for(user: User) -> SyncSettings:
    acc = _active_account_for(user)
    return SyncSettings(**(acc.get("sync") or {}))


def _bg_sync_flags(
    account_id: str,
    mailbox: Optional[str],
    uid: Optional[str],
    seen: Optional[bool] = None,
    answered: Optional[bool] = None,
) -> None:
    if not uid:
        return
    acc = get_account(account_id)
    settings = (acc or {}).get("settings")
    if not settings:
        return
    add: List[str] = []
    remove: List[str] = []
    if seen is True:
        add.append("\\Seen")
    elif seen is False:
        remove.append("\\Seen")
    if answered is True:
        add.append("\\Answered")
    elif answered is False:
        remove.append("\\Answered")
    if not add and not remove:
        return
    try:
        imap_set_flags(settings, mailbox or "INBOX", uid, add=add, remove=remove)
    except Exception:
        pass  # best-effort


def _bg_sync_deleted(
    account_id: str, mailbox: Optional[str], uid: Optional[str], deleted: bool
) -> None:
    if not uid:
        return
    acc = get_account(account_id)
    settings = (acc or {}).get("settings")
    if not settings:
        return
    try:
        if deleted:
            imap_set_flags(
                settings, mailbox or "INBOX", uid, add=["\\Deleted"], remove=None
            )
        else:
            imap_set_flags(
                settings, mailbox or "INBOX", uid, add=None, remove=["\\Deleted"]
            )
    except Exception:
        pass


def _bg_expunge_uid(account_id: str, mailbox: Optional[str], uid: Optional[str]) -> None:
    if not uid:
        return
    acc = get_account(account_id)
    settings = (acc or {}).get("settings")
    if not settings:
        return
    try:
        imap_expunge_uid(settings, mailbox or "INBOX", uid)
    except Exception:
        pass


def _bg_sync_move(
    account_id: str,
    src_mailbox: Optional[str],
    uid: Optional[str],
    dst_path: Optional[str],
) -> None:
    if not uid or not dst_path:
        return
    acc = get_account(account_id)
    settings = (acc or {}).get("settings")
    if not settings:
        return
    try:
        imap_move_uid(settings, src_mailbox or "INBOX", uid, dst_path)
    except Exception:
        pass


def _bg_append_sent(account_id: str, raw_message: bytes) -> None:
    acc = get_account(account_id)
    settings = (acc or {}).get("settings")
    if not settings:
        return
    try:
        imap_append_sent(settings, raw_message)
    except Exception:
        pass


@app.post("/api/send", response_model=SendResult)
def send_mail(
    payload: SendEmailRequest,
    background: BackgroundTasks,
    user: User = Depends(current_user),
) -> SendResult:
    acc = _active_account_for(user)
    settings = acc["settings"]
    active_id = acc["id"]

    # Collect attachments to actually send (read bytes from disk).
    send_attachments: List[Dict] = []
    seen_names: set = set()
    attach_meta: List[Dict] = []

    def _gather_from(source_id: str) -> None:
        for meta in list_attachments_meta(source_id):
            name = meta["filename"]
            if name in seen_names:
                continue
            try:
                path = get_attachment_path(source_id, name)
            except ValueError:
                continue
            if not path.exists() or not path.is_file():
                continue
            send_attachments.append(
                {
                    "filename": name,
                    "data": path.read_bytes(),
                    "content_type": meta.get("content_type")
                    or "application/octet-stream",
                }
            )
            attach_meta.append(
                {
                    "filename": name,
                    "size": meta.get("size", path.stat().st_size),
                    "content_type": meta.get("content_type")
                    or "application/octet-stream",
                }
            )
            seen_names.add(name)

    if payload.draft_id:
        _gather_from(payload.draft_id)
    if payload.attach_from_inbox_id:
        _gather_from(payload.attach_from_inbox_id)

    # We relaxed `to` from EmailStr to str so multi-recipient ("Reply All",
    # ad-hoc lists) is allowed. Validate per-address here so a typo still
    # fails fast with a clear 400.
    from app.services.email_client import _split_address_header
    _to_addrs = _split_address_header(payload.to or "")
    _cc_addrs = _split_address_header(payload.cc or "")
    _bcc_addrs = _split_address_header(payload.bcc or "")
    if not _to_addrs and not _cc_addrs and not _bcc_addrs:
        raise HTTPException(status_code=400, detail="收件人不能为空。")
    if not _to_addrs:
        # Some servers reject messages whose To header is empty; require it.
        raise HTTPException(
            status_code=400, detail="收件人（To）至少要有一个有效地址。"
        )

    try:
        raw_message = send_email(
            settings,
            payload.to,
            payload.subject,
            payload.body,
            attachments=send_attachments,
            cc=payload.cc or "",
            bcc=payload.bcc or "",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=_humanize_email_error("发送邮件", exc)
        ) from exc

    # Best-effort: mirror the sent message into the server's Sent folder.
    if _sync_settings_for(user).sync_sent and raw_message:
        background.add_task(_bg_append_sent, active_id, raw_message)

    sent_id = str(uuid.uuid4())
    # Threading: if the user is replying to an inbox email, copy its
    # source_message_id / message_id onto this sent record so the inbox
    # merge-view folds them into the same thread bucket. None on a plain
    # compose (not a reply).
    thread_source = None
    thread_in_reply_to = None
    if payload.reply_to_inbox_id:
        parent = next(
            (e for e in read_emails() if e.get("id") == payload.reply_to_inbox_id),
            None,
        )
        if parent:
            thread_source = parent.get("source_message_id") or parent.get(
                "message_id"
            )
            thread_in_reply_to = parent.get("message_id")
    sent_record = {
        "id": sent_id,
        "account_id": active_id,
        "from_email": settings.get("sender_email", ""),
        "to_email": payload.to,
        "cc_email": payload.cc or "",
        "bcc_email": payload.bcc or "",
        "subject": payload.subject,
        "body": payload.body,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "attachments": attach_meta,
        "reply_to_inbox_id": payload.reply_to_inbox_id or None,
        "source_message_id": thread_source,
        "in_reply_to": thread_in_reply_to,
    }

    # Relocate attachments to the sent folder (and merge in inbox carry-over).
    if payload.draft_id:
        move_attachments_folder(payload.draft_id, sent_id)
    if payload.attach_from_inbox_id:
        copy_attachments_folder(payload.attach_from_inbox_id, sent_id)
    # Refresh metadata from disk so the stored record matches what's on disk.
    sent_record["attachments"] = list_attachments_meta(sent_id)

    sent_items = read_sent()
    sent_items.append(sent_record)
    write_sent(sent_items)

    if payload.draft_id:
        drafts = [d for d in read_drafts() if d.get("id") != payload.draft_id]
        write_drafts(drafts)

    if payload.reply_to_inbox_id:
        emails = read_emails()
        for item in emails:
            if item.get("id") == payload.reply_to_inbox_id:
                item["replied"] = True
                break
        write_emails(emails)

    return SendResult(status="ok", detail="邮件发送成功")


@app.get("/api/drafts", response_model=List[DraftRecord])
def list_drafts(user: User = Depends(current_user)) -> List[DraftRecord]:
    active_id = get_user_active_account_id(user.id)
    items = [d for d in read_drafts() if d.get("account_id") == active_id]
    items.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return [DraftRecord(**item) for item in items]


@app.post("/api/drafts", response_model=DraftRecord)
def save_draft(
    payload: DraftPayload, user: User = Depends(current_user)
) -> DraftRecord:
    active_id = _active_account_id_for(user)
    to = (payload.to or "").strip()
    cc = (payload.cc or "").strip()
    bcc = (payload.bcc or "").strip()
    subject = payload.subject or ""
    body = payload.body or ""
    has_text = bool(to or cc or bcc or subject.strip() or body.strip())
    has_existing_attachments = bool(payload.id) and bool(
        list_attachments_meta(payload.id)
    )
    if not has_text and not payload.attach_from_inbox_id and not has_existing_attachments:
        raise HTTPException(status_code=400, detail="草稿内容为空，无需保存。")

    drafts = read_drafts()
    now = datetime.now(timezone.utc).isoformat()
    record = None

    if payload.id:
        for item in drafts:
            if item.get("id") == payload.id:
                item["to"] = to
                item["cc"] = cc
                item["bcc"] = bcc
                item["subject"] = subject
                item["body"] = body
                item["updated_at"] = now
                record = item
                break

    if record is None:
        record = {
            "id": str(uuid.uuid4()),
            "account_id": active_id,
            "to": to,
            "cc": cc,
            "bcc": bcc,
            "subject": subject,
            "body": body,
            "updated_at": now,
        }
        drafts.append(record)

    # Carry attachments from a source inbox email if requested.
    if payload.attach_from_inbox_id:
        copy_attachments_folder(payload.attach_from_inbox_id, record["id"])

    record["attachments"] = list_attachments_meta(record["id"])

    write_drafts(drafts)
    return DraftRecord(**record)


def _assert_record_belongs_to_user(record: Dict, user: User) -> None:
    """Block cross-user access to per-record assets (drafts, emails, sent
    items, and their attachments) by tracing back to the owning account."""
    if user.role == "admin":
        return
    acc_id = record.get("account_id") or ""
    if not acc_id:
        return  # legacy record without ownership info; treat as accessible
    acc = get_account(acc_id)
    if not acc or acc.get("owner_user_id") != user.id:
        raise HTTPException(status_code=403, detail="无权访问该记录。")


@app.delete("/api/drafts/{draft_id}")
def delete_draft(
    draft_id: str, user: User = Depends(current_user)
) -> Dict[str, str]:
    drafts = read_drafts()
    target = next((d for d in drafts if d.get("id") == draft_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="草稿不存在或已删除。")
    _assert_record_belongs_to_user(target, user)
    write_drafts([d for d in drafts if d.get("id") != draft_id])
    delete_attachments_folder(draft_id)
    return {"status": "ok"}


@app.post("/api/drafts/{draft_id}/attachments", response_model=Attachment)
async def upload_draft_attachment(
    draft_id: str,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
) -> Attachment:
    drafts = read_drafts()
    draft = next((d for d in drafts if d.get("id") == draft_id), None)
    if draft is None:
        raise HTTPException(status_code=404, detail="草稿不存在。")
    _assert_record_belongs_to_user(draft, user)

    content = await file.read()
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"附件超过 {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB 限制。",
        )

    saved = save_attachment_bytes(
        draft_id,
        file.filename or "attachment",
        content,
        file.content_type or "application/octet-stream",
    )
    draft["attachments"] = list_attachments_meta(draft_id)
    draft["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_drafts(drafts)
    return Attachment(**saved)


@app.delete("/api/drafts/{draft_id}/attachments/{filename}")
def delete_draft_attachment(
    draft_id: str, filename: str, user: User = Depends(current_user)
) -> Dict[str, str]:
    drafts = read_drafts()
    draft = next((d for d in drafts if d.get("id") == draft_id), None)
    if draft is None:
        raise HTTPException(status_code=404, detail="草稿不存在。")
    _assert_record_belongs_to_user(draft, user)
    try:
        removed = delete_attachment_file(draft_id, filename)
    except ValueError:
        raise HTTPException(status_code=400, detail="非法的附件名。")
    if not removed:
        raise HTTPException(status_code=404, detail="附件不存在。")
    draft["attachments"] = list_attachments_meta(draft_id)
    draft["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_drafts(drafts)
    return {"status": "ok"}


@app.get("/api/attachments/{record_id}/{filename}")
def download_attachment(
    record_id: str,
    filename: str,
    download: bool = Query(default=False),
    user: User = Depends(current_user),
) -> FileResponse:
    # Trace the record_id back to whichever email/draft/sent it belongs to,
    # then enforce ownership against the current user.
    record = (
        next((e for e in read_emails() if e.get("id") == record_id), None)
        or next((d for d in read_drafts() if d.get("id") == record_id), None)
        or next((s for s in read_sent() if s.get("id") == record_id), None)
    )
    if record is None:
        raise HTTPException(status_code=404, detail="附件不存在。")
    _assert_record_belongs_to_user(record, user)
    try:
        path = get_attachment_path(record_id, filename)
    except ValueError:
        raise HTTPException(status_code=400, detail="非法的附件路径。")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="附件不存在。")
    guessed_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    # Defense in depth: even with the path skip in ConditionalGZipMiddleware,
    # set Content-Encoding: identity so any future / external proxy doesn't
    # re-encode and break the on-the-wire size.
    headers = {"Content-Encoding": "identity"}
    # download=true forces an attachment prompt; otherwise let browsers decide
    # whether the content can be previewed inline.
    if download:
        return FileResponse(
            path,
            filename=path.name,
            media_type=guessed_type,
            headers=headers,
        )
    return FileResponse(path, media_type=guessed_type, headers=headers)


@app.get("/api/sent", response_model=List[SentRecord])
def list_sent(user: User = Depends(current_user)) -> List[SentRecord]:
    """Returns every sent record for the active account — including
    soft-deleted ones. The frontend filters by `deleted` to split
    已发送 from 回收站. Matches the /api/emails convention."""
    active_id = get_user_active_account_id(user.id)
    items = [s for s in read_sent() if s.get("account_id") == active_id]
    items.sort(key=lambda x: x.get("sent_at", ""), reverse=True)
    return [SentRecord(**item) for item in items]


@app.post("/api/sent/{sent_id}/update", response_model=SentRecord)
def update_sent(
    sent_id: str, payload: SentUpdate, user: User = Depends(current_user)
) -> SentRecord:
    """Soft-delete / restore a sent record. Sent message bodies are
    immutable, so the only field this route accepts today is `deleted`."""
    items = read_sent()
    target = next((s for s in items if s.get("id") == sent_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="已发送邮件不存在。")
    _assert_record_belongs_to_user(target, user)
    if payload.deleted is not None:
        target["deleted"] = bool(payload.deleted)
        target["deleted_at"] = (
            datetime.now(timezone.utc).isoformat() if target["deleted"] else None
        )
    write_sent(items)
    return SentRecord(**target)


@app.delete("/api/sent/{sent_id}")
def hard_delete_sent(
    sent_id: str, user: User = Depends(current_user)
) -> Dict[str, str]:
    """Permanently purge a sent record + its on-disk attachments. The
    regular 删除 button in 已发送 only soft-deletes; this route is the
    "彻底删除" path from 回收站."""
    items = read_sent()
    target = next((s for s in items if s.get("id") == sent_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="已发送邮件不存在。")
    _assert_record_belongs_to_user(target, user)
    new_items = [s for s in items if s.get("id") != sent_id]
    write_sent(new_items)
    delete_attachments_folder(sent_id)
    return {"status": "ok"}


@app.post("/api/receive", response_model=ReceiveResult)
def receive_mail(
    days: Optional[int] = Query(default=None, ge=1, le=100),
    user: User = Depends(current_user),
) -> ReceiveResult:
    acc = _active_account_for(user)
    settings = acc["settings"]
    active_id = acc["id"]

    # Per-account default lives in sync.fetch_days; explicit ?days= overrides.
    if days is None:
        sync_cfg = acc.get("sync") or {}
        days = int(sync_cfg.get("fetch_days") or SyncSettings().fetch_days)

    ctx = _classification_context(active_id)
    sync_state = get_account_sync_state(active_id)
    # UIDs we've already stored for this account — receive_emails uses this
    # to skip mail it would otherwise redownload + reclassify, mainly in
    # widened-window fetches (see receive_emails docstring, "Known-UID skip").
    known_uids = {
        str(e.get("imap_uid"))
        for e in read_emails()
        if e.get("account_id") == active_id and e.get("imap_uid")
    }

    try:
        fetched, sync_meta = receive_emails(
            settings=settings,
            days=days,
            sync_state=sync_state,
            known_uids=known_uids,
            **ctx,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=_humanize_email_error("收取邮件", exc)
        ) from exc

    # Stamp the fetched records with the active account so dedupe/filtering
    # stays scoped to this account and never collides with another mailbox
    # that happens to have seen the same Message-Id.
    for item in fetched:
        item["account_id"] = active_id

    all_items = read_emails()
    own_old = [e for e in all_items if e.get("account_id") == active_id]
    other_old = [e for e in all_items if e.get("account_id") != active_id]
    merged, stored = dedupe_by_message_id(own_old, fetched)

    # Materialise pending attachments under each kept record's final id, then
    # strip the in-memory blobs so they don't leak into the on-disk JSON.
    finalized: List[Dict] = []
    for rec in merged:
        pending = rec.pop("_pending_attachments", None) or []
        if pending:
            for fname, data, ctype in pending:
                try:
                    save_attachment_bytes(rec["id"], fname, data, ctype)
                except Exception:
                    continue
            rec["attachments"] = list_attachments_meta(rec["id"])
        finalized.append(rec)

    write_emails(other_old + finalized)
    # Bump the watermark so the next click only fetches UIDs newer than this.
    if sync_meta.get("mailbox") and sync_meta.get("uidvalidity") and sync_meta.get("last_uid"):
        update_account_sync_state_entry(
            active_id,
            sync_meta["mailbox"],
            sync_meta["uidvalidity"],
            sync_meta["last_uid"],
            sync_meta.get("fetch_days_at", ""),
        )
    return ReceiveResult(status="ok", fetched=len(fetched), stored=stored)


@app.post("/api/receive/stream")
def receive_mail_stream(
    days: Optional[int] = Query(default=None, ge=1, le=100),
    user: User = Depends(current_user),
) -> StreamingResponse:
    """Streaming variant of /api/receive. Returns newline-delimited JSON
    (NDJSON) so the frontend can show progress as each email is classified.

    Event types:
      {"type": "connected"}
      {"type": "planned",      "total": N, "mode": "incremental"|"initial", "days": D}
      {"type": "classified",   "index": i, "total": N, "subject": ..., "from": ..., "category": ..., "reason": ...}
      {"type": "skipped",      "index": i, "total": N, "uid": ..., "reason": ...}
      {"type": "batch-saved",  "fetched": N, "stored": K}        # every batch_size emails
      {"type": "saved",        "fetched": N, "stored": K}        # final after-loop summary
      {"type": "done",         "fetched": N, "stored": K}        # always the last event
      {"type": "error",        "message": "..."}                  # terminal on failure
    """
    acc = _active_account_for(user)
    settings = acc["settings"]
    active_id = acc["id"]
    if days is None:
        sync_cfg = acc.get("sync") or {}
        days = int(sync_cfg.get("fetch_days") or SyncSettings().fetch_days)
    ctx = _classification_context(active_id)
    sync_state = get_account_sync_state(active_id)
    # See /api/receive for why we collect this — saves the streaming flow
    # from re-FETCHing + re-classifying mail already in the local store.
    known_uids = {
        str(e.get("imap_uid"))
        for e in read_emails()
        if e.get("account_id") == active_id and e.get("imap_uid")
    }

    # Inter-thread channel. receive_emails runs in a worker thread; the
    # generator (running in the request thread) drains events from this queue.
    q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=256)
    SENTINEL: Dict[str, Any] = {"__sentinel__": True}
    state: Dict[str, Any] = {
        "records": None,
        "sync_meta": None,
        "error": None,
        "cum_fetched": 0,
        "cum_stored": 0,
    }

    def progress(ev: Dict[str, Any]) -> None:
        try:
            q.put(ev, timeout=30)
        except Exception:
            pass

    def persist_batch(batch_records: List[Dict], partial_sync_meta: Dict[str, str]) -> None:
        """Dedupe + write + materialise attachments for ONE batch; bump the
        watermark; enqueue a batch-saved event so the UI refreshes.
        Reads/writes happen on the worker thread — fine, the storage layer
        does whole-file replacement and we never overlap with another
        receive on the same account."""
        if not batch_records:
            return
        for item in batch_records:
            item["account_id"] = active_id
        all_items = read_emails()
        own_old = [e for e in all_items if e.get("account_id") == active_id]
        other_old = [e for e in all_items if e.get("account_id") != active_id]
        merged, stored = dedupe_by_message_id(own_old, batch_records)

        finalized: List[Dict] = []
        for rec in merged:
            pending = rec.pop("_pending_attachments", None) or []
            if pending:
                for fname, data, ctype in pending:
                    try:
                        save_attachment_bytes(rec["id"], fname, data, ctype)
                    except Exception:
                        continue
                rec["attachments"] = list_attachments_meta(rec["id"])
            finalized.append(rec)

        write_emails(other_old + finalized)
        if (
            partial_sync_meta.get("mailbox")
            and partial_sync_meta.get("uidvalidity")
            and partial_sync_meta.get("last_uid")
        ):
            update_account_sync_state_entry(
                active_id,
                partial_sync_meta["mailbox"],
                partial_sync_meta["uidvalidity"],
                partial_sync_meta["last_uid"],
                partial_sync_meta.get("fetch_days_at", ""),
            )
        state["cum_fetched"] += len(batch_records)
        state["cum_stored"] += stored
        progress(
            {
                "type": "batch-saved",
                "fetched": state["cum_fetched"],
                "stored": state["cum_stored"],
            }
        )

    def worker() -> None:
        try:
            records, sync_meta = receive_emails(
                settings=settings,
                days=days,
                on_progress=progress,
                on_batch_ready=persist_batch,
                batch_size=10,
                sync_state=sync_state,
                known_uids=known_uids,
                **ctx,
            )
            state["records"] = records
            state["sync_meta"] = sync_meta
        except Exception as exc:
            state["error"] = _humanize_email_error("收取邮件", exc)
        finally:
            q.put(SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    def line(ev: Dict[str, Any]) -> bytes:
        return (json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8")

    def gen():
        while True:
            ev = q.get()
            if ev is SENTINEL:
                break
            yield line(ev)

        if state["error"]:
            yield line({"type": "error", "message": state["error"]})
            yield line({"type": "done", "fetched": 0, "stored": 0})
            return

        # All persistence happened batch-by-batch inside persist_batch().
        # The final sync_meta may differ slightly from the last batch's
        # (covers the leftover partial flush) — apply it once more so the
        # watermark exactly matches the highest UID we saw.
        sync_meta = state.get("sync_meta") or {}
        if (
            sync_meta.get("mailbox")
            and sync_meta.get("uidvalidity")
            and sync_meta.get("last_uid")
        ):
            update_account_sync_state_entry(
                active_id,
                sync_meta["mailbox"],
                sync_meta["uidvalidity"],
                sync_meta["last_uid"],
                sync_meta.get("fetch_days_at", ""),
            )
        yield line(
            {
                "type": "saved",
                "fetched": state["cum_fetched"],
                "stored": state["cum_stored"],
            }
        )
        yield line(
            {
                "type": "done",
                "fetched": state["cum_fetched"],
                "stored": state["cum_stored"],
            }
        )

    # NDJSON, not SSE — POST + cookie auth is awkward with EventSource.
    # X-Accel-Buffering disables nginx buffering; harmless when nginx is absent.
    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/debug/reset")
def debug_reset(_: User = Depends(require_admin)) -> Dict[str, int]:
    """Wipe received-email state. Admin-only because it cascades across whichever
    account is currently active in the admin's session.

    Drafts, sent mail, folder structure, account configs and other accounts'
    emails are deliberately left untouched.
    """
    active_id = get_user_active_account_id(_.id) or ""
    if not active_id:
        raise HTTPException(status_code=400, detail="没有激活账号。")

    all_items = read_emails()
    own = [e for e in all_items if e.get("account_id") == active_id]
    other = [e for e in all_items if e.get("account_id") != active_id]

    for rec in own:
        rid = rec.get("id")
        if rid:
            try:
                delete_attachments_folder(rid)
            except Exception:
                continue

    write_emails(other)
    # Drop the IMAP watermark too, otherwise the next 收取邮件 only pulls UIDs
    # newer than the wiped batch — which makes 复位 feel like it did nothing.
    clear_account_sync_state(active_id)
    return {"cleared": len(own)}


@app.post("/api/debug/repair-email-times/stream")
def debug_repair_email_times_stream(
    user: User = Depends(current_user),
) -> StreamingResponse:
    """Dev-only utility: backfill historical inbox records' received_at from
    IMAP INTERNALDATE / Date header for the active account."""
    if read_system_mode() == "prod":
        raise HTTPException(status_code=403, detail="仅开发态允许执行时间修正。")

    acc = _active_account_for(user)
    settings = acc["settings"]
    active_id = acc["id"]

    q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=256)
    SENTINEL: Dict[str, Any] = {"__sentinel__": True}
    state: Dict[str, Any] = {"stats": None, "error": None}

    def progress(ev: Dict[str, Any]) -> None:
        try:
            q.put(ev, timeout=30)
        except Exception:
            pass

    def worker() -> None:
        try:
            all_items = read_emails()
            own = [e for e in all_items if e.get("account_id") == active_id]
            other = [e for e in all_items if e.get("account_id") != active_id]
            stats = repair_email_received_times(
                settings=settings,
                records=own,
                on_progress=progress,
            )
            # Keep newest-first order after timestamp corrections.
            own.sort(key=lambda x: x.get("received_at", ""), reverse=True)
            write_emails(other + own)
            state["stats"] = stats
        except Exception as exc:
            state["error"] = _humanize_email_error("时间修正", exc)
        finally:
            q.put(SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    def line(ev: Dict[str, Any]) -> bytes:
        return (json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8")

    def gen():
        while True:
            ev = q.get()
            if ev is SENTINEL:
                break
            yield line(ev)
        if state["error"]:
            yield line({"type": "error", "message": state["error"]})
            yield line({"type": "done", "total": 0, "updated": 0, "skipped": 0})
            return
        stats = state["stats"] or {}
        yield line(
            {
                "type": "done",
                "total": int(stats.get("total") or 0),
                "scanned": int(stats.get("scanned") or 0),
                "updated": int(stats.get("updated") or 0),
                "unchanged": int(stats.get("unchanged") or 0),
                "skipped": int(stats.get("skipped") or 0),
            }
        )

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/emails", response_model=List[EmailRecord])
def list_emails(
    category: str = "",
    important: bool = False,
    user: User = Depends(current_user),
) -> List[EmailRecord]:
    """List emails for the active account.

    Returns a SLIM payload: `body_html` is omitted (~77% of the wire size
    on typical inboxes) and replaced with a boolean `has_html` flag. The
    plain-text `body` is kept so list-view snippets and full-text search
    still work without a round-trip. Clients fetch full body_html lazily
    via /api/emails/{id}/body when the user opens an email.

    `category` filters to one folder; `important=true` returns only emails
    flagged as important regardless of folder (the "重要邮件" view).
    """
    active_id = get_user_active_account_id(user.id)
    items = [e for e in read_emails() if e.get("account_id") == active_id]
    if important:
        items = [item for item in items if item.get("important")]
    elif category:
        items = [item for item in items if item.get("category") == category]
    # Stable sort: newest first, then bubble pinned to the top.
    items.sort(key=lambda x: x.get("received_at", ""), reverse=True)
    items.sort(key=lambda x: 0 if x.get("pinned") else 1)
    out: List[EmailRecord] = []
    for item in items:
        slim = dict(item)
        # Record whether HTML exists before stripping, so the client can
        # decide if it should offer the HTML tab.
        slim["has_html"] = bool(slim.get("body_html"))
        slim["body_html"] = None
        out.append(EmailRecord(**slim))
    return out


@app.get("/api/emails/{email_id}/body")
def get_email_body(
    email_id: str, user: User = Depends(current_user)
) -> Dict[str, Optional[str]]:
    """Lazy-load endpoint for the heavy `body_html` field stripped from
    the /api/emails list. Returns just the two body variants; the client
    merges these into its in-memory record on demand."""
    target = next((e for e in read_emails() if e.get("id") == email_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="邮件不存在。")
    _assert_record_belongs_to_user(target, user)
    return {
        "body": target.get("body") or "",
        "body_html": target.get("body_html") or None,
    }


@app.post("/api/emails/{email_id}/update", response_model=EmailRecord)
def update_email(
    email_id: str,
    payload: EmailUpdate,
    background: BackgroundTasks,
    user: User = Depends(current_user),
) -> EmailRecord:
    emails = read_emails()
    target = next((e for e in emails if e.get("id") == email_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="邮件不存在。")
    _assert_record_belongs_to_user(target, user)

    changed_read = changed_replied = changed_deleted = changed_category = False
    if payload.read is not None and bool(payload.read) != bool(target.get("read")):
        target["read"] = bool(payload.read)
        changed_read = True
    if payload.replied is not None and bool(payload.replied) != bool(
        target.get("replied")
    ):
        target["replied"] = bool(payload.replied)
        changed_replied = True
    if payload.pinned is not None:
        target["pinned"] = bool(payload.pinned)
    if payload.important is not None:
        target["important"] = bool(payload.important)
        # Un-marking 重要 implicitly clears 已处理 — the flag only makes
        # sense in the context of an active important mark.
        if not target["important"]:
            target["handled"] = False
    if payload.handled is not None:
        # Product rule: clicking「已处理」moves the email out of the active
        # 重要 list, but keeps `important=True` so it shows up in the
        # 曾经重要 archive and the distilled experience stays semantically
        # anchored to a still-important record. Un-marking 重要 (handled
        # elsewhere in this function) is the only path that clears both.
        target["handled"] = bool(payload.handled)
    if payload.deleted is not None and bool(payload.deleted) != bool(
        target.get("deleted")
    ):
        target["deleted"] = bool(payload.deleted)
        target["deleted_at"] = (
            datetime.now(timezone.utc).isoformat() if target["deleted"] else None
        )
        changed_deleted = True
    if payload.category is not None:
        cat = (payload.category or "").strip()
        if not cat:
            raise HTTPException(status_code=400, detail="分类不能为空。")
        if cat not in read_folders(target.get("account_id") or ""):
            raise HTTPException(status_code=400, detail=f"未知文件夹: {cat}")
        if cat != target.get("category"):
            old_category = target.get("category")
            target["category"] = cat
            target["_old_category"] = old_category  # used by background sync
            changed_category = True
    write_emails(emails)

    # Schedule best-effort IMAP sync if any tracked flag changed. The sync
    # settings come from the account that owns this email — not the active
    # one — so toggling flags on inbox items from a non-active account still
    # routes through the right credentials.
    target_account_id = target.get("account_id") or ""
    owner_acc = get_account(target_account_id)
    owner_sync = SyncSettings(**((owner_acc or {}).get("sync") or {}))
    if (changed_read or changed_replied) and owner_sync.sync_flags:
        background.add_task(
            _bg_sync_flags,
            target_account_id,
            target.get("imap_mailbox"),
            target.get("imap_uid"),
            seen=target.get("read") if changed_read else None,
            answered=target.get("replied") if changed_replied else None,
        )
    if changed_deleted and owner_sync.sync_deletes:
        background.add_task(
            _bg_sync_deleted,
            target_account_id,
            target.get("imap_mailbox"),
            target.get("imap_uid"),
            deleted=target.get("deleted"),
        )
    if changed_category and owner_sync.sync_folders:
        background.add_task(
            _bg_sync_move,
            target_account_id,
            target.get("imap_mailbox"),
            target.get("imap_uid"),
            target.get("category"),
        )

    target.pop("_old_category", None)
    return EmailRecord(**target)


@app.delete("/api/emails/{email_id}")
def hard_delete_email(
    email_id: str,
    background: BackgroundTasks,
    user: User = Depends(current_user),
) -> Dict[str, str]:
    emails = read_emails()
    target = next((e for e in emails if e.get("id") == email_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="邮件不存在。")
    _assert_record_belongs_to_user(target, user)
    new_emails = [e for e in emails if e.get("id") != email_id]
    write_emails(new_emails)
    # Clean up attachments
    delete_attachments_folder(email_id)

    # Best-effort: also EXPUNGE from server.
    target_account_id = target.get("account_id") or ""
    owner_acc = get_account(target_account_id)
    owner_sync = SyncSettings(**((owner_acc or {}).get("sync") or {}))
    if owner_sync.sync_deletes and target.get("imap_uid"):
        background.add_task(
            _bg_expunge_uid,
            target_account_id,
            target.get("imap_mailbox"),
            target.get("imap_uid"),
        )
    return {"status": "ok"}


# -------- folders --------

class FolderCreatePayload(BaseModel):
    name: str
    parent: Optional[str] = ""


class FolderReorderPayload(BaseModel):
    """Replace the active account's folder list with this exact ordering.
    Must contain the same set of paths as currently exist — no adds, no
    drops; reorder only."""

    folder_paths: List[str]


@app.get("/api/folders", response_model=List[str])
def list_folders(user: User = Depends(current_user)) -> List[str]:
    active_id = get_user_active_account_id(user.id) or ""
    return read_folders(active_id)


@app.post("/api/folders/reorder", response_model=List[str])
def reorder_folders_endpoint(
    payload: FolderReorderPayload, user: User = Depends(current_user)
) -> List[str]:
    """Persist a user-defined folder ordering. The frontend's ▲▼ buttons
    swap siblings and POST the full flat list (parent-before-child); we
    just save it verbatim and return it. The order in folders.json is
    what drives sidebar rendering — there is no auto-sort."""
    active_id = _active_account_id_for(user)
    current = read_folders(active_id)
    new_paths = [p.strip() for p in payload.folder_paths if isinstance(p, str)]
    if len(new_paths) != len(set(new_paths)):
        raise HTTPException(status_code=400, detail="排序列表中含有重复路径。")
    current_set = set(current)
    new_set = set(new_paths)
    if new_set != current_set:
        missing = sorted(current_set - new_set)
        extra = sorted(new_set - current_set)
        raise HTTPException(
            status_code=400,
            detail=(
                "排序列表与当前文件夹不一致。"
                f"缺失: {missing or '无'}; 多余: {extra or '无'}。"
            ),
        )
    write_folders(new_paths, active_id)
    return new_paths


@app.post("/api/folders")
def create_folder(
    payload: FolderCreatePayload, user: User = Depends(current_user)
) -> Dict[str, str]:
    active_id = _active_account_id_for(user)
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="文件夹名称不能为空。")
    if "/" in name:
        raise HTTPException(status_code=400, detail="文件夹名称不能包含 /。")
    if len(name) > 64:
        raise HTTPException(status_code=400, detail="文件夹名称过长（最长 64 字符）。")
    parent = (payload.parent or "").strip().strip("/")
    folders = read_folders(active_id)
    if parent and parent not in folders:
        raise HTTPException(status_code=400, detail="父文件夹不存在。")
    full_path = f"{parent}/{name}" if parent else name
    if full_path in folders:
        raise HTTPException(status_code=409, detail="同名文件夹已存在。")
    folders.append(full_path)
    write_folders(folders, active_id)
    return {"status": "ok", "path": full_path}


@app.delete("/api/folders/{path:path}")
def delete_folder(
    path: str, user: User = Depends(current_user)
) -> Dict[str, Any]:
    """Delete a custom folder. Any emails still living in it are reassigned
    to「未分类」so they remain in a real, visible folder rather than
    becoming orphaned. (Project policy: every email belongs to some
    folder; "no folder" is itself the 未分类 bucket.)

    Default folders and folders with surviving subfolders are still
    rejected so the user has to be intentional about a recursive cleanup.
    """
    active_id = _active_account_id_for(user)
    folders = read_folders(active_id)
    if path not in folders:
        raise HTTPException(status_code=404, detail="文件夹不存在。")
    if path in DEFAULT_FOLDERS:
        raise HTTPException(status_code=400, detail="默认文件夹不可删除。")
    if any(f != path and f.startswith(path + "/") for f in folders):
        raise HTTPException(status_code=400, detail="请先删除其子文件夹。")

    all_emails = read_emails()
    moved = 0
    for rec in all_emails:
        if rec.get("account_id") != active_id:
            continue
        if rec.get("category") == path:
            rec["category"] = UNCLASSIFIED_FOLDER
            moved += 1
    if moved:
        write_emails(all_emails)

    new_folders = [f for f in folders if f != path]
    write_folders(new_folders, active_id)
    return {"status": "ok", "moved": moved}


# -------- contacts --------
#
# Per-account address book. Everything is scoped to the *active* account
# for the calling user — no cross-account leakage. Records dedupe on
# (account_id, lower(email)), so saving the same address twice updates the
# existing record instead of creating a duplicate.


def _normalize_tags(tags) -> List[str]:
    """Trim, drop empties, dedupe (case-insensitive but preserve first
    casing). Cap at 16 tags and 32 chars each so a runaway client can't
    blow the page up."""
    if not tags:
        return []
    seen: set = set()
    out: List[str] = []
    for raw in tags:
        t = str(raw or "").strip()
        if not t:
            continue
        if len(t) > 32:
            t = t[:32]
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
        if len(out) >= 16:
            break
    return out


@app.get("/api/contacts", response_model=List[Contact])
def api_list_contacts(user: User = Depends(current_user)) -> List[Contact]:
    acc_id = _active_account_id_for(user)
    items = list_contacts_for_account(acc_id)
    # Stable order: by name (case-insensitive), then email.
    items.sort(key=lambda c: ((c.get("name") or "").lower(), c.get("email") or ""))
    return [Contact(**c) for c in items]


@app.post("/api/contacts", response_model=Contact)
def api_create_contact(
    payload: ContactCreate, user: User = Depends(current_user)
) -> Contact:
    acc_id = _active_account_id_for(user)
    email = str(payload.email).strip()
    if not email:
        raise HTTPException(status_code=400, detail="邮箱地址不能为空。")
    # Dedupe: if an entry with this email already exists for the account,
    # merge in the incoming fields rather than create a second record. This
    # keeps the picker clean when the user adds the same address from
    # multiple entry points.
    existing = find_contact_by_email(acc_id, email)
    now = datetime.now(timezone.utc).isoformat()
    if existing:
        patch = {
            "name": payload.name or existing.get("name") or "",
            "email": email,
            "tags": _normalize_tags(payload.tags) or existing.get("tags") or [],
            "note": payload.note or existing.get("note") or "",
            "updated_at": now,
        }
        updated = update_contact(existing["id"], patch)
        return Contact(**updated)
    record = {
        "id": "",  # add_contact fills this
        "account_id": acc_id,
        "name": (payload.name or "").strip(),
        "email": email,
        "tags": _normalize_tags(payload.tags),
        "note": (payload.note or "").strip(),
        "created_at": now,
        "updated_at": now,
    }
    saved = add_contact(record)
    return Contact(**saved)


@app.patch("/api/contacts/{contact_id}", response_model=Contact)
def api_update_contact(
    contact_id: str,
    payload: ContactUpdate,
    user: User = Depends(current_user),
) -> Contact:
    acc_id = _active_account_id_for(user)
    existing = get_contact(contact_id)
    if not existing or (existing.get("account_id") or "") != acc_id:
        raise HTTPException(status_code=404, detail="联系人不存在。")
    patch: Dict = {"updated_at": datetime.now(timezone.utc).isoformat()}
    if payload.name is not None:
        patch["name"] = payload.name.strip()
    if payload.email is not None:
        new_email = str(payload.email).strip()
        # If the new address collides with a different contact, refuse —
        # the user should delete the dup explicitly rather than have us
        # silently merge two distinct records.
        clash = find_contact_by_email(acc_id, new_email)
        if clash and clash["id"] != contact_id:
            raise HTTPException(
                status_code=400, detail="该邮箱已存在于通讯录中。"
            )
        patch["email"] = new_email
    if payload.tags is not None:
        patch["tags"] = _normalize_tags(payload.tags)
    if payload.note is not None:
        patch["note"] = payload.note.strip()
    updated = update_contact(contact_id, patch)
    return Contact(**updated)


@app.delete("/api/contacts/{contact_id}")
def api_delete_contact(
    contact_id: str, user: User = Depends(current_user)
) -> Dict:
    acc_id = _active_account_id_for(user)
    existing = get_contact(contact_id)
    if not existing or (existing.get("account_id") or "") != acc_id:
        raise HTTPException(status_code=404, detail="联系人不存在。")
    delete_contact(contact_id)
    return {"status": "ok"}


@app.get("/api/contacts/tags", response_model=List[str])
def api_list_contact_tags(user: User = Depends(current_user)) -> List[str]:
    """Distinct tags in use under the active account — drives the sidebar
    filter on /contacts. Sorted case-insensitively."""
    acc_id = _active_account_id_for(user)
    seen: set = set()
    out: List[str] = []
    for c in list_contacts_for_account(acc_id):
        for t in c.get("tags") or []:
            key = str(t or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(t)
    out.sort(key=lambda s: s.lower())
    return out


@app.post("/api/diagnose")
def diagnose_connection(user: User = Depends(current_user)) -> Dict:
    settings = _active_settings_for(user)

    result = diagnose_email_connection(settings)
    smtp = result["smtp"]
    imap = result["imap"]

    smtp_message = (
        "SMTP 诊断通过"
        if smtp["ok"]
        else _humanize_email_error("SMTP 诊断", RuntimeError(smtp["detail"]))
    )
    imap_message = (
        "IMAP 诊断通过"
        if imap["ok"]
        else _humanize_email_error("IMAP 诊断", RuntimeError(imap["detail"]))
    )

    return {
        "status": "ok",
        "smtp": {"ok": smtp["ok"], "message": smtp_message, "raw_detail": smtp["detail"]},
        "imap": {"ok": imap["ok"], "message": imap_message, "raw_detail": imap["detail"]},
    }
