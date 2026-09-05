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
import re
import signal
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

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
    ExperienceOrganizeAction,
    ExperienceOrganizePlan,
    PromptOrganizeAction,
    PromptOrganizePlan,
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
    ActiveTaskResponse,
    ReceiveResult,
    ReceiveTaskStartRequest,
    RecategorizeConfirmRequest,
    RecategorizeRequest,
    RecategorizeResult,
    ImportanceSuggestRequest,
    RecategorizeSuggestRequest,
    RecategorizeSuggestion,
    TaskInfo,
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
    read_session_token,
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
from app.services import task_registry
from app.services.task_registry import TaskCancelled, TaskControl
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
    delete_draft as storage_delete_draft,
    delete_email as storage_delete_email,
    delete_experience,
    delete_fixed_rule,
    delete_prompt,
    delete_sent as storage_delete_sent,
    delete_user,
    find_contact_by_email,
    get_account,
    get_account_sync_state,
    get_attachment_path,
    get_contact,
    get_draft,
    get_email,
    get_experience,
    get_fixed_rule,
    get_prompt,
    get_sent,
    get_user,
    get_user_active_account_id,
    get_user_by_username,
    has_any_user,
    list_accounts,
    list_attachments_meta,
    list_contacts_for_account,
    list_drafts_for_account,
    list_emails_for_account,
    list_experiences_for_account,
    list_fixed_rules_for_account,
    list_known_imap_uids,
    list_prompts_for_account,
    list_sent_for_account,
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
    upsert_draft,
    upsert_email,
    upsert_emails,
    upsert_sent,
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

# Endpoints that own the session cookie themselves — login/setup/register
# set a fresh cookie, logout deletes it. The sliding-TTL middleware skips
# them so its refresh doesn't fight the endpoint's own Set-Cookie.
_SESSION_COOKIE_OWNER_PATHS = frozenset(
    {"/api/auth/login", "/api/auth/logout", "/api/auth/setup", "/api/auth/register"}
)


@app.middleware("http")
async def slide_session_cookie_expiry(request: Request, call_next):
    """Keep the user signed in for as long as they're actively using the
    app: on every authenticated request, re-issue the session cookie with
    a fresh `max_age` so the 30-day window rolls forward. Without this,
    even with a persistent webview store the cookie eventually hits its
    original expiry and the user gets bounced to the login screen."""
    response = await call_next(request)
    if request.url.path in _SESSION_COOKIE_OWNER_PATHS:
        return response
    session = request.cookies.get(SESSION_COOKIE_NAME)
    if not session:
        return response
    uid = read_session_token(session)
    if not uid:
        return response
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=make_session_token(uid),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


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


# WKWebView (pywebview, private_mode=False) persists its URLCache across
# launches, so without explicit no-cache headers the desktop app can keep
# serving a STALE index.html long after a pkg upgrade has dropped fresh
# HTML on disk — users then see "the new feature isn't there" because the
# webview painted last week's bundle. Apply to every server-rendered HTML
# entrypoint; static assets (logo / favicon / i18n.js) are fine to cache.
_NO_HTML_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/")
def home() -> FileResponse:
    if not WEB_INDEX.exists():
        raise HTTPException(status_code=404, detail="Web page not found.")
    return FileResponse(WEB_INDEX, headers=_NO_HTML_CACHE_HEADERS)


@app.get("/settings")
def settings_page() -> FileResponse:
    if not WEB_SETTINGS.exists():
        raise HTTPException(status_code=404, detail="Settings page not found.")
    return FileResponse(WEB_SETTINGS, headers=_NO_HTML_CACHE_HEADERS)


@app.get("/health")
def health() -> Dict[str, str]:
    # `data_dir` is included so the desktop launcher can tell whether an
    # already-running backend on port 8000 is bound to the same data
    # directory the user just chose. Without this, an orphan uvicorn left
    # over from a previous install would silently serve requests against
    # the wrong users.json after an upgrade, and login would fail.
    from app.storage import DATA_DIR

    return {"status": "ok", "data_dir": str(DATA_DIR)}


WEB_LOGIN = WEB_DIR / "login.html"
WEB_ADMIN = WEB_DIR / "admin.html"
WEB_CONTACTS = WEB_DIR / "contacts.html"


@app.get("/login")
def login_page() -> FileResponse:
    if not WEB_LOGIN.exists():
        raise HTTPException(status_code=404, detail="Login page not found.")
    return FileResponse(WEB_LOGIN, headers=_NO_HTML_CACHE_HEADERS)


@app.get("/admin")
def admin_page() -> FileResponse:
    # Auth check happens client-side via /api/auth/me; this just serves the HTML.
    if not WEB_ADMIN.exists():
        raise HTTPException(status_code=404, detail="Admin page not found.")
    return FileResponse(WEB_ADMIN, headers=_NO_HTML_CACHE_HEADERS)


@app.get("/contacts")
def contacts_page() -> FileResponse:
    if not WEB_CONTACTS.exists():
        raise HTTPException(status_code=404, detail="Contacts page not found.")
    return FileResponse(WEB_CONTACTS, headers=_NO_HTML_CACHE_HEADERS)


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


# -------- forgotten-password recovery (filesystem challenge) -----------
#
# XEmail runs as a local desktop app. The security boundary that
# separates "the actual owner" from "a network attacker who found port
# 8000" is *filesystem access to the data directory* — the same data
# dir where password hashes, sessions, and API keys already live.
# A network attacker can't read files there without user privileges;
# the owner can trivially.
#
# So the recovery flow is: on request, write a random 6-digit code to
# a 0600 challenge file inside the data dir; the user opens the file
# through the OS, reads the code, types it back in the modal along
# with a new password. A remote attacker triggering the /request
# endpoint gets nothing they can use — the response only names the
# file path, never the code itself.

_PW_RESET_CHALLENGE_FILENAME = ".password_reset_challenge"
_PW_RESET_TTL_SECONDS = 300  # 5 minutes


def _pw_reset_challenge_path() -> Path:
    from app.storage import DATA_DIR
    return Path(DATA_DIR) / _PW_RESET_CHALLENGE_FILENAME


def _write_pw_reset_challenge(username: str) -> "tuple[Path, int]":
    """Create/overwrite the challenge file with a fresh random code.
    Returns (path, ttl_seconds). File is written with 0600 perms."""
    import secrets as _secrets
    code = f"{_secrets.randbelow(1_000_000):06d}"
    payload = {
        "username": username,
        "code": code,
        "created_at": _now_iso(),
        "expires_at": (datetime.now(timezone.utc)
                       + timedelta(seconds=_PW_RESET_TTL_SECONDS)).isoformat(),
    }
    path = _pw_reset_challenge_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Two-step write so a partial write can't leave the previous code
    # in place: write to .tmp, chmod 0600, rename atomically.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return path, _PW_RESET_TTL_SECONDS


def _consume_pw_reset_challenge(username: str, code: str) -> None:
    """Validate the code against the on-disk challenge and delete it.
    Raises HTTPException on any mismatch — mismatches are deliberately
    reported with the same message so a probe can't distinguish a
    missing file from a wrong code from an expired code."""
    path = _pw_reset_challenge_path()
    generic = HTTPException(
        status_code=400,
        detail="验证码错误或已过期,请重新申请。",
    )
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise generic
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise generic
    if not isinstance(data, dict):
        raise generic
    if (data.get("username") or "") != username:
        raise generic
    if (data.get("code") or "") != code:
        raise generic
    exp_raw = data.get("expires_at") or ""
    try:
        exp = datetime.fromisoformat(exp_raw)
    except ValueError:
        raise generic
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        try:
            path.unlink()
        except OSError:
            pass
        raise generic
    # Success — burn the challenge so the code can't be reused.
    try:
        path.unlink()
    except OSError:
        pass


class PasswordResetRequest(BaseModel):
    username: str


class PasswordResetComplete(BaseModel):
    username: str
    code: str
    new_password: str


@app.post("/api/auth/password-reset/request")
def auth_password_reset_request(payload: PasswordResetRequest) -> Dict[str, Any]:
    """Create a fresh challenge code for the given username and drop it
    into `<data_dir>/.password_reset_challenge`. Returns the path the
    user must open — NEVER the code itself. Always returns success even
    when the username doesn't exist, so a remote probe can't enumerate
    valid usernames."""
    uname = (payload.username or "").strip()
    if not uname:
        raise HTTPException(status_code=400, detail="用户名不能为空。")
    # Whether or not the user exists, produce a challenge file so the
    # response shape stays constant. Only when the /complete step
    # actually looks up the user do we branch — but by then the code
    # verification already gates the sensitive path.
    path, ttl = _write_pw_reset_challenge(uname)
    return {
        "status": "ok",
        "challenge_path": str(path),
        "expires_in": ttl,
        "hint": "已在上述文件生成验证码,请打开该文件查看,然后回本页面输入。",
    }


@app.post("/api/auth/password-reset/complete", response_model=User)
def auth_password_reset_complete(
    payload: PasswordResetComplete,
    response: Response,
) -> User:
    """Verify the code from the on-disk challenge and set the user's
    new password hash. On success also drops a fresh session cookie so
    the user is logged in immediately."""
    uname = (payload.username or "").strip()
    code = (payload.code or "").strip()
    new_pw = (payload.new_password or "")
    if not uname or not code:
        raise HTTPException(status_code=400, detail="用户名和验证码不能为空。")
    if len(new_pw) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位。")
    _consume_pw_reset_challenge(uname, code)

    record = get_user_by_username(uname)
    if not record:
        raise HTTPException(status_code=404, detail="用户不存在。")
    update_user(record["id"], {"password_hash": hash_password(new_pw)})
    _set_session_cookie(response, record["id"])
    fresh = get_user(record["id"]) or record
    return User(
        id=fresh["id"],
        username=fresh["username"],
        role=fresh.get("role", "normal"),
        active_account_id=fresh.get("active_account_id"),
        created_at=fresh.get("created_at", ""),
    )


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


# ── Admin: classification history ─────────────────────────────────────
# Every classify_email_record call appends one entry to the target
# email's `classification_trace` list. These two endpoints let an admin
# browse that history across all accounts so misclassifications can be
# investigated ("why did this land in 未分类?" — check the last trace).


@app.get("/api/admin/classification-history")
def admin_list_classification_history(
    account_id: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    stage: str = Query(default=""),           # "" | "fixed_rule" | "llm" | "fallback"
    _: User = Depends(require_admin),
) -> Dict[str, Any]:
    """Paginated list of emails with at least one classification-trace entry.
    Newest-classified-first, cross-account by default.

    Response shape (kept flat so the UI can render a table without extra
    fetches):
        {
          "total": <int>,     # count matching the current filter
          "items": [{
            "email_id": ..., "account_id": ..., "subject": ...,
            "from_email": ..., "received_at": ...,
            "final_category": ..., "final_important": ...,
            "last_trace_ts": ..., "last_trace_stage": ...,
            "last_trace_reason": ..., "trace_count": <int>
          }]
        }"""
    # Load all emails once. For very large mailboxes this is fine — read
    # returns a python list; the trace field lives inside data_json.
    if account_id:
        all_emails = list_emails_for_account(account_id)
    else:
        all_emails = read_emails()

    # Filter to those that actually have a trace. Sort newest-first by the
    # latest trace timestamp so freshly-classified mail shows up on top.
    matched: List[Dict] = []
    for e in all_emails:
        traces = e.get("classification_trace") or []
        if not traces:
            continue
        last = traces[-1] if isinstance(traces[-1], dict) else {}
        if stage and last.get("stage") != stage:
            continue
        matched.append((e, traces, last))
    matched.sort(key=lambda t: (t[2].get("ts") or ""), reverse=True)
    total = len(matched)
    page = matched[offset : offset + limit]

    items = []
    for e, traces, last in page:
        items.append({
            "email_id": e.get("id") or "",
            "account_id": e.get("account_id") or "",
            "subject": (e.get("subject") or "")[:200],
            "from_email": e.get("from_email") or "",
            "received_at": e.get("received_at") or "",
            "final_category": e.get("category") or "",
            "final_important": bool(e.get("important")),
            "last_trace_ts": last.get("ts") or "",
            "last_trace_stage": last.get("stage") or "",
            "last_trace_reason": last.get("reason") or "",
            "trace_count": len(traces),
        })
    return {"total": total, "items": items}


@app.get("/api/admin/classification-history/{email_id}")
def admin_get_classification_history(
    email_id: str,
    _: User = Depends(require_admin),
) -> Dict[str, Any]:
    """Full trace history for one email — every entry in the order it was
    appended (oldest → newest). Includes email metadata so the admin page
    doesn't need a second /api/emails/{id} fetch."""
    e = get_email(email_id)
    if not e:
        raise HTTPException(status_code=404, detail="邮件不存在。")
    return {
        "email_id": e.get("id") or "",
        "account_id": e.get("account_id") or "",
        "subject": e.get("subject") or "",
        "from_email": e.get("from_email") or "",
        "to_email": e.get("to_email") or "",
        "received_at": e.get("received_at") or "",
        "final_category": e.get("category") or "",
        "final_important": bool(e.get("important")),
        "traces": e.get("classification_trace") or [],
    }


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
        mark_important=bool(r.get("mark_important")),
        unmark_important=bool(r.get("unmark_important")),
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


# ── Experience one-click organize: dedup + conflict resolution via LLM. ──
# Two-step so the user always sees what will change before it happens:
#   POST /api/experiences/organize/preview  → returns the plan, no mutation
#   POST /api/experiences/organize/apply    → takes the plan back and executes
# The apply step re-validates every id against the current account so the
# preview can't be replayed against a different account or against experiences
# that have since been deleted / added.

@app.post(
    "/api/experiences/organize/preview",
    response_model=ExperienceOrganizePlan,
)
def preview_organize_experiences(
    user: User = Depends(current_user),
) -> ExperienceOrganizePlan:
    active_id = _active_account_id_for(user)
    experiences = list_experiences_for_account(active_id)
    if len(experiences) < 2:
        return ExperienceOrganizePlan(actions=[])
    from app.services.spam_filter import distill_organize_experiences

    plan = distill_organize_experiences(
        [{"id": e.get("id") or "", "text": e.get("text") or ""} for e in experiences]
    )
    return ExperienceOrganizePlan(**plan)


@app.post(
    "/api/experiences/organize/apply",
    response_model=Dict[str, Any],
)
def apply_organize_experiences(
    plan: ExperienceOrganizePlan,
    user: User = Depends(current_user),
) -> Dict[str, Any]:
    active_id = _active_account_id_for(user)
    existing_by_id = {
        e["id"]: e for e in list_experiences_for_account(active_id) if e.get("id")
    }
    ids_to_delete: List[str] = []
    texts_to_add: List[str] = []
    for action in plan.actions:
        if action.type == "drop":
            for eid in action.ids:
                if eid not in existing_by_id:
                    raise HTTPException(
                        status_code=400,
                        detail=f"经验 {eid} 已不存在(可能被其他会话修改),请重新整理。",
                    )
                ids_to_delete.append(eid)
        elif action.type == "merge":
            if len(action.from_ids) < 2 or not action.new_text.strip():
                continue
            for eid in action.from_ids:
                if eid not in existing_by_id:
                    raise HTTPException(
                        status_code=400,
                        detail=f"经验 {eid} 已不存在(可能被其他会话修改),请重新整理。",
                    )
                ids_to_delete.append(eid)
            texts_to_add.append(action.new_text.strip()[:240])
        # keep: no-op

    # Execute: delete first (frees name/slug room in case of any conflict),
    # then insert merged replacements.
    for eid in ids_to_delete:
        try:
            delete_experience(eid)
        except Exception:  # noqa: BLE001
            # A concurrent delete would have raised 400 above; anything else
            # is unusual — log and continue so the rest of the plan applies.
            logger.warning("organize/apply: failed to delete %s", eid)
    added_ids: List[str] = []
    for text in texts_to_add:
        rec = add_experience(
            {
                "account_id": active_id,
                "user_id": user.id,
                "text": text,
                "source": "organize",
                "source_email_id": None,
                "created_at": _now_iso(),
                "updated_at": None,
            }
        )
        if isinstance(rec, dict) and rec.get("id"):
            added_ids.append(rec["id"])
    return {
        "status": "ok",
        "deleted": len(ids_to_delete),
        "merged_into": len(added_ids),
        "added_ids": added_ids,
    }


# ── Single-email reclassify (right-click 「重新智能分类」) ───────────
# The bulk 执行分类 / 重新分类 endpoints (see the task-registry code
# above) rerun the classifier over the whole account. This one-shot
# endpoint applies the same "clean-slate reclassify" semantics to a
# single email — resets `category` and `important` first, then runs
# `classify_email_record` against the current rules, prompts,
# experiences, and field config. Useful when the user has just added
# a fixed rule / prompt / experience and wants to test it on one
# specific email without re-running the whole batch.
#
# Runs synchronously — one email is a single LLM call at worst, no
# progress bar needed.

@app.post(
    "/api/emails/{email_id}/reclassify",
    response_model=EmailRecord,
)
def reclassify_single_email(
    email_id: str,
    user: User = Depends(current_user),
) -> EmailRecord:
    target = get_email(email_id)
    if target is None:
        raise HTTPException(status_code=404, detail="邮件不存在。")
    _assert_record_belongs_to_user(target, user)

    account_id = target.get("account_id") or ""
    ctx = _classification_context(account_id)

    # Clean-slate reclassify semantics — same as reclassify_all: clear
    # category + important so the LLM's verdict is fully authoritative
    # and manual marks don't leak through. If the user wanted to
    # preserve a manual ⭐, they'd use「智能分类」on the unclassified
    # bucket instead (which is additive).
    target["category"] = UNCLASSIFIED
    target["important"] = False

    try:
        category, important, reason, trace = classify_email_record(
            from_email=target.get("from_email") or "",
            to_email=target.get("to_email") or "",
            cc_email=target.get("cc_email") or "",
            subject=target.get("subject") or "",
            body=target.get("body") or "",
            attachments=[
                a.get("filename", "")
                for a in (target.get("attachments") or [])
            ],
            **ctx,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"重新分类失败: {exc.__class__.__name__}: {exc}",
        )

    target["category"] = category or UNCLASSIFIED
    target["important"] = bool(important)
    target["spam_reason"] = reason
    _append_classification_trace(target, trace)
    upsert_email(target)
    return EmailRecord(**target)


# ── One-click organize for user prompts ──────────────────────────────
# Same 2-step preview/apply contract as experiences. The apply step
# re-validates every id + guards target_folder consistency so an old
# preview can't merge prompts that have since moved to a different
# folder (or been deleted).

@app.post(
    "/api/prompts/organize/preview",
    response_model=PromptOrganizePlan,
)
def preview_organize_prompts(
    user: User = Depends(current_user),
) -> PromptOrganizePlan:
    active_id = _active_account_id_for(user)
    prompts = list_prompts_for_account(active_id)
    if len(prompts) < 2:
        return PromptOrganizePlan(actions=[])
    from app.services.spam_filter import distill_organize_prompts

    plan = distill_organize_prompts(
        [
            {
                "id": p.get("id") or "",
                "name": p.get("name") or "",
                "text": p.get("text") or "",
                "target_folder": p.get("target_folder") or "",
            }
            for p in prompts
        ]
    )
    return PromptOrganizePlan(**plan)


@app.post(
    "/api/prompts/organize/apply",
    response_model=Dict[str, Any],
)
def apply_organize_prompts(
    plan: PromptOrganizePlan,
    user: User = Depends(current_user),
) -> Dict[str, Any]:
    active_id = _active_account_id_for(user)
    existing_by_id = {
        p["id"]: p for p in list_prompts_for_account(active_id) if p.get("id")
    }
    ids_to_delete: List[str] = []
    to_add: List[Dict[str, str]] = []
    for action in plan.actions:
        if action.type == "drop":
            for pid in action.ids:
                if pid not in existing_by_id:
                    raise HTTPException(
                        status_code=400,
                        detail=f"提示 {pid} 已不存在(可能被其他会话修改),请重新整理。",
                    )
                ids_to_delete.append(pid)
        elif action.type == "merge":
            if len(action.from_ids) < 2 or not action.new_text.strip():
                continue
            # Re-validate all source prompts still exist AND still share
            # the same target_folder. If either changed, refuse — the
            # plan is stale, user should re-run 一键整理.
            targets = set()
            for pid in action.from_ids:
                p = existing_by_id.get(pid)
                if p is None:
                    raise HTTPException(
                        status_code=400,
                        detail=f"提示 {pid} 已不存在(可能被其他会话修改),请重新整理。",
                    )
                targets.add((p.get("target_folder") or "").strip())
            if len(targets) > 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"提示 {action.from_ids} 的 target_folder 不一致,拒绝合并。请重新整理。",
                )
            resolved_target = (action.target_folder or "").strip() or next(iter(targets), "")
            for pid in action.from_ids:
                ids_to_delete.append(pid)
            to_add.append({
                "name": (action.new_name or "").strip()[:48],
                "text": action.new_text.strip()[:2000],
                "target_folder": resolved_target,
            })
        # keep: no-op

    for pid in ids_to_delete:
        try:
            delete_prompt(pid)
        except Exception:  # noqa: BLE001
            logger.warning("prompts organize/apply: failed to delete %s", pid)
    added_ids: List[str] = []
    for item in to_add:
        rec = add_prompt(
            {
                "account_id": active_id,
                "user_id": user.id,
                "name": item["name"],
                "text": item["text"],
                "target_folder": item["target_folder"] or None,
                "created_at": _now_iso(),
                "updated_at": None,
            }
        )
        if isinstance(rec, dict) and rec.get("id"):
            added_ids.append(rec["id"])
    return {
        "status": "ok",
        "deleted": len(ids_to_delete),
        "merged_into": len(added_ids),
        "added_ids": added_ids,
    }


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
    target = get_email(email_id)
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
    upsert_email(target)

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

    target = get_email(email_id)
    if target is None:
        raise HTTPException(status_code=404, detail="邮件不存在。")
    _assert_record_belongs_to_user(target, user)

    if new_cat not in read_folders(target.get("account_id") or ""):
        raise HTTPException(status_code=400, detail=f"未知文件夹: {new_cat}")

    old_cat = target.get("category") or ""
    if new_cat == old_cat:
        raise HTTPException(status_code=400, detail="邮件已在该分类中。")

    target["category"] = new_cat
    upsert_email(target)

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


# ── Auto-experience-on-recategorize ─────────────────────────────────
# Called AFTER the email has already been moved to its new folder (via
# right-click 移动到 X or drag-and-drop into folder X). The frontend does
# not block the move on this — it fires-and-shows the suggestion modal in
# parallel, so a slow LLM never delays the move. The single LLM call does
# two things at once: generate a one-sentence candidate experience derived
# from the move, AND check whether that candidate would duplicate an
# already-stored experience for this account (LLM-based semantic match,
# no fragile string-similarity threshold).


@app.post(
    "/api/emails/{email_id}/recategorize/suggest",
    response_model=RecategorizeSuggestion,
)
def suggest_experience_for_recategorize(
    email_id: str,
    payload: RecategorizeSuggestRequest,
    user: User = Depends(current_user),
) -> RecategorizeSuggestion:
    target = get_email(email_id)
    if target is None:
        raise HTTPException(status_code=404, detail="邮件不存在。")
    _assert_record_belongs_to_user(target, user)
    active_id = _active_account_id_for(user)

    from app.services.spam_filter import suggest_recategorize_experience

    existing = list_experiences_for_account(active_id)
    existing_lite = [
        {"id": e.get("id") or "", "text": e.get("text") or ""}
        for e in existing
        if (e.get("text") or "").strip()
    ]
    res = suggest_recategorize_experience(
        from_category=payload.from_category or "",
        to_category=payload.to_category or "",
        from_email=target.get("from_email") or "",
        to_email=target.get("to_email") or "",
        subject=target.get("subject") or "",
        body=target.get("body") or "",
        existing_experiences=existing_lite,
    )
    # Denorm the duplicate's text for the modal so the UI doesn't need a
    # second /experiences round-trip. Look up by id in the same in-memory
    # list we sent to the LLM.
    similar_text: Optional[str] = None
    dup = res.get("duplicate_of")
    if dup:
        for e in existing_lite:
            if e["id"] == dup:
                similar_text = e["text"]
                break
        if similar_text is None:
            # LLM referenced an id that vanished between the request and
            # now — drop the duplicate hint so the frontend treats it as
            # add-new.
            dup = None
    return RecategorizeSuggestion(
        candidate_text=res.get("candidate_text") or "",
        duplicate_of=dup,
        similar_text=similar_text,
        reason=res.get("reason") or "",
        error=None if res.get("candidate_text") else res.get("reason") or "LLM 未返回候选经验",
    )


@app.post(
    "/api/emails/{email_id}/recategorize/confirm",
    response_model=Dict[str, Any],
)
def confirm_experience_for_recategorize(
    email_id: str,
    payload: RecategorizeConfirmRequest,
    user: User = Depends(current_user),
) -> Dict[str, Any]:
    """Apply the user's decision from the suggestion modal:

      - action=skip  → no-op, returns {status: "skipped"}
      - action=reuse → validate reuse_id belongs to the account, no-op else
      - action=add   → persist `text` as a new experience
    """
    target = get_email(email_id)
    if target is None:
        raise HTTPException(status_code=404, detail="邮件不存在。")
    _assert_record_belongs_to_user(target, user)
    active_id = _active_account_id_for(user)
    action = (payload.action or "").strip().lower()
    if action == "skip":
        return {"status": "skipped"}
    if action == "reuse":
        rid = (payload.reuse_id or "").strip()
        if not rid:
            raise HTTPException(status_code=400, detail="reuse_id 缺失。")
        # Confirm the reuse target still exists and belongs to this account.
        for e in list_experiences_for_account(active_id):
            if e.get("id") == rid:
                return {"status": "reused", "experience_id": rid}
        raise HTTPException(status_code=404, detail="要复用的经验不存在或已被删除。")
    if action == "add":
        text = (payload.text or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="经验内容不能为空。")
        record = add_experience(
            {
                "account_id": active_id,
                "user_id": user.id,
                "text": text[:240],
                "source": "recategorize-auto",
                "source_email_id": email_id,
                "created_at": _now_iso(),
                "updated_at": None,
            }
        )
        return {
            "status": "added",
            "experience": _decorate_experience(record).model_dump(),
        }
    raise HTTPException(status_code=400, detail=f"未知的 action: {payload.action!r}")


# ── Auto-experience-on-importance-toggle ────────────────────────────
# Same shape as the recategorize auto-experience flow: frontend flips
# the ⭐ flag via /update first (immediate feedback + optimistic UI),
# THEN asks the backend to generate a candidate experience derived from
# the flip AND check for duplicates against the account's existing
# experience list — one LLM call does both. User picks add/reuse/skip
# in the same suggestion modal.


@app.post(
    "/api/emails/{email_id}/importance/suggest",
    response_model=RecategorizeSuggestion,
)
def suggest_experience_for_importance(
    email_id: str,
    payload: ImportanceSuggestRequest,
    user: User = Depends(current_user),
) -> RecategorizeSuggestion:
    target = get_email(email_id)
    if target is None:
        raise HTTPException(status_code=404, detail="邮件不存在。")
    _assert_record_belongs_to_user(target, user)
    active_id = _active_account_id_for(user)

    direction = (payload.direction or "").strip().lower()
    if direction not in {"mark", "unmark"}:
        raise HTTPException(
            status_code=400,
            detail=f"direction 必须是 'mark' 或 'unmark',收到: {payload.direction!r}",
        )

    from app.services.spam_filter import suggest_importance_experience

    existing = list_experiences_for_account(active_id)
    existing_lite = [
        {"id": e.get("id") or "", "text": e.get("text") or ""}
        for e in existing
        if (e.get("text") or "").strip()
    ]
    res = suggest_importance_experience(
        direction=direction,
        from_email=target.get("from_email") or "",
        to_email=target.get("to_email") or "",
        subject=target.get("subject") or "",
        body=target.get("body") or "",
        existing_experiences=existing_lite,
    )
    similar_text: Optional[str] = None
    dup = res.get("duplicate_of")
    if dup:
        for e in existing_lite:
            if e["id"] == dup:
                similar_text = e["text"]
                break
        if similar_text is None:
            dup = None
    return RecategorizeSuggestion(
        candidate_text=res.get("candidate_text") or "",
        duplicate_of=dup,
        similar_text=similar_text,
        reason=res.get("reason") or "",
        error=None if res.get("candidate_text") else res.get("reason") or "LLM 未返回候选经验",
    )


@app.post(
    "/api/emails/{email_id}/importance/confirm",
    response_model=Dict[str, Any],
)
def confirm_experience_for_importance(
    email_id: str,
    payload: RecategorizeConfirmRequest,
    user: User = Depends(current_user),
) -> Dict[str, Any]:
    """Same three-way decision (add/reuse/skip) as the recategorize
    confirm endpoint; only the `source` label on a newly-added
    experience differs so 分类历史 / experience listings can tell where
    the record came from."""
    target = get_email(email_id)
    if target is None:
        raise HTTPException(status_code=404, detail="邮件不存在。")
    _assert_record_belongs_to_user(target, user)
    active_id = _active_account_id_for(user)
    action = (payload.action or "").strip().lower()
    if action == "skip":
        return {"status": "skipped"}
    if action == "reuse":
        rid = (payload.reuse_id or "").strip()
        if not rid:
            raise HTTPException(status_code=400, detail="reuse_id 缺失。")
        for e in list_experiences_for_account(active_id):
            if e.get("id") == rid:
                return {"status": "reused", "experience_id": rid}
        raise HTTPException(status_code=404, detail="要复用的经验不存在或已被删除。")
    if action == "add":
        text = (payload.text or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="经验内容不能为空。")
        record = add_experience(
            {
                "account_id": active_id,
                "user_id": user.id,
                "text": text[:240],
                "source": "importance-auto",
                "source_email_id": email_id,
                "created_at": _now_iso(),
                "updated_at": None,
            }
        )
        return {
            "status": "added",
            "experience": _decorate_experience(record).model_dump(),
        }
    raise HTTPException(status_code=400, detail=f"未知的 action: {payload.action!r}")


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
    target = get_email(email_id)
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
    target = get_email(email_id)
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
    # target_folder is now optional — an "importance-only" rule leaves it
    # empty and only touches the ⭐ flag. See FixedRule docstring.
    target_folder = _normalize_target_folder(
        payload.target_folder, active_id, required=False
    ) or ""
    if payload.mark_important and payload.unmark_important:
        raise HTTPException(
            status_code=400,
            detail="mark_important 和 unmark_important 只能二选一。",
        )
    # A rule with no folder AND no importance directive would be a no-op:
    # nothing to do on match. Reject early so the user can't save one.
    if not target_folder and not payload.mark_important and not payload.unmark_important:
        raise HTTPException(
            status_code=400,
            detail=(
                "规则至少要做一件事:选择目标文件夹,或勾选"
                "「命中时标为重要 / 命中时取消重要」之一。"
            ),
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
        mark_important=bool(payload.mark_important),
        unmark_important=bool(payload.unmark_important),
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
        payload.target_folder, active_id, required=False
    ) or ""
    if payload.mark_important and payload.unmark_important:
        raise HTTPException(
            status_code=400,
            detail="mark_important 和 unmark_important 只能二选一。",
        )
    if not target_folder and not payload.mark_important and not payload.unmark_important:
        raise HTTPException(
            status_code=400,
            detail=(
                "规则至少要做一件事:选择目标文件夹,或勾选"
                "「命中时标为重要 / 命中时取消重要」之一。"
            ),
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
            "mark_important": bool(payload.mark_important),
            "unmark_important": bool(payload.unmark_important),
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
        # `""` explicitly clears the folder (rule becomes importance-only).
        fields["target_folder"] = _normalize_target_folder(
            payload.target_folder,
            existing.get("account_id") or "",
            required=False,
        ) or ""
    if payload.mark_important is not None:
        fields["mark_important"] = bool(payload.mark_important)
    if payload.unmark_important is not None:
        fields["unmark_important"] = bool(payload.unmark_important)

    # Reject the two impossible states: both importance flags set, or a
    # rule with neither folder nor importance directive. We check against
    # the merged view (existing + incoming fields).
    def _eff(key: str, default=None):
        return fields.get(key, existing.get(key, default))
    if _eff("mark_important", False) and _eff("unmark_important", False):
        raise HTTPException(
            status_code=400,
            detail="mark_important 和 unmark_important 只能二选一。",
        )
    if (
        not _eff("target_folder", "")
        and not _eff("mark_important", False)
        and not _eff("unmark_important", False)
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "规则至少要做一件事:选择目标文件夹,或勾选"
                "「命中时标为重要 / 命中时取消重要」之一。"
            ),
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

# Max classification-trace entries kept per email. Every re-classify
# (manual retry, 重新分类, receive-then-recategorize) appends one entry;
# without a cap a heavily re-run email would grow its data_json blob
# forever. 20 is plenty to see recent history and pattern shifts.
_TRACE_MAX_PER_EMAIL = 20


def _append_classification_trace(record: Dict, trace: Optional[Dict]) -> None:
    """Push one trace entry onto the email record's history (in-place),
    trimming to the most recent _TRACE_MAX_PER_EMAIL entries. Called by
    every code path that runs classify_email_record so the admin 分类历史
    page has a complete audit trail."""
    if not isinstance(trace, dict):
        return
    hist = record.get("classification_trace")
    if not isinstance(hist, list):
        hist = []
    hist.append(trace)
    if len(hist) > _TRACE_MAX_PER_EMAIL:
        hist = hist[-_TRACE_MAX_PER_EMAIL:]
    record["classification_trace"] = hist


def _classification_context(account_id: str) -> Dict:
    """Bundle every piece of state the classifier needs for one account.

    Fixed rules whose target is the "全部" sentinel (`*`) are forwarded to
    the LLM as extra general guidance, since they have no folder to route
    to. Their AST still runs at the fixed-rule stage, but it can never
    return early — see classify_email_record.

    Each entry in `user_prompts_with_targets` carries `_kind` + `_id`
    sidecar fields so the classification-history trace can attribute
    which prompts / experiences / target-less-rules were in scope for a
    given LLM call. _compose_system_prompt only reads .text / .target_folder,
    so the extra keys are ignored by the LLM prompt-building path."""
    fixed_rules = list_fixed_rules_for_account(account_id)
    user_prompts: List[Dict] = [
        {
            "text": p.get("text") or "",
            "target_folder": p.get("target_folder"),
            "_kind": "prompt",
            "_id": p.get("id") or "",
        }
        for p in list_prompts_for_account(account_id)
        if (p.get("text") or "").strip()
    ]
    for rule in fixed_rules:
        if (rule.get("target_folder") or "").strip() == ALL_FOLDERS_SENTINEL:
            nl = (rule.get("nl_text") or "").strip()
            if nl:
                user_prompts.append({
                    "text": nl,
                    "target_folder": None,
                    "_kind": "fixed_rule_general",
                    "_id": rule.get("id") or "",
                })
    # Distilled experiences are surfaced to the LLM as additional general
    # guidance — same channel as target-less prompts. The user can curate
    # them via the right-sidebar 经验 section.
    for exp in list_experiences_for_account(account_id):
        text = (exp.get("text") or "").strip()
        if text:
            user_prompts.append({
                "text": "经验: " + text,
                "target_folder": None,
                "_kind": "experience",
                "_id": exp.get("id") or "",
            })
    # `owner_email` is threaded into the LLM's user-message header so
    # prompts / experiences that talk about "我" (the current user) —
    # "发给我 vs 抄送给我" and the like — actually have a reference
    # point to compare To/Cc against. Without it the classifier can't
    # tell whose address is whose.
    acc = get_account(account_id) or {}
    settings = acc.get("settings") or {}
    owner_email = str(settings.get("sender_email") or "").strip()
    return {
        "system_prompt": read_system_spam_prompt(),
        "user_prompts_with_targets": user_prompts,
        "fixed_rules": fixed_rules,
        "available_folders": read_folders(account_id),
        "field_config": _field_config_for_account(account_id).model_dump(),
        "owner_email": owner_email,
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
        category, important, reason, trace = classify_email_record(
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
        _append_classification_trace(rec, trace)

    write_emails(emails)
    return ClassifyUnsortedResult(
        classified=classified, remaining=remaining, total=total
    )


def _task_snapshot_to_info(task: task_registry.Task) -> TaskInfo:
    s = task.snapshot()
    return TaskInfo(**s)


def _humanize_task_error(kind: str, exc: BaseException) -> str:
    label = {
        "receive": "收取邮件",
        "classify_unsorted": "执行分类",
        "reclassify_all": "重新分类",
    }.get(kind, kind)
    if kind == "receive":
        return _humanize_email_error(label, exc)
    return f"{label}失败: {exc.__class__.__name__}: {exc}"


_CLASSIFY_FLUSH_EVERY = 10


def _classify_worker_for_scope(
    *,
    active_id: str,
    ctx: Dict[str, Any],
    scope_filter,
    reset_before_classify: bool,
    task_id: str,
    control: TaskControl,
    label: str,
) -> Dict[str, Any]:
    """Common inner loop for both `classify_unsorted` and `reclassify_all`.
    Extracted so the same pause/cancel logic serves both — the only
    real difference is which records get picked up (scope_filter) and
    whether we reset category/important before running the classifier.

    Persists results incrementally in chunks of `_CLASSIFY_FLUSH_EVERY`
    via `upsert_emails` so a force-quit / crash / TaskCancelled mid-run
    keeps all completed chunks on disk (worst-case data loss = at most
    the last un-flushed chunk of ≤10 LLM calls). Pause is fine either
    way — it blocks the worker in-place, in-memory state stays intact
    across resume. Terminal cancel is caught here so we flush the
    partial chunk before re-raising."""
    import traceback as _tb

    task_registry.set_progress(
        task_id, phase="loading", index=0, total=0,
        skipped_count=0, changed=0,
    )
    # NOTE: we no longer keep `others` around — we mutate `scope` rows
    # in place and upsert them; other-account rows stay untouched on disk.
    all_emails = read_emails()
    scope = [e for e in all_emails if scope_filter(e)]
    total = len(scope)
    task_registry.set_progress(
        task_id, phase="processing", index=0, total=total,
        skipped_count=0, changed=0, percent=0.0,
    )

    classified = 0
    changed = 0
    remaining = 0
    skipped = 0
    pending_flush: List[Dict[str, Any]] = []
    last_flushed = 0

    def flush_pending() -> None:
        nonlocal last_flushed
        if not pending_flush:
            return
        # One transaction per chunk — cheap, and it means the worker's
        # progress is durable at each 10-email boundary.
        upsert_emails(pending_flush)
        last_flushed += len(pending_flush)
        pending_flush.clear()

    try:
        for idx, rec in enumerate(scope):
            control.check()
            old_cat = rec.get("category") or ""
            if reset_before_classify:
                rec["category"] = UNCLASSIFIED
                rec["important"] = False
            try:
                category, important, reason, trace = classify_email_record(
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
                if reset_before_classify:
                    rec["category"] = category or UNCLASSIFIED
                    rec["important"] = bool(important)
                    rec["spam_reason"] = reason
                    if rec["category"] != old_cat:
                        changed += 1
                else:
                    if important and not rec.get("important"):
                        rec["important"] = True
                    if category and category != UNCLASSIFIED:
                        rec["category"] = category
                        rec["spam_reason"] = reason
                        classified += 1
                    else:
                        rec["spam_reason"] = reason
                        remaining += 1
                _append_classification_trace(rec, trace)
                # Queue for the next batch flush. Skipped-due-to-exception
                # rows do NOT go into pending_flush — nothing changed, no
                # need to overwrite the DB row with an identical copy.
                pending_flush.append(rec)
            except TaskCancelled:
                raise
            except Exception as per_email_exc:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "%s: skipping email %s (idx %d/%d): %s\n%s",
                    label, rec.get("id") or "?", idx + 1, total,
                    per_email_exc, _tb.format_exc(),
                )
                skipped += 1
                if not reset_before_classify:
                    remaining += 1

            if len(pending_flush) >= _CLASSIFY_FLUSH_EVERY:
                flush_pending()

            percent = ((idx + 1) / total * 100.0) if total else 0.0
            last_event = {
                "type": "classified",
                "index": idx + 1,
                "total": total,
                "subject": rec.get("subject") or "",
                "category": rec.get("category") or UNCLASSIFIED,
            }
            task_registry.set_progress(
                task_id, index=idx + 1, total=total, percent=percent,
                skipped_count=skipped, changed=changed,
                saved_count=last_flushed + len(pending_flush),
                last_event=last_event,
            )
    except TaskCancelled:
        # User hit "取消". Flush whatever LLM work already succeeded
        # so those calls aren't wasted, then propagate — the runner
        # marks the task as cancelled.
        try:
            flush_pending()
        except Exception:
            # Persist-on-cancel is best-effort; a flush failure here
            # shouldn't stop the cancel from taking effect.
            pass
        raise

    task_registry.set_progress(task_id, phase="saving")
    flush_pending()
    return {
        "total": total,
        "classified": classified,
        "changed": changed,
        "remaining": remaining,
        "skipped": skipped,
    }


# ==============================================================
# Task-based long-running work (receive / classify / reclassify)
# ==============================================================
#
# These endpoints run the work on a background thread whose lifetime is
# independent of the HTTP client — so the task keeps going even when the
# user navigates to /admin or /settings. The frontend polls
# /api/tasks/active on page load to reattach the progress modal.

@app.post("/api/tasks/receive/start", response_model=TaskInfo)
def start_receive_task(
    req: Optional[ReceiveTaskStartRequest] = None,
    user: User = Depends(current_user),
) -> TaskInfo:
    """Start a background 收取邮件 task. If one is already running for this
    user, returns it instead of spawning a duplicate."""
    # Only one long-running task per user at a time: receive + classify
    # both mutate the emails table (receive via upsert_emails per batch,
    # classify via write_emails at end) and interleaving them can silently
    # overwrite in-flight rows. If another task is running, hand back that
    # one so the UI reopens its modal instead of spawning a conflict.
    existing = task_registry.active_for(user.id)
    if existing:
        return _task_snapshot_to_info(existing)

    acc = _active_account_for(user)
    settings = acc["settings"]
    active_id = acc["id"]
    days = (req.days if req and req.days is not None else None)
    if days is None:
        sync_cfg = acc.get("sync") or {}
        days = int(sync_cfg.get("fetch_days") or SyncSettings().fetch_days)
    ctx = _classification_context(active_id)
    sync_state = get_account_sync_state(active_id)
    known_uids = list_known_imap_uids(active_id)

    task = task_registry.create_task(
        kind="receive", owner=user.id, label="收取邮件",
    )
    task_registry.set_progress(
        task.id, phase="connecting", index=0, total=0,
        skipped_count=0, cum_fetched=0, cum_stored=0,
    )

    def worker(control: TaskControl) -> Dict[str, Any]:
        state = {"cum_fetched": 0, "cum_stored": 0, "skipped": 0}

        def on_progress(ev: Dict[str, Any]) -> None:
            typ = ev.get("type")
            if typ == "connected":
                task_registry.set_progress(task.id, phase="searching")
            elif typ == "planned":
                total = int(ev.get("total") or 0)
                task_registry.set_progress(
                    task.id, phase="processing",
                    index=0, total=total, percent=0.0,
                    mode=ev.get("mode") or "",
                    days=ev.get("days") or 0,
                    already_known=ev.get("already_known") or 0,
                )
            elif typ == "classified":
                idx = int(ev.get("index") or 0)
                total = int(ev.get("total") or 0)
                percent = (idx / total * 100.0) if total else 0.0
                task_registry.set_progress(
                    task.id, index=idx, total=total, percent=percent,
                    last_event={
                        "type": "classified",
                        "index": idx, "total": total,
                        "subject": ev.get("subject") or "",
                        "from": ev.get("from") or "",
                        "category": ev.get("category") or "",
                        "received_at": ev.get("received_at") or "",
                    },
                )
            elif typ == "skipped":
                state["skipped"] += 1
                task_registry.set_progress(
                    task.id,
                    index=int(ev.get("index") or 0),
                    total=int(ev.get("total") or 0),
                    skipped_count=state["skipped"],
                    last_event={
                        "type": "skipped",
                        "index": int(ev.get("index") or 0),
                        "total": int(ev.get("total") or 0),
                        "uid": ev.get("uid") or "",
                        "reason": ev.get("reason") or "",
                    },
                )

        def persist_batch(batch_records: List[Dict], partial_sync_meta: Dict[str, str]) -> None:
            if not batch_records:
                return
            for item in batch_records:
                item["account_id"] = active_id
            own_old = list_emails_for_account(active_id)
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

            upsert_emails(finalized)
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
            task_registry.set_progress(
                task.id,
                cum_fetched=state["cum_fetched"],
                cum_stored=state["cum_stored"],
            )

        records, sync_meta = receive_emails(
            settings=settings, days=days,
            on_progress=on_progress, on_batch_ready=persist_batch,
            batch_size=10, sync_state=sync_state, known_uids=known_uids,
            on_yield=control.check, **ctx,
        )

        # Final sync_meta application (matches original stream endpoint's
        # after-loop bump).
        sm = sync_meta or {}
        if sm.get("mailbox") and sm.get("uidvalidity") and sm.get("last_uid"):
            update_account_sync_state_entry(
                active_id, sm["mailbox"], sm["uidvalidity"],
                sm["last_uid"], sm.get("fetch_days_at", ""),
            )
        task_registry.set_progress(task.id, phase="done")
        return {
            "fetched": state["cum_fetched"],
            "stored": state["cum_stored"],
            "skipped": state["skipped"],
        }

    def wrapped_worker(control: TaskControl) -> Dict[str, Any]:
        try:
            return worker(control)
        except TaskCancelled:
            raise
        except Exception as exc:
            # Match the humanised message the old stream endpoint produced.
            task_registry.set_error(task.id, _humanize_task_error("receive", exc))
            raise

    task_registry.run_task(task, wrapped_worker)
    return _task_snapshot_to_info(task)


@app.post("/api/tasks/classify-unsorted/start", response_model=TaskInfo)
def start_classify_unsorted_task(user: User = Depends(current_user)) -> TaskInfo:
    """Start a background 执行分类 task over every 未分类 email for the
    active account. Returns the existing task if one is already running."""
    # See start_receive_task for why we forbid any concurrent task per user.
    existing = task_registry.active_for(user.id)
    if existing:
        return _task_snapshot_to_info(existing)

    active_id = _active_account_id_for(user)
    ctx = _classification_context(active_id)
    task = task_registry.create_task(
        kind="classify_unsorted", owner=user.id, label="执行分类",
    )

    def scope_filter(rec: Dict) -> bool:
        return (
            rec.get("account_id") == active_id
            and (rec.get("category") or "") == UNCLASSIFIED
        )

    def worker(control: TaskControl) -> Dict[str, Any]:
        try:
            return _classify_worker_for_scope(
                active_id=active_id, ctx=ctx, scope_filter=scope_filter,
                reset_before_classify=False, task_id=task.id, control=control,
                label="classify-unsorted",
            )
        except TaskCancelled:
            raise
        except Exception as exc:
            task_registry.set_error(task.id, _humanize_task_error("classify_unsorted", exc))
            raise

    task_registry.run_task(task, worker)
    return _task_snapshot_to_info(task)


@app.post("/api/tasks/reclassify-all/start", response_model=TaskInfo)
def start_reclassify_all_task(user: User = Depends(current_user)) -> TaskInfo:
    """Start a background 重新分类 task over every email for the active
    account. Resets category+important before running the classifier —
    the manual 重要 flag is intentionally cleared (see the old stream
    endpoint's docstring)."""
    # See start_receive_task for why we forbid any concurrent task per user.
    existing = task_registry.active_for(user.id)
    if existing:
        return _task_snapshot_to_info(existing)

    active_id = _active_account_id_for(user)
    ctx = _classification_context(active_id)
    task = task_registry.create_task(
        kind="reclassify_all", owner=user.id, label="重新分类",
    )

    def scope_filter(rec: Dict) -> bool:
        return rec.get("account_id") == active_id

    def worker(control: TaskControl) -> Dict[str, Any]:
        try:
            return _classify_worker_for_scope(
                active_id=active_id, ctx=ctx, scope_filter=scope_filter,
                reset_before_classify=True, task_id=task.id, control=control,
                label="reclassify-all",
            )
        except TaskCancelled:
            raise
        except Exception as exc:
            task_registry.set_error(task.id, _humanize_task_error("reclassify_all", exc))
            raise

    task_registry.run_task(task, worker)
    return _task_snapshot_to_info(task)


class ReclassifyScopeStartRequest(BaseModel):
    """Filter for the "reclassify emails in THIS scope" task.

    `scope` picks the semantics:
      * "folder"     — reclassify emails whose current category matches
                       `folder`. Use "__all__" (or leave empty) for
                       every inbox mail of the active account.
      * "important"  — reclassify every currently-flagged-important email.
    """
    scope: str = "folder"
    folder: str = ""


@app.post("/api/tasks/reclassify-scope/start", response_model=TaskInfo)
def start_reclassify_scope_task(
    req: ReclassifyScopeStartRequest,
    user: User = Depends(current_user),
) -> TaskInfo:
    """Reclassify only the emails matching a scope filter — used by the
    folder-list right-click 「重新智能分类」 action. Same clean-slate
    semantics as reclassify_all (reset category + important, then
    re-run the classifier)."""
    existing = task_registry.active_for(user.id)
    if existing:
        return _task_snapshot_to_info(existing)

    active_id = _active_account_id_for(user)
    ctx = _classification_context(active_id)

    scope_kind = (req.scope or "folder").strip()
    folder = (req.folder or "").strip()
    if scope_kind == "important":
        # Match the sidebar's ⭐ 重要 label exactly: important + NOT
        # handled. Without the !handled clause we'd sweep in every
        # archived "曾经重要" record — dozens or hundreds of extra
        # LLM calls the user didn't ask for.
        label = "重新分类:重要邮件"

        def scope_filter(rec: Dict) -> bool:
            return (
                rec.get("account_id") == active_id
                and bool(rec.get("important"))
                and not bool(rec.get("handled"))
                and not bool(rec.get("deleted"))
            )
    elif scope_kind == "formerly_important":
        label = "重新分类:曾经重要"

        def scope_filter(rec: Dict) -> bool:
            return (
                rec.get("account_id") == active_id
                and bool(rec.get("important"))
                and bool(rec.get("handled"))
                and not bool(rec.get("deleted"))
            )
    elif scope_kind == "folder":
        if not folder or folder == "__all__":
            label = "重新分类:全部收件"

            def scope_filter(rec: Dict) -> bool:
                return (
                    rec.get("account_id") == active_id
                    and not bool(rec.get("deleted"))
                )
        else:
            label = f"重新分类:{folder}"

            def scope_filter(rec: Dict) -> bool:
                return (
                    rec.get("account_id") == active_id
                    and (rec.get("category") or "") == folder
                    and not bool(rec.get("deleted"))
                )
    else:
        raise HTTPException(
            status_code=400,
            detail=f"未知的 scope: {req.scope!r}",
        )

    task = task_registry.create_task(
        kind="reclassify_all", owner=user.id, label=label,
    )

    def worker(control: TaskControl) -> Dict[str, Any]:
        try:
            return _classify_worker_for_scope(
                active_id=active_id, ctx=ctx, scope_filter=scope_filter,
                reset_before_classify=True, task_id=task.id, control=control,
                label=f"reclassify-scope:{scope_kind}:{folder or '*'}",
            )
        except TaskCancelled:
            raise
        except Exception as exc:
            task_registry.set_error(task.id, _humanize_task_error("reclassify_all", exc))
            raise

    task_registry.run_task(task, worker)
    return _task_snapshot_to_info(task)


class AiSearchStartRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    start_date: str = Field(default="")   # ISO date "YYYY-MM-DD" (inclusive)
    end_date: str = Field(default="")     # ISO date "YYYY-MM-DD" (inclusive)
    # Optional scope narrowing. "folder" + "" or "__all__" means "every
    # non-deleted inbox mail". "important" / "formerly_important" are
    # rollup labels that filter on the ⭐ / handled flags directly.
    scope_kind: str = Field(default="folder")
    scope_folder: str = Field(default="")


@app.post("/api/tasks/ai-search/start", response_model=TaskInfo)
def start_ai_search_task(
    req: AiSearchStartRequest,
    user: User = Depends(current_user),
) -> TaskInfo:
    """Semantic search: iterate over the active account's emails in the
    given date range, LLM-judges each against the natural-language
    query, and streams a list of matching ids into task.result.
    Progress + pause + cancel driven by the standard task modal."""
    existing = task_registry.active_for(user.id)
    if existing:
        return _task_snapshot_to_info(existing)

    active_id = _active_account_id_for(user)
    acc = get_account(active_id) or {}
    settings = acc.get("settings") or {}
    owner_email = str(settings.get("sender_email") or "").strip()

    # Date-range parsing. Empty → open-ended on that side. The received_at
    # comparison uses ISO string prefix, which sorts lexicographically.
    start_raw = (req.start_date or "").strip()
    end_raw = (req.end_date or "").strip()
    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    if start_raw and not date_re.match(start_raw):
        raise HTTPException(status_code=400, detail=f"start_date 格式应为 YYYY-MM-DD: {start_raw}")
    if end_raw and not date_re.match(end_raw):
        raise HTTPException(status_code=400, detail=f"end_date 格式应为 YYYY-MM-DD: {end_raw}")
    if start_raw and end_raw and start_raw > end_raw:
        raise HTTPException(status_code=400, detail="start_date 不能晚于 end_date。")

    # Scope validation. `scope_kind` narrows what the LLM even looks
    # at — reduces cost and matches the user's mental model of
    # "search THIS folder / label".
    scope_kind = (req.scope_kind or "folder").strip().lower()
    scope_folder = (req.scope_folder or "").strip()
    known_folders = read_folders(active_id)
    scope_label_bits: List[str] = []
    if scope_kind == "folder":
        if scope_folder and scope_folder != "__all__":
            if scope_folder not in known_folders:
                raise HTTPException(status_code=400, detail=f"未知文件夹: {scope_folder}")
            scope_label_bits.append(scope_folder)
    elif scope_kind == "important":
        scope_label_bits.append("重要")
    elif scope_kind == "formerly_important":
        scope_label_bits.append("曾经重要")
    else:
        raise HTTPException(
            status_code=400,
            detail=f"未知的 scope_kind: {req.scope_kind!r}",
        )

    label = "智能检索"
    if scope_label_bits:
        label = f"智能检索:{'/'.join(scope_label_bits)}"

    task = task_registry.create_task(
        kind="ai_search", owner=user.id, label=label,
    )
    task_registry.set_progress(
        task.id, phase="loading", index=0, total=0, matched=0,
        query=req.query.strip()[:80],
    )

    from app.services.spam_filter import judge_email_matches_query

    def _rec_in_scope(rec: Dict) -> bool:
        """Apply scope_kind filter — orthogonal to date range."""
        if scope_kind == "important":
            return bool(rec.get("important")) and not bool(rec.get("handled"))
        if scope_kind == "formerly_important":
            return bool(rec.get("important")) and bool(rec.get("handled"))
        # scope_kind == "folder"
        if not scope_folder or scope_folder == "__all__":
            return True
        cat = (rec.get("category") or "")
        # Match the folder itself and any sub-folder (using "/" separator
        # like everywhere else in the UI).
        return cat == scope_folder or cat.startswith(scope_folder + "/")

    def worker(control: TaskControl) -> Dict[str, Any]:
        emails = read_emails()
        scope: List[Dict] = []
        for rec in emails:
            if rec.get("account_id") != active_id:
                continue
            if rec.get("deleted"):
                continue
            if not _rec_in_scope(rec):
                continue
            ra = str(rec.get("received_at") or "")
            # received_at is ISO-8601 (may include timezone). Prefix
            # match against 10-char date strings works because ISO-8601
            # sorts lexicographically, and comparing prefix > full is
            # equivalent to comparing full > full+"T…" for the endpoint.
            day = ra[:10] if len(ra) >= 10 else ""
            if start_raw and (not day or day < start_raw):
                continue
            if end_raw and (not day or day > end_raw):
                continue
            scope.append(rec)
        total = len(scope)
        matched_ids: List[str] = []
        task_registry.set_progress(
            task.id, phase="processing", index=0, total=total, matched=0,
        )

        for idx, rec in enumerate(scope):
            control.check()
            try:
                res = judge_email_matches_query(
                    query=req.query,
                    from_email=rec.get("from_email") or "",
                    to_email=rec.get("to_email") or "",
                    cc_email=rec.get("cc_email") or "",
                    subject=rec.get("subject") or "",
                    body=rec.get("body") or "",
                    owner_email=owner_email,
                )
            except TaskCancelled:
                raise
            except Exception as exc:
                res = {"match": False, "reason": f"{exc.__class__.__name__}: {exc}"}
            if res.get("match"):
                matched_ids.append(rec.get("id") or "")
                task_registry.set_progress(
                    task.id,
                    last_match={
                        "id": rec.get("id") or "",
                        "subject": rec.get("subject") or "",
                        "from": rec.get("from_email") or "",
                        "received_at": rec.get("received_at") or "",
                        "reason": res.get("reason") or "",
                    },
                )
            percent = ((idx + 1) / total * 100.0) if total else 0.0
            task_registry.set_progress(
                task.id, index=idx + 1, total=total, percent=percent,
                matched=len(matched_ids),
                last_event={
                    "type": "checked",
                    "index": idx + 1,
                    "total": total,
                    "subject": rec.get("subject") or "",
                    "matched": bool(res.get("match")),
                },
            )
        return {
            "total": total,
            "matched": len(matched_ids),
            "matched_ids": matched_ids,
        }

    def wrapped(control: TaskControl) -> Dict[str, Any]:
        try:
            return worker(control)
        except TaskCancelled:
            raise
        except Exception as exc:
            task_registry.set_error(task.id, f"智能检索失败: {exc.__class__.__name__}: {exc}")
            raise

    task_registry.run_task(task, wrapped)
    return _task_snapshot_to_info(task)


@app.get("/api/tasks/active", response_model=ActiveTaskResponse)
def get_active_task(user: User = Depends(current_user)) -> ActiveTaskResponse:
    """The user's most recent non-terminal task, or the most recent
    just-finished task (so the UI can render its summary before the user
    dismisses it)."""
    t = task_registry.active_for(user.id)
    if not t:
        # Also expose a recently-finished task so the modal can show the
        # summary line after completion.
        latest = task_registry.latest_for(user.id)
        if latest and latest.finished_at:
            # Only if the finish was recent (last 10 minutes) — older
            # finished tasks are considered acknowledged implicitly.
            import time as _t
            if _t.time() - latest.finished_at < 600:
                return ActiveTaskResponse(task=_task_snapshot_to_info(latest))
        return ActiveTaskResponse(task=None)
    return ActiveTaskResponse(task=_task_snapshot_to_info(t))


@app.get("/api/tasks/{task_id}", response_model=TaskInfo)
def get_task_status(task_id: str, user: User = Depends(current_user)) -> TaskInfo:
    t = task_registry.get_task(task_id)
    if not t or t.owner != user.id:
        raise HTTPException(status_code=404, detail="任务不存在。")
    return _task_snapshot_to_info(t)


@app.post("/api/tasks/{task_id}/pause", response_model=TaskInfo)
def pause_task(task_id: str, user: User = Depends(current_user)) -> TaskInfo:
    t = task_registry.get_task(task_id)
    if not t or t.owner != user.id:
        raise HTTPException(status_code=404, detail="任务不存在。")
    task_registry.pause(task_id)
    t = task_registry.get_task(task_id)
    return _task_snapshot_to_info(t)


@app.post("/api/tasks/{task_id}/resume", response_model=TaskInfo)
def resume_task(task_id: str, user: User = Depends(current_user)) -> TaskInfo:
    t = task_registry.get_task(task_id)
    if not t or t.owner != user.id:
        raise HTTPException(status_code=404, detail="任务不存在。")
    task_registry.resume(task_id)
    t = task_registry.get_task(task_id)
    return _task_snapshot_to_info(t)


@app.post("/api/tasks/{task_id}/cancel", response_model=TaskInfo)
def cancel_task(task_id: str, user: User = Depends(current_user)) -> TaskInfo:
    t = task_registry.get_task(task_id)
    if not t or t.owner != user.id:
        raise HTTPException(status_code=404, detail="任务不存在。")
    task_registry.cancel(task_id)
    t = task_registry.get_task(task_id)
    return _task_snapshot_to_info(t)


@app.post("/api/tasks/{task_id}/ack")
def ack_task(task_id: str, user: User = Depends(current_user)) -> Dict[str, bool]:
    """Drop a terminal task from the registry so /api/tasks/active stops
    returning it. The frontend calls this after the user dismisses the
    completion state of the progress modal."""
    t = task_registry.get_task(task_id)
    if not t or t.owner != user.id:
        raise HTTPException(status_code=404, detail="任务不存在。")
    ok = task_registry.acknowledge(task_id)
    return {"ok": ok}


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
                    category, important, reason, trace = classify_email_record(
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
                    _append_classification_trace(rec, trace)

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
                    category, important, reason, trace = classify_email_record(
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
                    _append_classification_trace(rec, trace)
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

    # Two send modes:
    #   • "grouped" (default): a single wire message with the full
    #     To/Cc/Bcc list — normal email.
    #   • "independent": one wire message per unique recipient across
    #     To/Cc/Bcc, each showing only that recipient in the To field
    #     (Cc/Bcc empty). Lets the user do group notifications
    #     without leaking the recipient list.
    raw_message = ""
    independent_raws: List[str] = []
    if payload.send_independently:
        # Deduplicate across To + Cc + Bcc; preserve first-seen order.
        seen_addrs: set = set()
        indep_targets: List[str] = []
        for src in (_to_addrs, _cc_addrs, _bcc_addrs):
            for addr in src:
                key = addr.lower()
                if key not in seen_addrs:
                    seen_addrs.add(key)
                    indep_targets.append(addr)
        if not indep_targets:
            raise HTTPException(status_code=400, detail="收件人不能为空。")
        for addr in indep_targets:
            try:
                one_raw = send_email(
                    settings,
                    addr,
                    payload.subject,
                    payload.body,
                    attachments=send_attachments,
                    cc="",
                    bcc="",
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=500,
                    detail=_humanize_email_error(f"发送邮件到 {addr}", exc),
                ) from exc
            independent_raws.append(one_raw)
        # For the "mirror to server Sent folder" step below, pick the
        # first raw as a representative — IMAP APPEND N times would be
        # a lot of round-trips for the same body content.
        raw_message = independent_raws[0] if independent_raws else ""
    else:
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
        parent = get_email(payload.reply_to_inbox_id)
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
        "send_mode": "independent" if payload.send_independently else "grouped",
    }

    # Relocate attachments to the sent folder (and merge in inbox carry-over).
    if payload.draft_id:
        move_attachments_folder(payload.draft_id, sent_id)
    if payload.attach_from_inbox_id:
        copy_attachments_folder(payload.attach_from_inbox_id, sent_id)
    # Refresh metadata from disk so the stored record matches what's on disk.
    sent_record["attachments"] = list_attachments_meta(sent_id)

    upsert_sent(sent_record)

    if payload.draft_id:
        storage_delete_draft(payload.draft_id)

    if payload.reply_to_inbox_id:
        parent_email = get_email(payload.reply_to_inbox_id)
        if parent_email is not None:
            parent_email["replied"] = True
            upsert_email(parent_email)

    return SendResult(status="ok", detail="邮件发送成功")


@app.get("/api/drafts", response_model=List[DraftRecord])
def list_drafts(user: User = Depends(current_user)) -> List[DraftRecord]:
    active_id = get_user_active_account_id(user.id) or ""
    items = list_drafts_for_account(active_id)
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

    now = datetime.now(timezone.utc).isoformat()
    record = None

    if payload.id:
        existing = get_draft(payload.id)
        if existing is not None:
            existing["to"] = to
            existing["cc"] = cc
            existing["bcc"] = bcc
            existing["subject"] = subject
            existing["body"] = body
            existing["updated_at"] = now
            record = existing

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

    # Carry attachments from a source inbox email if requested.
    if payload.attach_from_inbox_id:
        copy_attachments_folder(payload.attach_from_inbox_id, record["id"])

    record["attachments"] = list_attachments_meta(record["id"])

    upsert_draft(record)
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
    target = get_draft(draft_id)
    if target is None:
        raise HTTPException(status_code=404, detail="草稿不存在或已删除。")
    _assert_record_belongs_to_user(target, user)
    storage_delete_draft(draft_id)
    delete_attachments_folder(draft_id)
    return {"status": "ok"}


@app.post("/api/drafts/{draft_id}/attachments", response_model=Attachment)
async def upload_draft_attachment(
    draft_id: str,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
) -> Attachment:
    draft = get_draft(draft_id)
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
    upsert_draft(draft)
    return Attachment(**saved)


@app.delete("/api/drafts/{draft_id}/attachments/{filename}")
def delete_draft_attachment(
    draft_id: str, filename: str, user: User = Depends(current_user)
) -> Dict[str, str]:
    draft = get_draft(draft_id)
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
    upsert_draft(draft)
    return {"status": "ok"}


def _resolve_attachment_path(record_id: str, filename: str, user: User) -> Path:
    """Common attachment lookup: locate the record (across emails/drafts/sent),
    enforce ownership, validate the path, and return a usable Path."""
    record = get_email(record_id) or get_draft(record_id) or get_sent(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="附件不存在。")
    _assert_record_belongs_to_user(record, user)
    try:
        path = get_attachment_path(record_id, filename)
    except ValueError:
        raise HTTPException(status_code=400, detail="非法的附件路径。")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="附件不存在。")
    return path


@app.get("/api/attachments/{record_id}/{filename}")
def download_attachment(
    record_id: str,
    filename: str,
    download: bool = Query(default=False),
    user: User = Depends(current_user),
) -> FileResponse:
    path = _resolve_attachment_path(record_id, filename, user)
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


def _is_local_client(request: Request) -> bool:
    # "Open with system default app" runs on the backend host. If the client
    # is remote, the file would open on the server rather than the user's
    # machine — never what they want. Restrict to loopback.
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


def _spawn_system_open(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform.startswith("win"):
        # os.startfile only exists on Windows; type checker on macOS complains.
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])


@app.post("/api/attachments/{record_id}/{filename}/open")
def open_attachment_with_system_app(
    record_id: str,
    filename: str,
    request: Request,
    user: User = Depends(current_user),
) -> Dict[str, str]:
    """Open an attachment with the OS default application on the machine
    running the backend. This is the desktop-app path that avoids the
    target=_blank/system-browser cookie loss problem entirely."""
    if not _is_local_client(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅本机客户端可使用系统默认应用打开附件。",
        )
    path = _resolve_attachment_path(record_id, filename, user)
    try:
        _spawn_system_open(path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=f"未找到系统打开命令：{exc}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"打开附件失败：{exc}")
    return {"status": "ok"}


def _unique_destination_path(target_dir: Path, filename: str) -> Path:
    # Avoid clobbering an existing file: "report.pdf" → "report (1).pdf".
    candidate = target_dir / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for i in range(1, 1000):
        candidate = target_dir / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    return target_dir / f"{stem}-{uuid.uuid4().hex[:8]}{suffix}"


class AttachmentSaveItem(BaseModel):
    record_id: str
    filename: str


class AttachmentSaveBatchRequest(BaseModel):
    folder: str
    items: List[AttachmentSaveItem]


class RevealPathRequest(BaseModel):
    path: str


@app.post("/api/system/reveal-path")
def reveal_path(
    payload: RevealPathRequest,
    request: Request,
    user: User = Depends(current_user),
) -> Dict[str, str]:
    """Open a folder in the system file browser (Finder / Explorer /
    Nautilus). Powers the "打开文件夹" button on the download-complete
    popup so the user can see what just landed on disk."""
    if not _is_local_client(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅本机客户端可使用此操作。",
        )
    target = Path(payload.path).expanduser()
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=400, detail=f"目录不存在：{target}")
    try:
        _spawn_system_open(target)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=f"未找到系统打开命令：{exc}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"打开目录失败：{exc}")
    return {"status": "ok"}


@app.post("/api/attachments/save-batch")
def save_attachments_batch(
    payload: AttachmentSaveBatchRequest,
    request: Request,
    user: User = Depends(current_user),
) -> Dict[str, Any]:
    """Copy one or more attachments into a folder the user picked. Used by
    both the single-attachment download path (one item in the list) and
    the multi-select bulk download. Needed because pywebview's WKWebView
    silently ignores in-page `<a download>` clicks, so the frontend can't
    rely on the browser-native download mechanism inside the desktop app."""
    if not _is_local_client(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅本机客户端可使用此下载方式。",
        )
    if not payload.items:
        raise HTTPException(status_code=400, detail="未指定要下载的附件。")
    target_dir = Path(payload.folder).expanduser()
    if not target_dir.exists() or not target_dir.is_dir():
        raise HTTPException(
            status_code=400, detail=f"目录不存在：{target_dir}"
        )
    saved: List[str] = []
    errors: List[Dict[str, str]] = []
    for item in payload.items:
        try:
            src = _resolve_attachment_path(item.record_id, item.filename, user)
            dst = _unique_destination_path(target_dir, item.filename)
            dst.write_bytes(src.read_bytes())
            saved.append(str(dst))
        except HTTPException as exc:
            errors.append({"filename": item.filename, "error": str(exc.detail)})
        except Exception as exc:  # noqa: BLE001
            errors.append({"filename": item.filename, "error": str(exc)})
    return {
        "folder": str(target_dir),
        "saved": saved,
        "errors": errors,
        "count": len(saved),
    }


@app.get("/api/sent", response_model=List[SentRecord])
def list_sent(user: User = Depends(current_user)) -> List[SentRecord]:
    """Returns every sent record for the active account — including
    soft-deleted ones. The frontend filters by `deleted` to split
    已发送 from 回收站. Matches the /api/emails convention."""
    active_id = get_user_active_account_id(user.id) or ""
    items = list_sent_for_account(active_id)
    items.sort(key=lambda x: x.get("sent_at", ""), reverse=True)
    return [SentRecord(**item) for item in items]


@app.post("/api/sent/{sent_id}/update", response_model=SentRecord)
def update_sent(
    sent_id: str, payload: SentUpdate, user: User = Depends(current_user)
) -> SentRecord:
    """Soft-delete / restore a sent record. Sent message bodies are
    immutable, so the only field this route accepts today is `deleted`."""
    target = get_sent(sent_id)
    if target is None:
        raise HTTPException(status_code=404, detail="已发送邮件不存在。")
    _assert_record_belongs_to_user(target, user)
    if payload.deleted is not None:
        target["deleted"] = bool(payload.deleted)
        target["deleted_at"] = (
            datetime.now(timezone.utc).isoformat() if target["deleted"] else None
        )
    upsert_sent(target)
    return SentRecord(**target)


@app.delete("/api/sent/{sent_id}")
def hard_delete_sent(
    sent_id: str, user: User = Depends(current_user)
) -> Dict[str, str]:
    """Permanently purge a sent record + its on-disk attachments. The
    regular 删除 button in 已发送 only soft-deletes; this route is the
    "彻底删除" path from 回收站."""
    target = get_sent(sent_id)
    if target is None:
        raise HTTPException(status_code=404, detail="已发送邮件不存在。")
    _assert_record_belongs_to_user(target, user)
    storage_delete_sent(sent_id)
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
    # Indexed SQL query, no need to load body_html for every stored email.
    known_uids = list_known_imap_uids(active_id)

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

    own_old = list_emails_for_account(active_id)
    merged, stored = dedupe_by_message_id(own_old, fetched)

    # Materialise pending attachments under each kept record's final id, then
    # strip the in-memory blobs so they don't leak into the persisted row.
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

    # Bulk upsert in one transaction — only this account's rows are touched;
    # other accounts' emails are never read or rewritten.
    upsert_emails(finalized)
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
    known_uids = list_known_imap_uids(active_id)

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
        """Dedupe + persist + materialise attachments for ONE batch; bump
        the watermark; enqueue a batch-saved event so the UI refreshes.
        Runs on the worker thread — fine, the storage layer's per-record
        upsert holds a row-level lock and we never overlap another receive
        on the same account."""
        if not batch_records:
            return
        for item in batch_records:
            item["account_id"] = active_id
        own_old = list_emails_for_account(active_id)
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

        upsert_emails(finalized)
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
    active_id = get_user_active_account_id(user.id) or ""
    items = list_emails_for_account(active_id)
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
    target = get_email(email_id)
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
    target = get_email(email_id)
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
    upsert_email(target)

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
    target = get_email(email_id)
    if target is None:
        raise HTTPException(status_code=404, detail="邮件不存在。")
    _assert_record_belongs_to_user(target, user)
    storage_delete_email(email_id)
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
