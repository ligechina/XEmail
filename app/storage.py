# Copyright (c) 2026 Peking University & Beijing Siliconheart Technology Co., Ltd.
# XEmail is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#          http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Persistent storage layer — SQLite backed.

Everything that used to live in `data/*.json` (emails, sent, drafts, contacts,
folders, prompts/rules/experiences/field_config, users, accounts, settings)
now lives in `data/xemail.db`. Binary blobs (attachments, cipher key, session
secret) stay on the filesystem.

On first run after the JSON→SQLite upgrade the module reads any legacy JSON
files it finds in `data/`, loads them into a fresh DB inside a single
transaction, atomically renames it into place, then deletes the JSONs. The
public API in this module keeps the same signatures it always had — callers
in `app/main.py`, `app/services/*`, and `desktop/app.py` are untouched.
"""

import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


log = logging.getLogger(__name__)


# ── Paths ─────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent
_data_dir_override = os.environ.get("XEMAIL_DATA_DIR", "").strip()
if _data_dir_override:
    _candidate = Path(_data_dir_override).expanduser()
    DATA_DIR = _candidate if _candidate.is_absolute() else (BASE_DIR / _candidate)
else:
    DATA_DIR = BASE_DIR / "data"

DB_FILE = DATA_DIR / "xemail.db"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
SESSION_SECRET_FILE = DATA_DIR / ".session_secret"
LLM_SECRET_FILE = DATA_DIR / ".llm_secret"

# Legacy JSON file paths — referenced only by the one-shot migration that
# runs the first time the DB is built. After migration these files are
# deleted, so any new code path that touches them is a bug.
_LEGACY_CONFIG_FILE = DATA_DIR / "config.json"
_LEGACY_EMAILS_FILE = DATA_DIR / "emails.json"
_LEGACY_DRAFTS_FILE = DATA_DIR / "drafts.json"
_LEGACY_SENT_FILE = DATA_DIR / "sent.json"
_LEGACY_FOLDERS_FILE = DATA_DIR / "folders.json"
_LEGACY_USERS_FILE = DATA_DIR / "users.json"
_LEGACY_PROMPTS_FILE = DATA_DIR / "prompts.json"
_LEGACY_CONTACTS_FILE = DATA_DIR / "contacts.json"


# ── Constants ─────────────────────────────────────────────────────────────

MAX_FILENAME_LEN = 200
UNCLASSIFIED_FOLDER = "未分类"
# Baseline folders the system always ensures exist on every account. Kept
# minimal: only the two buckets the classifier guarantees (垃圾邮件 for
# spam, 未分类 for "no rule matched"). Anything else is user-defined.
DEFAULT_FOLDERS: List[str] = [
    "垃圾邮件",
    UNCLASSIFIED_FOLDER,
]

SYSTEM_MODES = ("dev", "prod")
SCHEMA_VERSION = 1


# ── Schema DDL ────────────────────────────────────────────────────────────
#
# Design philosophy: indexed columns for fields that drive queries/sorting/
# filtering, plus a `data_json` blob column carrying the full record. New
# fields don't break the schema; queries on hot fields stay fast.

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS users (
  id                TEXT PRIMARY KEY,
  username          TEXT NOT NULL DEFAULT '',
  password_hash     TEXT NOT NULL DEFAULT '',
  role              TEXT NOT NULL DEFAULT 'normal',
  active_account_id TEXT,
  created_at        TEXT NOT NULL DEFAULT '',
  data_json         TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS accounts (
  id            TEXT PRIMARY KEY,
  label         TEXT,
  owner_user_id TEXT,
  ord           INTEGER NOT NULL DEFAULT 0,
  data_json     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_accounts_owner ON accounts(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_accounts_ord   ON accounts(ord);

CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS folders (
  account_id TEXT NOT NULL,
  name       TEXT NOT NULL,
  ord        INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (account_id, name)
);
CREATE INDEX IF NOT EXISTS idx_folders_account_ord ON folders(account_id, ord);

CREATE TABLE IF NOT EXISTS emails (
  id                TEXT PRIMARY KEY,
  account_id        TEXT NOT NULL,
  message_id        TEXT,
  from_email        TEXT,
  to_email          TEXT,
  subject           TEXT,
  received_at       TEXT NOT NULL DEFAULT '',
  category          TEXT,
  important         INTEGER NOT NULL DEFAULT 0,
  imap_uid          INTEGER,
  imap_mailbox      TEXT,
  source_message_id TEXT,
  in_reply_to       TEXT,
  data_json         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_emails_account_received  ON emails(account_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_emails_account_category  ON emails(account_id, category, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_emails_account_msgid     ON emails(account_id, message_id);
CREATE INDEX IF NOT EXISTS idx_emails_account_imapuid   ON emails(account_id, imap_mailbox, imap_uid);

-- FTS5 over the search-relevant fields. Kept content-less and updated by
-- application code (not triggers) — simpler to reason about during the
-- bulk-write paths that DELETE + reinsert.
CREATE VIRTUAL TABLE IF NOT EXISTS emails_fts USING fts5(
  email_id UNINDEXED,
  subject,
  body,
  from_email,
  to_email,
  tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS sent (
  id                TEXT PRIMARY KEY,
  account_id        TEXT NOT NULL,
  sent_at           TEXT NOT NULL DEFAULT '',
  subject           TEXT,
  source_message_id TEXT,
  data_json         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sent_account_sentat ON sent(account_id, sent_at DESC);

CREATE TABLE IF NOT EXISTS drafts (
  id         TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  updated_at TEXT,
  data_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_account ON drafts(account_id);

CREATE TABLE IF NOT EXISTS contacts (
  id         TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  email      TEXT,
  data_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contacts_account ON contacts(account_id);

CREATE TABLE IF NOT EXISTS prompts (
  id         TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  data_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prompts_account ON prompts(account_id);

CREATE TABLE IF NOT EXISTS fixed_rules (
  id         TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  ord        INTEGER NOT NULL DEFAULT 0,
  data_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fixed_rules_account_ord ON fixed_rules(account_id, ord);

CREATE TABLE IF NOT EXISTS experiences (
  id         TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  data_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_experiences_account ON experiences(account_id);

CREATE TABLE IF NOT EXISTS field_configs (
  account_id TEXT PRIMARY KEY,
  data_json  TEXT NOT NULL
);
"""


# ── Connection management ─────────────────────────────────────────────────
#
# One sqlite3.Connection per thread, lazily opened on first use. A shared
# module-level connection would seem simpler, but Python's sqlite3 module
# raises `InterfaceError: bad parameter or other API misuse` (SQLITE_MISUSE)
# when one thread's BEGIN IMMEDIATE / COMMIT interleaves with another
# thread's SELECT on the same connection — even with check_same_thread=False.
# Under FastAPI + uvicorn's thread pool that hits production immediately:
# the moment a write request and a read request overlap, every concurrent
# request starts 500-ing. Per-thread connections sidestep the problem at
# the C level. WAL mode keeps reads concurrent at the file level, and the
# 5s busy_timeout (default in `sqlite3.connect(timeout=...)`) makes write-
# write contention spin and retry instead of erroring out.

_bootstrap_lock = threading.Lock()
_bootstrap_done = False
_conn_local = threading.local()


def _ensure_data_files() -> None:
    """Ensure the data dir exists, the SQLite DB exists, and any legacy JSON
    files have been migrated. Safe to call repeatedly — work happens exactly
    once per process."""
    global _bootstrap_done
    if _bootstrap_done:
        return
    with _bootstrap_lock:
        if _bootstrap_done:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
        if not DB_FILE.exists():
            _migrate_json_to_db()
        else:
            # If a previous migration's "delete legacy JSON" step was
            # interrupted, the DB is authoritative — clean up the orphans now
            # so subsequent reads can't be confused into thinking JSON data is
            # still authoritative.
            _purge_legacy_json_files(silent=True)
        _bootstrap_done = True


def _get_conn() -> sqlite3.Connection:
    """Thread-local SQLite connection, opened lazily. First call in any
    thread also triggers migration (idempotent, globally serialized)."""
    conn = getattr(_conn_local, "conn", None)
    if conn is not None:
        return conn
    _ensure_data_files()
    # timeout=5.0 → if another writer holds the file lock, this connection's
    # next BEGIN IMMEDIATE spins up to 5s instead of failing immediately.
    conn = sqlite3.connect(
        str(DB_FILE),
        check_same_thread=False,
        isolation_level=None,  # autocommit; we BEGIN/COMMIT explicitly
        timeout=5.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    # Defensive: schema would already be in place from migration, but if
    # the DB was created by some external tool we still want the tables.
    conn.executescript(_SCHEMA_DDL)
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if ver < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    _conn_local.conn = conn
    return conn


class _WriteTxn:
    """Context manager: BEGIN IMMEDIATE on the thread-local connection,
    COMMIT on success or ROLLBACK on exception. Cross-thread serialization
    is provided by SQLite's file-level write lock (busy_timeout retries
    under contention) — no application-level lock needed."""

    def __init__(self) -> None:
        self.conn: Optional[sqlite3.Connection] = None
        self._owns_txn = False

    def __enter__(self) -> sqlite3.Connection:
        self.conn = _get_conn()
        # Reentrant: if this thread is already mid-transaction (nested
        # context-manager use), don't start a new one — let the outer
        # commit/rollback own the boundaries.
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
            self._owns_txn = True
        return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._owns_txn and self.conn is not None:
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")


def _write_txn() -> _WriteTxn:
    return _WriteTxn()


# ── ID generators ─────────────────────────────────────────────────────────

def _new_account_id() -> str:
    return f"acc_{uuid.uuid4().hex[:12]}"


def _new_user_id() -> str:
    return f"u_{uuid.uuid4().hex[:12]}"


def _new_prompt_id() -> str:
    return f"p_{uuid.uuid4().hex[:12]}"


def _new_rule_id() -> str:
    return f"r_{uuid.uuid4().hex[:12]}"


def _new_experience_id() -> str:
    return f"x_{uuid.uuid4().hex[:12]}"


def _new_contact_id() -> str:
    return f"ct_{uuid.uuid4().hex[:12]}"


# ── Atomic write helper (still needed for cipher key / session secret) ───

def _atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write `text` to `path` atomically — write to a sibling `.tmp` file,
    then `os.replace` it onto the target."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, path)


# ── Local secrets: encryption-at-rest ────────────────────────────────────
#
# Two pieces of config must never live on disk in cleartext:
#   - each account's SMTP/IMAP `sender_password`
#   - the LLM API key
# Both are encrypted with Fernet (AES-128-CBC + HMAC-SHA256). The symmetric
# key lives in data/.llm_secret (mode 0o600), separate from the DB so that
# leaking the DB alone (backups, accidental shares) does not disclose the
# secrets. Cleartext is materialised only in-memory and never logged.


def _get_local_cipher():
    """Lazy-load the Fernet cipher backed by data/.llm_secret. The secret
    file is generated on first use and locked down to owner-only (0o600).

    Despite the legacy filename, this cipher protects every at-rest secret
    in this codebase — both the LLM API key and account passwords. The
    filename is retained so existing installs don't lose their key after
    upgrade."""
    from cryptography.fernet import Fernet  # local import: not all callers need it

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not LLM_SECRET_FILE.exists():
        LLM_SECRET_FILE.write_bytes(Fernet.generate_key())
        try:
            LLM_SECRET_FILE.chmod(0o600)
        except Exception:
            pass
    return Fernet(LLM_SECRET_FILE.read_bytes())


def _decrypt_account_settings(settings: Dict[str, Any]) -> bool:
    """In-place: if `sender_password_enc` is present, decrypt it into
    `sender_password` and remove the ciphertext field. Returns True when a
    decryption actually happened."""
    if not isinstance(settings, dict):
        return False
    enc = settings.get("sender_password_enc")
    if not (isinstance(enc, str) and enc.strip()):
        return False
    try:
        settings["sender_password"] = (
            _get_local_cipher().decrypt(enc.encode("utf-8")).decode("utf-8")
        )
    except Exception:
        # Secret file rotated, ciphertext corrupted, or config transplanted
        # from another machine: surface as empty so the UI shows a blank
        # password field and the user can re-enter it. Never crash a request.
        settings["sender_password"] = ""
    settings.pop("sender_password_enc", None)
    return True


def _encrypt_account_settings(settings: Dict[str, Any]) -> None:
    """In-place: encrypt a non-empty `sender_password` into
    `sender_password_enc` and drop the cleartext field. Idempotent: when the
    password is absent / empty, both fields are cleared."""
    if not isinstance(settings, dict):
        return
    pwd = settings.get("sender_password")
    if isinstance(pwd, str) and pwd:
        token = _get_local_cipher().encrypt(pwd.encode("utf-8"))
        settings["sender_password_enc"] = token.decode("utf-8")
    settings.pop("sender_password", None)


# ── JSON → SQLite migration ──────────────────────────────────────────────

def _load_json_file(path: Path, default: Any) -> Any:
    """Read a legacy JSON file. Tolerates missing file / empty content."""
    if not path.exists():
        return default
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return default
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("Failed to parse %s during migration: %s", path.name, e)
        return default


def _legacy_load_config() -> Dict[str, Any]:
    """Load config.json, applying any pre-multi-account legacy reshape so the
    rest of the migration sees a uniform shape: {active_account_id, accounts,
    llm, system}. Also pulls in the most recent .bak if the live file is
    corrupt."""
    cfg = _load_json_file(_LEGACY_CONFIG_FILE, default=None)
    if cfg is None:
        # File missing or unparseable — try a .bak.
        baks = sorted(
            DATA_DIR.glob("config.json.bak.*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for bak in baks:
            cand = _load_json_file(bak, default=None)
            if isinstance(cand, dict):
                cfg = cand
                log.warning("Recovered config from backup %s", bak.name)
                break
    if not isinstance(cfg, dict):
        return {"active_account_id": None, "accounts": []}

    # Legacy single-account: top-level {settings, rules, sync} → wrap into
    # the multi-account form.
    if "accounts" not in cfg:
        legacy_settings = cfg.get("settings")
        if isinstance(legacy_settings, dict) and legacy_settings:
            acc_id = _new_account_id()
            cfg = {
                "active_account_id": acc_id,
                "accounts": [
                    {
                        "id": acc_id,
                        "label": legacy_settings.get("sender_email", "默认账号"),
                        "settings": legacy_settings,
                        "rules": cfg.get("rules", {}),
                        "sync": cfg.get("sync", {}),
                    }
                ],
                "llm": cfg.get("llm") or {},
                "system": cfg.get("system") or {},
            }
        else:
            cfg = {"active_account_id": None, "accounts": []}
    return cfg


def _migrate_json_to_db() -> None:
    """Build a fresh DB at DB_FILE.tmp from any existing JSON files, then
    os.replace it onto DB_FILE. Idempotent if interrupted: leftover .tmp
    files from a prior crashed run are cleaned up first."""
    tmp_path = DB_FILE.with_suffix(".db.tmp")
    if tmp_path.exists():
        try:
            tmp_path.unlink()
        except OSError as e:
            log.error("Failed to remove stale %s: %s", tmp_path.name, e)
            raise

    has_any_legacy = any(
        p.exists() for p in (
            _LEGACY_CONFIG_FILE, _LEGACY_EMAILS_FILE, _LEGACY_DRAFTS_FILE,
            _LEGACY_SENT_FILE, _LEGACY_FOLDERS_FILE, _LEGACY_USERS_FILE,
            _LEGACY_PROMPTS_FILE, _LEGACY_CONTACTS_FILE,
        )
    )
    if has_any_legacy:
        log.info("Migrating legacy JSON data into %s", DB_FILE.name)

    conn = sqlite3.connect(str(tmp_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SCHEMA_DDL)
        conn.execute("BEGIN")
        try:
            _migrate_load_all(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    os.replace(tmp_path, DB_FILE)
    if has_any_legacy:
        _purge_legacy_json_files()
        log.info("Migration complete; legacy JSON files removed")


def _migrate_load_all(conn: sqlite3.Connection) -> None:
    """Read every legacy JSON file and INSERT its rows into the open DB."""
    cfg = _legacy_load_config()
    active_acc_id = cfg.get("active_account_id") or ""

    # ── accounts (encrypt settings as we go) ──
    for ord_, acc in enumerate(cfg.get("accounts") or []):
        if not isinstance(acc, dict):
            continue
        acc = dict(acc)
        acc_id = acc.get("id") or _new_account_id()
        acc["id"] = acc_id
        if isinstance(acc.get("settings"), dict):
            settings = dict(acc["settings"])
            # Any cleartext sender_password gets encrypted here and removed
            # — matches what the legacy `read_config()` would have done on
            # next access. _encrypt_account_settings is idempotent so an
            # already-encrypted account just passes through unchanged.
            _encrypt_account_settings(settings)
            acc["settings"] = settings
        conn.execute(
            "INSERT INTO accounts (id, label, owner_user_id, ord, data_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                acc_id,
                acc.get("label"),
                acc.get("owner_user_id"),
                ord_,
                json.dumps(acc, ensure_ascii=False),
            ),
        )

    # ── settings (key/value) ──
    if cfg.get("active_account_id"):
        _set_setting(conn, "active_account_id", cfg["active_account_id"])
    llm = cfg.get("llm") or {}
    if isinstance(llm.get("api_key_enc"), str) and llm["api_key_enc"].strip():
        _set_setting(conn, "llm_api_key_enc", llm["api_key_enc"].strip())
    elif isinstance(llm.get("api_key"), str) and llm["api_key"].strip():
        # Legacy plaintext API key — encrypt it now.
        try:
            token = _get_local_cipher().encrypt(llm["api_key"].strip().encode("utf-8"))
            _set_setting(conn, "llm_api_key_enc", token.decode("utf-8"))
        except Exception as e:
            log.warning("Failed to encrypt legacy LLM api key during migration: %s", e)
    system = cfg.get("system") or {}
    if isinstance(system.get("mode"), str) and system["mode"].strip().lower() in SYSTEM_MODES:
        _set_setting(conn, "system_mode", system["mode"].strip().lower())
    desktop = system.get("desktop") or {}
    if isinstance(desktop, dict) and "enable_tray" in desktop:
        _set_setting(conn, "desktop_enable_tray", bool(desktop["enable_tray"]))

    # ── users ──
    users = _load_json_file(_LEGACY_USERS_FILE, default={"users": []})
    if isinstance(users, list):
        users_list = users
    else:
        users_list = (users or {}).get("users") or []
    for u in users_list:
        if not isinstance(u, dict):
            continue
        u = dict(u)
        uid = u.get("id") or _new_user_id()
        u["id"] = uid
        conn.execute(
            "INSERT INTO users (id, username, password_hash, role, "
            " active_account_id, created_at, data_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                uid,
                u.get("username", ""),
                u.get("password_hash", ""),
                u.get("role", "normal"),
                u.get("active_account_id"),
                u.get("created_at", ""),
                json.dumps(u, ensure_ascii=False),
            ),
        )

    # ── folders ──
    folders = _load_json_file(_LEGACY_FOLDERS_FILE, default={})
    # Legacy: bare list got bound to the active account on first read.
    if isinstance(folders, list):
        folders = {active_acc_id: folders} if active_acc_id else {}
    if isinstance(folders, dict):
        for acc_id, names in folders.items():
            if not isinstance(names, list):
                continue
            ord_counter = 0
            seen: set = set()
            for name in names:
                if not isinstance(name, str) or not name or name in seen:
                    continue
                seen.add(name)
                conn.execute(
                    "INSERT OR IGNORE INTO folders (account_id, name, ord) "
                    "VALUES (?, ?, ?)",
                    (acc_id, name, ord_counter),
                )
                ord_counter += 1

    # ── emails ──
    emails = _load_json_file(_LEGACY_EMAILS_FILE, default=[])
    if isinstance(emails, list):
        for e in emails:
            if not isinstance(e, dict):
                continue
            e = dict(e)
            eid = e.get("id") or uuid.uuid4().hex
            e["id"] = eid
            if not e.get("account_id") and active_acc_id:
                e["account_id"] = active_acc_id
            _insert_email_row(conn, e)

    # ── sent ──
    sent_list = _load_json_file(_LEGACY_SENT_FILE, default=[])
    if isinstance(sent_list, list):
        for s in sent_list:
            if not isinstance(s, dict):
                continue
            s = dict(s)
            sid = s.get("id") or uuid.uuid4().hex
            s["id"] = sid
            if not s.get("account_id") and active_acc_id:
                s["account_id"] = active_acc_id
            _insert_sent_row(conn, s)

    # ── drafts ──
    drafts = _load_json_file(_LEGACY_DRAFTS_FILE, default=[])
    if isinstance(drafts, list):
        for d in drafts:
            if not isinstance(d, dict):
                continue
            d = dict(d)
            did = d.get("id") or uuid.uuid4().hex
            d["id"] = did
            if not d.get("account_id") and active_acc_id:
                d["account_id"] = active_acc_id
            _insert_draft_row(conn, d)

    # ── contacts ──
    contacts = _load_json_file(_LEGACY_CONTACTS_FILE, default=[])
    if isinstance(contacts, list):
        for c in contacts:
            if not isinstance(c, dict):
                continue
            c = dict(c)
            cid = c.get("id") or _new_contact_id()
            c["id"] = cid
            _insert_contact_row(conn, c)

    # ── prompts.json (multi-shape: system, items, fixed_rules, experiences,
    #    field_configs) ──
    prompts = _load_json_file(_LEGACY_PROMPTS_FILE, default={})
    if isinstance(prompts, dict):
        sys_prompt = prompts.get("system")
        if isinstance(sys_prompt, str) and sys_prompt.strip():
            _set_setting(conn, "system_spam_prompt", sys_prompt)

        for p in prompts.get("items") or []:
            if not isinstance(p, dict):
                continue
            p = dict(p)
            pid = p.get("id") or _new_prompt_id()
            p["id"] = pid
            conn.execute(
                "INSERT INTO prompts (id, account_id, data_json) VALUES (?, ?, ?)",
                (pid, p.get("account_id") or "", json.dumps(p, ensure_ascii=False)),
            )

        for ord_, r in enumerate(prompts.get("fixed_rules") or []):
            if not isinstance(r, dict):
                continue
            r = dict(r)
            rid = r.get("id") or _new_rule_id()
            r["id"] = rid
            conn.execute(
                "INSERT INTO fixed_rules (id, account_id, ord, data_json) "
                "VALUES (?, ?, ?, ?)",
                (rid, r.get("account_id") or "", ord_,
                 json.dumps(r, ensure_ascii=False)),
            )

        for x in prompts.get("experiences") or []:
            if not isinstance(x, dict):
                continue
            x = dict(x)
            xid = x.get("id") or _new_experience_id()
            x["id"] = xid
            conn.execute(
                "INSERT INTO experiences (id, account_id, data_json) VALUES (?, ?, ?)",
                (xid, x.get("account_id") or "", json.dumps(x, ensure_ascii=False)),
            )

        field_configs = prompts.get("field_configs") or {}
        if isinstance(field_configs, dict):
            for acc_id, fc in field_configs.items():
                if not isinstance(acc_id, str) or not isinstance(fc, dict):
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO field_configs (account_id, data_json) "
                    "VALUES (?, ?)",
                    (acc_id, json.dumps(fc, ensure_ascii=False)),
                )


def _purge_legacy_json_files(*, silent: bool = False) -> None:
    """Delete every legacy JSON file and its sidecars. Best-effort: never
    raises. `silent=True` skips logging — used by the safety-net cleanup on
    startups after migration."""
    targets = [
        _LEGACY_CONFIG_FILE,
        _LEGACY_EMAILS_FILE,
        _LEGACY_DRAFTS_FILE,
        _LEGACY_SENT_FILE,
        _LEGACY_FOLDERS_FILE,
        _LEGACY_USERS_FILE,
        _LEGACY_PROMPTS_FILE,
        _LEGACY_CONTACTS_FILE,
    ]
    for p in targets:
        try:
            if p.exists():
                p.unlink()
        except OSError as e:
            if not silent:
                log.warning("Failed to remove %s: %s", p, e)
    # Backups + sidecars: never preserved (they may contain cleartext
    # passwords from the pre-encryption era).
    for pattern in ("config.json.bak.*", "config.json.corrupted.*", "*.json.tmp"):
        for p in DATA_DIR.glob(pattern):
            try:
                p.unlink()
            except OSError:
                pass


# ── Settings (key/value) helpers ────────────────────────────────────────

def _set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """Upsert a setting. Passing `None` deletes the row — callers use this
    to clear an optional field."""
    if value is None:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        return
    conn.execute(
        "INSERT INTO settings (key, value_json) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def _get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return default


# ── Row-write helpers (shared between migration + public API) ───────────

def _insert_email_row(conn: sqlite3.Connection, e: Dict[str, Any]) -> None:
    """INSERT one email + matching FTS row. Caller already supplied `id`."""
    eid = e["id"]
    conn.execute(
        "INSERT OR REPLACE INTO emails ("
        "  id, account_id, message_id, from_email, to_email, subject,"
        "  received_at, category, important, imap_uid, imap_mailbox,"
        "  source_message_id, in_reply_to, data_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            eid,
            e.get("account_id") or "",
            e.get("message_id"),
            e.get("from_email"),
            e.get("to_email"),
            e.get("subject"),
            e.get("received_at") or "",
            e.get("category"),
            1 if e.get("important") else 0,
            e.get("imap_uid"),
            e.get("imap_mailbox"),
            e.get("source_message_id"),
            e.get("in_reply_to"),
            json.dumps(e, ensure_ascii=False),
        ),
    )
    # FTS5: contentless table, so we manage the rows manually. INSERT OR
    # REPLACE on the parent doesn't cascade — clear any prior FTS row first.
    conn.execute("DELETE FROM emails_fts WHERE email_id = ?", (eid,))
    conn.execute(
        "INSERT INTO emails_fts (email_id, subject, body, from_email, to_email) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            eid,
            e.get("subject") or "",
            e.get("body") or "",
            e.get("from_email") or "",
            e.get("to_email") or "",
        ),
    )


def _insert_sent_row(conn: sqlite3.Connection, s: Dict[str, Any]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO sent ("
        "  id, account_id, sent_at, subject, source_message_id, data_json"
        ") VALUES (?, ?, ?, ?, ?, ?)",
        (
            s["id"],
            s.get("account_id") or "",
            s.get("sent_at") or "",
            s.get("subject"),
            s.get("source_message_id"),
            json.dumps(s, ensure_ascii=False),
        ),
    )


def _insert_draft_row(conn: sqlite3.Connection, d: Dict[str, Any]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO drafts (id, account_id, updated_at, data_json) "
        "VALUES (?, ?, ?, ?)",
        (
            d["id"],
            d.get("account_id") or "",
            d.get("updated_at"),
            json.dumps(d, ensure_ascii=False),
        ),
    )


def _insert_contact_row(conn: sqlite3.Connection, c: Dict[str, Any]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO contacts (id, account_id, email, data_json) "
        "VALUES (?, ?, ?, ?)",
        (
            c["id"],
            c.get("account_id") or "",
            c.get("email") or "",
            json.dumps(c, ensure_ascii=False),
        ),
    )


# ═════════════════════════════════════════════════════════════════════════
#  Public API
# ═════════════════════════════════════════════════════════════════════════


# ── config / accounts ────────────────────────────────────────────────────

def read_config() -> Dict[str, Any]:
    """Return a dict in the same shape callers always saw from config.json:
    {active_account_id, accounts, llm, system}. Synthesised from the DB.
    Every account's `sender_password_enc` is decrypted into a cleartext
    `sender_password` so callers see the legacy shape."""
    conn = _get_conn()
    accounts: List[Dict[str, Any]] = []
    for row in conn.execute("SELECT data_json FROM accounts ORDER BY ord"):
        acc = json.loads(row[0])
        if isinstance(acc.get("settings"), dict):
            _decrypt_account_settings(acc["settings"])
        accounts.append(acc)

    active = _get_setting(conn, "active_account_id")
    llm: Dict[str, Any] = {}
    api_enc = _get_setting(conn, "llm_api_key_enc")
    if api_enc:
        llm["api_key_enc"] = api_enc
    system: Dict[str, Any] = {}
    mode = _get_setting(conn, "system_mode")
    if mode:
        system["mode"] = mode
    enable_tray = _get_setting(conn, "desktop_enable_tray")
    if enable_tray is not None:
        system["desktop"] = {"enable_tray": bool(enable_tray)}

    return {
        "active_account_id": active,
        "accounts": accounts,
        "llm": llm,
        "system": system,
    }


def write_config(data: Dict[str, Any]) -> None:
    """Replace the entire config from a dict in the legacy shape. Encrypts
    every account's `sender_password` before persisting. Bulk-rewrites the
    accounts table — fine since accounts are typically ≤ a handful."""
    accounts_in = list((data or {}).get("accounts") or [])
    with _write_txn() as conn:
        conn.execute("DELETE FROM accounts")
        for ord_, acc in enumerate(accounts_in):
            if not isinstance(acc, dict):
                continue
            acc = dict(acc)
            if isinstance(acc.get("settings"), dict):
                settings = dict(acc["settings"])
                _encrypt_account_settings(settings)
                acc["settings"] = settings
            conn.execute(
                "INSERT INTO accounts (id, label, owner_user_id, ord, data_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    acc.get("id"),
                    acc.get("label"),
                    acc.get("owner_user_id"),
                    ord_,
                    json.dumps(acc, ensure_ascii=False),
                ),
            )
        _set_setting(conn, "active_account_id", (data or {}).get("active_account_id"))
        llm = (data or {}).get("llm") or {}
        api_enc = str(llm.get("api_key_enc") or "").strip()
        _set_setting(conn, "llm_api_key_enc", api_enc or None)
        system = (data or {}).get("system") or {}
        mode = system.get("mode")
        _set_setting(conn, "system_mode", mode if isinstance(mode, str) and mode else None)
        desktop = system.get("desktop") or {}
        if isinstance(desktop, dict) and "enable_tray" in desktop:
            _set_setting(conn, "desktop_enable_tray", bool(desktop["enable_tray"]))


def list_accounts() -> List[Dict[str, Any]]:
    return list(read_config().get("accounts") or [])


def get_account(account_id: str) -> Optional[Dict[str, Any]]:
    if not account_id:
        return None
    conn = _get_conn()
    row = conn.execute(
        "SELECT data_json FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if row is None:
        return None
    acc = json.loads(row[0])
    if isinstance(acc.get("settings"), dict):
        _decrypt_account_settings(acc["settings"])
    return acc


def get_active_account_id() -> Optional[str]:
    return _get_setting(_get_conn(), "active_account_id")


def get_active_account() -> Optional[Dict[str, Any]]:
    acc_id = get_active_account_id()
    return get_account(acc_id) if acc_id else None


def set_active_account_id(account_id: str) -> None:
    with _write_txn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if not exists:
            raise ValueError("account not found")
        _set_setting(conn, "active_account_id", account_id)


def add_account(account: Dict[str, Any]) -> str:
    acc = dict(account or {})
    acc_id = acc.get("id") or _new_account_id()
    acc["id"] = acc_id
    if isinstance(acc.get("settings"), dict):
        settings = dict(acc["settings"])
        _encrypt_account_settings(settings)
        acc["settings"] = settings
    with _write_txn() as conn:
        max_ord_row = conn.execute("SELECT COALESCE(MAX(ord), -1) FROM accounts").fetchone()
        next_ord = (max_ord_row[0] if max_ord_row else -1) + 1
        conn.execute(
            "INSERT INTO accounts (id, label, owner_user_id, ord, data_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                acc_id,
                acc.get("label"),
                acc.get("owner_user_id"),
                next_ord,
                json.dumps(acc, ensure_ascii=False),
            ),
        )
        if not _get_setting(conn, "active_account_id"):
            _set_setting(conn, "active_account_id", acc_id)
    return acc_id


def update_account(account_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    with _write_txn() as conn:
        row = conn.execute(
            "SELECT data_json FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if row is None:
            raise ValueError("account not found")
        acc = json.loads(row[0])
        for k, v in (fields or {}).items():
            if v is not None:
                acc[k] = v
        if isinstance(acc.get("settings"), dict):
            settings = dict(acc["settings"])
            _encrypt_account_settings(settings)
            acc["settings"] = settings
        conn.execute(
            "UPDATE accounts SET label = ?, owner_user_id = ?, data_json = ? "
            "WHERE id = ?",
            (
                acc.get("label"),
                acc.get("owner_user_id"),
                json.dumps(acc, ensure_ascii=False),
                account_id,
            ),
        )
    # Return the decrypted shape the caller expects.
    return get_account(account_id) or {}


def delete_account(account_id: str) -> None:
    """Remove the account and cascade-delete all of its data (emails, sent,
    drafts, contacts, folders, prompts, rules, experiences, field config)
    plus the attachment folders for any deleted email/sent record."""
    if not account_id:
        return
    # Collect record IDs to clean up their attachment folders before the
    # DB rows are gone.
    conn = _get_conn()
    record_ids: List[str] = []
    for tbl in ("emails", "sent"):
        for r in conn.execute(f"SELECT id FROM {tbl} WHERE account_id = ?", (account_id,)):
            record_ids.append(r[0])

    with _write_txn() as conn:
        # Wipe FTS rows for this account's emails before we lose the join.
        conn.execute(
            "DELETE FROM emails_fts WHERE email_id IN "
            "(SELECT id FROM emails WHERE account_id = ?)",
            (account_id,),
        )
        for tbl in ("emails", "sent", "drafts", "contacts", "folders",
                    "prompts", "fixed_rules", "experiences"):
            conn.execute(f"DELETE FROM {tbl} WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM field_configs WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        active = _get_setting(conn, "active_account_id")
        if active == account_id:
            next_row = conn.execute(
                "SELECT id FROM accounts ORDER BY ord LIMIT 1"
            ).fetchone()
            _set_setting(conn, "active_account_id", next_row[0] if next_row else None)

    for rid in record_ids:
        try:
            delete_attachments_folder(rid)
        except Exception:  # noqa: BLE001
            pass


# ── emails / sent / drafts (bulk read+replace, plus per-record helpers) ─

def read_emails() -> List[Dict[str, Any]]:
    """Return every email row in insertion order. Compatible with the legacy
    JSON shape — callers that expect a single list still work."""
    conn = _get_conn()
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT data_json FROM emails ORDER BY rowid"
    )]


def write_emails(data: List[Dict[str, Any]]) -> None:
    """Replace the entire emails table from `data`. O(n) — preferred for
    bulk imports. Per-record updates should use upsert_email / delete_email
    instead."""
    with _write_txn() as conn:
        conn.execute("DELETE FROM emails")
        conn.execute("DELETE FROM emails_fts")
        for e in (data or []):
            if not isinstance(e, dict):
                continue
            e = dict(e)
            if not e.get("id"):
                e["id"] = uuid.uuid4().hex
            _insert_email_row(conn, e)


def upsert_email(email: Dict[str, Any]) -> Dict[str, Any]:
    """Single-row insert-or-update. Returns the canonical record. Far cheaper
    than read_emails+modify+write_emails for a single change."""
    e = dict(email or {})
    if not e.get("id"):
        e["id"] = uuid.uuid4().hex
    with _write_txn() as conn:
        _insert_email_row(conn, e)
    return e


def delete_email(email_id: str) -> bool:
    """Single-row delete. Returns True when a row was removed."""
    if not email_id:
        return False
    with _write_txn() as conn:
        conn.execute("DELETE FROM emails_fts WHERE email_id = ?", (email_id,))
        cur = conn.execute("DELETE FROM emails WHERE id = ?", (email_id,))
        return cur.rowcount > 0


def get_email(email_id: str) -> Optional[Dict[str, Any]]:
    if not email_id:
        return None
    row = _get_conn().execute(
        "SELECT data_json FROM emails WHERE id = ?", (email_id,)
    ).fetchone()
    return json.loads(row[0]) if row else None


def list_emails_for_account(account_id: str) -> List[Dict[str, Any]]:
    """All emails belonging to one account, in insertion order. Indexed
    lookup — avoids loading other accounts' rows just to filter them out."""
    if not account_id:
        return []
    return [json.loads(r[0]) for r in _get_conn().execute(
        "SELECT data_json FROM emails WHERE account_id = ? ORDER BY rowid",
        (account_id,),
    )]


def list_known_imap_uids(account_id: str) -> set:
    """Set of `str(imap_uid)` values for one account. Indexed query — the
    receive flow uses this to skip mail it has already stored, without
    loading every email's full body_html. Returns an empty set on missing
    account_id."""
    if not account_id:
        return set()
    rows = _get_conn().execute(
        "SELECT imap_uid FROM emails "
        "WHERE account_id = ? AND imap_uid IS NOT NULL",
        (account_id,),
    )
    return {str(r[0]) for r in rows if r[0] is not None}


def upsert_emails(emails: List[Dict[str, Any]]) -> None:
    """Bulk upsert in one transaction. Prefer this over a Python loop around
    `upsert_email` when persisting a batch (e.g. after a receive cycle) —
    one BEGIN/COMMIT pair, one fsync."""
    with _write_txn() as conn:
        for e in (emails or []):
            if not isinstance(e, dict):
                continue
            e = dict(e)
            if not e.get("id"):
                e["id"] = uuid.uuid4().hex
            _insert_email_row(conn, e)


def read_drafts() -> List[Dict[str, Any]]:
    return [json.loads(r[0]) for r in _get_conn().execute(
        "SELECT data_json FROM drafts ORDER BY rowid"
    )]


def write_drafts(data: List[Dict[str, Any]]) -> None:
    with _write_txn() as conn:
        conn.execute("DELETE FROM drafts")
        for d in (data or []):
            if not isinstance(d, dict):
                continue
            d = dict(d)
            if not d.get("id"):
                d["id"] = uuid.uuid4().hex
            _insert_draft_row(conn, d)


def upsert_draft(draft: Dict[str, Any]) -> Dict[str, Any]:
    d = dict(draft or {})
    if not d.get("id"):
        d["id"] = uuid.uuid4().hex
    with _write_txn() as conn:
        _insert_draft_row(conn, d)
    return d


def delete_draft(draft_id: str) -> bool:
    if not draft_id:
        return False
    with _write_txn() as conn:
        cur = conn.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))
        return cur.rowcount > 0


def get_draft(draft_id: str) -> Optional[Dict[str, Any]]:
    if not draft_id:
        return None
    row = _get_conn().execute(
        "SELECT data_json FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()
    return json.loads(row[0]) if row else None


def list_drafts_for_account(account_id: str) -> List[Dict[str, Any]]:
    if not account_id:
        return []
    return [json.loads(r[0]) for r in _get_conn().execute(
        "SELECT data_json FROM drafts WHERE account_id = ? ORDER BY rowid",
        (account_id,),
    )]


def read_sent() -> List[Dict[str, Any]]:
    return [json.loads(r[0]) for r in _get_conn().execute(
        "SELECT data_json FROM sent ORDER BY rowid"
    )]


def write_sent(data: List[Dict[str, Any]]) -> None:
    with _write_txn() as conn:
        conn.execute("DELETE FROM sent")
        for s in (data or []):
            if not isinstance(s, dict):
                continue
            s = dict(s)
            if not s.get("id"):
                s["id"] = uuid.uuid4().hex
            _insert_sent_row(conn, s)


def upsert_sent(sent: Dict[str, Any]) -> Dict[str, Any]:
    s = dict(sent or {})
    if not s.get("id"):
        s["id"] = uuid.uuid4().hex
    with _write_txn() as conn:
        _insert_sent_row(conn, s)
    return s


def delete_sent(sent_id: str) -> bool:
    if not sent_id:
        return False
    with _write_txn() as conn:
        cur = conn.execute("DELETE FROM sent WHERE id = ?", (sent_id,))
        return cur.rowcount > 0


def get_sent(sent_id: str) -> Optional[Dict[str, Any]]:
    if not sent_id:
        return None
    row = _get_conn().execute(
        "SELECT data_json FROM sent WHERE id = ?", (sent_id,)
    ).fetchone()
    return json.loads(row[0]) if row else None


def list_sent_for_account(account_id: str) -> List[Dict[str, Any]]:
    if not account_id:
        return []
    return [json.loads(r[0]) for r in _get_conn().execute(
        "SELECT data_json FROM sent WHERE account_id = ? ORDER BY rowid",
        (account_id,),
    )]


# ── prompts / folders import + replace ──────────────────────────────────

def import_prompts_for_account(
    account_id: str, data: Dict[str, Any]
) -> Dict[str, int]:
    """Per-account import. Replaces THIS account's prompts / fixed_rules /
    experiences / field_config with whatever's in `data`, leaving other
    accounts and the global system prompt untouched.

    Imported items are stripped of their original `account_id` and `id`,
    then re-tagged with `account_id = account_id` and fresh ids.

    `field_configs` (if any) — first non-empty entry is taken and written
    under this account's slot. `system` is intentionally ignored — global.

    Returns counts: {items, fixed_rules, experiences, field_configs}.
    """
    if not account_id:
        raise ValueError("account_id 不能为空")
    if not isinstance(data, dict):
        raise ValueError("prompts.json 顶层必须是一个对象 / dict")

    def _retag(seq: Any) -> List[Dict[str, Any]]:
        if not isinstance(seq, list):
            return []
        out: List[Dict[str, Any]] = []
        for item in seq:
            if not isinstance(item, dict):
                continue
            copy = dict(item)
            copy["account_id"] = account_id
            copy.pop("id", None)
            out.append(copy)
        return out

    new_items = _retag(data.get("items"))
    new_rules = _retag(data.get("fixed_rules"))
    new_experiences = _retag(data.get("experiences"))

    fc_raw = data.get("field_configs")
    new_field_config: Optional[Dict[str, Any]] = None
    if isinstance(fc_raw, dict) and fc_raw:
        for v in fc_raw.values():
            if isinstance(v, dict):
                new_field_config = dict(v)
                break

    with _write_txn() as conn:
        conn.execute("DELETE FROM prompts WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM fixed_rules WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM experiences WHERE account_id = ?", (account_id,))
        for p in new_items:
            p["id"] = _new_prompt_id()
            conn.execute(
                "INSERT INTO prompts (id, account_id, data_json) VALUES (?, ?, ?)",
                (p["id"], account_id, json.dumps(p, ensure_ascii=False)),
            )
        for ord_, r in enumerate(new_rules):
            r["id"] = _new_rule_id()
            conn.execute(
                "INSERT INTO fixed_rules (id, account_id, ord, data_json) "
                "VALUES (?, ?, ?, ?)",
                (r["id"], account_id, ord_, json.dumps(r, ensure_ascii=False)),
            )
        for x in new_experiences:
            x["id"] = _new_experience_id()
            conn.execute(
                "INSERT INTO experiences (id, account_id, data_json) VALUES (?, ?, ?)",
                (x["id"], account_id, json.dumps(x, ensure_ascii=False)),
            )
        if new_field_config is not None:
            conn.execute(
                "INSERT OR REPLACE INTO field_configs (account_id, data_json) "
                "VALUES (?, ?)",
                (account_id, json.dumps(new_field_config, ensure_ascii=False)),
            )

    return {
        "items": len(new_items),
        "fixed_rules": len(new_rules),
        "experiences": len(new_experiences),
        "field_configs": 1 if new_field_config is not None else 0,
    }


def import_folders_for_account(
    account_id: str, data: Dict[str, Any]
) -> Dict[str, int]:
    """Per-account folders import. Accepts either the export shape
    `{<acc_id>: ["A", "B/sub", ...]}` or a bare list `["A", "B/sub", ...]`."""
    if not account_id:
        raise ValueError("account_id 不能为空")

    collected: List[str] = []
    if isinstance(data, list):
        for f in data:
            if isinstance(f, str) and f.strip():
                collected.append(f.strip())
    elif isinstance(data, dict):
        for v in data.values():
            if not isinstance(v, list):
                continue
            for f in v:
                if isinstance(f, str) and f.strip():
                    collected.append(f.strip())
    else:
        raise ValueError("folders.json 顶层必须是对象 / 数组")

    seen: set = set()
    deduped: List[str] = []
    for f in collected:
        if f in seen:
            continue
        seen.add(f)
        deduped.append(f)

    with _write_txn() as conn:
        conn.execute("DELETE FROM folders WHERE account_id = ?", (account_id,))
        for ord_, name in enumerate(deduped):
            conn.execute(
                "INSERT INTO folders (account_id, name, ord) VALUES (?, ?, ?)",
                (account_id, name, ord_),
            )
    return {"total_folders": len(deduped)}


def replace_prompts_file(data: Dict[str, Any]) -> Dict[str, int]:
    """Overwrite the entire prompts/rules/experiences/system store from
    `data`. Used by the admin import endpoint."""
    if not isinstance(data, dict):
        raise ValueError("prompts.json 顶层必须是一个对象 / dict")

    sys_in = data.get("system")
    sys_norm: Optional[str] = sys_in if isinstance(sys_in, str) else None

    items = list(data.get("items") or []) if isinstance(data.get("items"), list) else []
    rules = list(data.get("fixed_rules") or []) if isinstance(data.get("fixed_rules"), list) else []
    experiences = list(data.get("experiences") or []) if isinstance(data.get("experiences"), list) else []
    field_configs = dict(data.get("field_configs") or {}) if isinstance(data.get("field_configs"), dict) else {}

    with _write_txn() as conn:
        _set_setting(conn, "system_spam_prompt", sys_norm)
        conn.execute("DELETE FROM prompts")
        conn.execute("DELETE FROM fixed_rules")
        conn.execute("DELETE FROM experiences")
        conn.execute("DELETE FROM field_configs")

        for p in items:
            if not isinstance(p, dict):
                continue
            p = dict(p)
            if not p.get("id"):
                p["id"] = _new_prompt_id()
            conn.execute(
                "INSERT INTO prompts (id, account_id, data_json) VALUES (?, ?, ?)",
                (p["id"], p.get("account_id") or "", json.dumps(p, ensure_ascii=False)),
            )
        for ord_, r in enumerate(rules):
            if not isinstance(r, dict):
                continue
            r = dict(r)
            if not r.get("id"):
                r["id"] = _new_rule_id()
            conn.execute(
                "INSERT INTO fixed_rules (id, account_id, ord, data_json) "
                "VALUES (?, ?, ?, ?)",
                (r["id"], r.get("account_id") or "", ord_,
                 json.dumps(r, ensure_ascii=False)),
            )
        for x in experiences:
            if not isinstance(x, dict):
                continue
            x = dict(x)
            if not x.get("id"):
                x["id"] = _new_experience_id()
            conn.execute(
                "INSERT INTO experiences (id, account_id, data_json) VALUES (?, ?, ?)",
                (x["id"], x.get("account_id") or "",
                 json.dumps(x, ensure_ascii=False)),
            )
        for acc_id, fc in field_configs.items():
            if not isinstance(acc_id, str) or not isinstance(fc, dict):
                continue
            conn.execute(
                "INSERT INTO field_configs (account_id, data_json) VALUES (?, ?)",
                (acc_id, json.dumps(fc, ensure_ascii=False)),
            )
    return {
        "system": 1 if sys_norm else 0,
        "items": len(items),
        "fixed_rules": len(rules),
        "experiences": len(experiences),
        "field_configs": len(field_configs),
    }


def replace_folders_file(data: Dict[str, List[str]]) -> Dict[str, int]:
    """Overwrite the entire folders store from `data`."""
    if not isinstance(data, dict):
        raise ValueError("folders.json 顶层必须是一个 {account_id: [folder, ...]} 形式的对象")

    cleaned: Dict[str, List[str]] = {}
    total = 0
    for acc_id, entries in data.items():
        if not isinstance(acc_id, str) or not acc_id:
            continue
        if not isinstance(entries, list):
            raise ValueError(f"账号 {acc_id} 对应的值必须是字符串数组")
        out: List[str] = []
        for f in entries:
            if isinstance(f, str) and f.strip():
                out.append(f.strip())
        cleaned[acc_id] = out
        total += len(out)

    with _write_txn() as conn:
        conn.execute("DELETE FROM folders")
        for acc_id, names in cleaned.items():
            for ord_, name in enumerate(names):
                conn.execute(
                    "INSERT OR IGNORE INTO folders (account_id, name, ord) "
                    "VALUES (?, ?, ?)",
                    (acc_id, name, ord_),
                )
    return {"accounts": len(cleaned), "total_folders": total}


def read_folders(account_id: Optional[str] = None) -> List[str]:
    """Folders for the given account (active account if omitted). Defaults
    are always appended so the classifier never lacks the baseline categories."""
    if account_id is None:
        account_id = get_active_account_id() or ""
    conn = _get_conn()
    items: List[str] = [
        r[0] for r in conn.execute(
            "SELECT name FROM folders WHERE account_id = ? ORDER BY ord",
            (account_id,),
        )
    ]
    seen: set = set()
    out: List[str] = []
    for f in items + DEFAULT_FOLDERS:
        if isinstance(f, str) and f and f not in seen:
            seen.add(f)
            out.append(f)
    return out


def write_folders(data: List[str], account_id: Optional[str] = None) -> None:
    if account_id is None:
        account_id = get_active_account_id() or ""
    if not account_id:
        return
    with _write_txn() as conn:
        conn.execute("DELETE FROM folders WHERE account_id = ?", (account_id,))
        seen: set = set()
        ord_ = 0
        for name in (data or []):
            if not isinstance(name, str) or not name or name in seen:
                continue
            seen.add(name)
            conn.execute(
                "INSERT INTO folders (account_id, name, ord) VALUES (?, ?, ?)",
                (account_id, name, ord_),
            )
            ord_ += 1


# ── attachment storage (filesystem-backed, unchanged from JSON era) ─────

def sanitize_filename(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "attachment"
    name = name.replace("\\", "/").split("/")[-1]
    name = name.replace("..", "_")
    if not name or name in (".", ".."):
        return "attachment"
    return name[:MAX_FILENAME_LEN]


def _sanitize_record_id(record_id: str) -> str:
    rid = (record_id or "").strip()
    if not rid or "/" in rid or "\\" in rid or ".." in rid:
        raise ValueError("invalid record id")
    return rid


def attachments_dir_for(record_id: str) -> Path:
    return ATTACHMENTS_DIR / _sanitize_record_id(record_id)


def _unique_filename(folder: Path, filename: str) -> str:
    base = folder / filename
    if not base.exists():
        return filename
    stem = base.stem
    suffix = base.suffix
    i = 1
    while True:
        cand = f"{stem} ({i}){suffix}"
        if not (folder / cand).exists():
            return cand
        i += 1


def save_attachment_bytes(
    record_id: str,
    filename: str,
    data: bytes,
    content_type: str = "application/octet-stream",
) -> Dict[str, Any]:
    _ensure_data_files()
    folder = attachments_dir_for(record_id)
    folder.mkdir(parents=True, exist_ok=True)
    safe = sanitize_filename(filename)
    final_name = _unique_filename(folder, safe)
    (folder / final_name).write_bytes(data)
    return {
        "filename": final_name,
        "size": len(data),
        "content_type": content_type or "application/octet-stream",
    }


def list_attachments_meta(record_id: str) -> List[Dict[str, Any]]:
    try:
        folder = attachments_dir_for(record_id)
    except ValueError:
        return []
    if not folder.exists():
        return []
    out: List[Dict[str, Any]] = []
    for p in sorted(folder.iterdir()):
        if p.is_file():
            out.append(
                {
                    "filename": p.name,
                    "size": p.stat().st_size,
                    "content_type": "application/octet-stream",
                }
            )
    return out


def get_attachment_path(record_id: str, filename: str) -> Path:
    folder = attachments_dir_for(record_id)
    safe = sanitize_filename(filename)
    target = (folder / safe).resolve()
    folder_resolved = folder.resolve()
    if not (target == folder_resolved or folder_resolved in target.parents):
        raise ValueError("invalid attachment path")
    return target


def delete_attachment_file(record_id: str, filename: str) -> bool:
    p = get_attachment_path(record_id, filename)
    if p.exists() and p.is_file():
        p.unlink()
        return True
    return False


def delete_attachments_folder(record_id: str) -> None:
    try:
        folder = attachments_dir_for(record_id)
    except ValueError:
        return
    if folder.exists():
        shutil.rmtree(folder)


def copy_attachments_folder(src_id: str, dst_id: str) -> None:
    try:
        src = attachments_dir_for(src_id)
        dst = attachments_dir_for(dst_id)
    except ValueError:
        return
    if not src.exists():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        if not f.is_file():
            continue
        target_name = (
            _unique_filename(dst, f.name) if (dst / f.name).exists() else f.name
        )
        (dst / target_name).write_bytes(f.read_bytes())


def move_attachments_folder(src_id: str, dst_id: str) -> None:
    if src_id == dst_id:
        return
    try:
        src = attachments_dir_for(src_id)
        dst = attachments_dir_for(dst_id)
    except ValueError:
        return
    if not src.exists():
        return
    if dst.exists():
        for f in src.iterdir():
            if not f.is_file():
                continue
            target_name = (
                _unique_filename(dst, f.name) if (dst / f.name).exists() else f.name
            )
            (dst / target_name).write_bytes(f.read_bytes())
        shutil.rmtree(src)
    else:
        src.rename(dst)


# ── users ────────────────────────────────────────────────────────────────

def _user_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return json.loads(row["data_json"])


def list_users() -> List[Dict[str, Any]]:
    return [_user_row_to_dict(r) for r in _get_conn().execute(
        "SELECT data_json FROM users ORDER BY rowid"
    )]


def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    if not user_id:
        return None
    row = _get_conn().execute(
        "SELECT data_json FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return _user_row_to_dict(row) if row else None


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    target = (username or "").strip().lower()
    if not target:
        return None
    row = _get_conn().execute(
        "SELECT data_json FROM users WHERE lower(username) = ?", (target,)
    ).fetchone()
    return _user_row_to_dict(row) if row else None


def has_any_user() -> bool:
    row = _get_conn().execute("SELECT 1 FROM users LIMIT 1").fetchone()
    return row is not None


def add_user(user: Dict[str, Any]) -> str:
    u = dict(user or {})
    uid = u.get("id") or _new_user_id()
    u["id"] = uid
    with _write_txn() as conn:
        conn.execute(
            "INSERT INTO users (id, username, password_hash, role, "
            " active_account_id, created_at, data_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                uid,
                u.get("username", ""),
                u.get("password_hash", ""),
                u.get("role", "normal"),
                u.get("active_account_id"),
                u.get("created_at", ""),
                json.dumps(u, ensure_ascii=False),
            ),
        )
    return uid


def update_user(user_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    with _write_txn() as conn:
        row = conn.execute(
            "SELECT data_json FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            raise ValueError("user not found")
        u = json.loads(row[0])
        for k, v in (fields or {}).items():
            u[k] = v
        conn.execute(
            "UPDATE users SET username = ?, password_hash = ?, role = ?, "
            " active_account_id = ?, data_json = ? WHERE id = ?",
            (
                u.get("username", ""),
                u.get("password_hash", ""),
                u.get("role", "normal"),
                u.get("active_account_id"),
                json.dumps(u, ensure_ascii=False),
                user_id,
            ),
        )
        return u


def delete_user(user_id: str) -> None:
    """Remove a user and cascade-delete every email account they own."""
    conn = _get_conn()
    owned_account_ids = [
        r[0] for r in conn.execute(
            "SELECT id FROM accounts WHERE owner_user_id = ?", (user_id,)
        )
    ]
    with _write_txn() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    for acc_id in owned_account_ids:
        try:
            delete_account(acc_id)
        except Exception:  # noqa: BLE001
            continue


def set_user_active_account(user_id: str, account_id: Optional[str]) -> None:
    update_user(user_id, {"active_account_id": account_id})


def get_user_active_account_id(user_id: str) -> Optional[str]:
    u = get_user(user_id)
    return u.get("active_account_id") if u else None


# ── ownership migration ─────────────────────────────────────────────────

def assign_orphan_accounts_to(user_id: str) -> int:
    """Stamp owner_user_id on every account that has none. Used at first-
    admin setup so legacy accounts created before the user system land
    under the bootstrap admin."""
    if not user_id:
        return 0
    touched = 0
    with _write_txn() as conn:
        rows = conn.execute(
            "SELECT id, data_json FROM accounts WHERE owner_user_id IS NULL"
        ).fetchall()
        for row in rows:
            acc = json.loads(row[1])
            acc["owner_user_id"] = user_id
            conn.execute(
                "UPDATE accounts SET owner_user_id = ?, data_json = ? WHERE id = ?",
                (user_id, json.dumps(acc, ensure_ascii=False), row[0]),
            )
            touched += 1
    return touched


# ── spam classification prompts (system + per-account items) ────────────

def read_system_spam_prompt() -> Optional[str]:
    """Admin-edited override for the built-in spam-detection prompt. Returns
    None when admin hasn't customised it yet — callers should fall back to
    the hard-coded default in app.services.spam_filter."""
    val = _get_setting(_get_conn(), "system_spam_prompt")
    return val if isinstance(val, str) and val else None


def write_system_spam_prompt(text: Optional[str]) -> None:
    with _write_txn() as conn:
        _set_setting(conn, "system_spam_prompt", text or None)


def list_prompts_for_account(account_id: str) -> List[Dict[str, Any]]:
    if not account_id:
        return []
    return [json.loads(r[0]) for r in _get_conn().execute(
        "SELECT data_json FROM prompts WHERE account_id = ? ORDER BY rowid",
        (account_id,),
    )]


def get_prompt(prompt_id: str) -> Optional[Dict[str, Any]]:
    if not prompt_id:
        return None
    row = _get_conn().execute(
        "SELECT data_json FROM prompts WHERE id = ?", (prompt_id,)
    ).fetchone()
    return json.loads(row[0]) if row else None


def add_prompt(prompt: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(prompt or {})
    pid = p.get("id") or _new_prompt_id()
    p["id"] = pid
    with _write_txn() as conn:
        conn.execute(
            "INSERT INTO prompts (id, account_id, data_json) VALUES (?, ?, ?)",
            (pid, p.get("account_id") or "", json.dumps(p, ensure_ascii=False)),
        )
    return p


def update_prompt(prompt_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    with _write_txn() as conn:
        row = conn.execute(
            "SELECT data_json FROM prompts WHERE id = ?", (prompt_id,)
        ).fetchone()
        if row is None:
            raise ValueError("prompt not found")
        p = json.loads(row[0])
        for k, v in (fields or {}).items():
            p[k] = v
        conn.execute(
            "UPDATE prompts SET account_id = ?, data_json = ? WHERE id = ?",
            (p.get("account_id") or "", json.dumps(p, ensure_ascii=False), prompt_id),
        )
        return p


def delete_prompt(prompt_id: str) -> None:
    if not prompt_id:
        return
    with _write_txn() as conn:
        conn.execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))


# ── fixed rules ─────────────────────────────────────────────────────────

def list_fixed_rules_for_account(account_id: str) -> List[Dict[str, Any]]:
    if not account_id:
        return []
    return [json.loads(r[0]) for r in _get_conn().execute(
        "SELECT data_json FROM fixed_rules WHERE account_id = ? ORDER BY ord",
        (account_id,),
    )]


def get_fixed_rule(rule_id: str) -> Optional[Dict[str, Any]]:
    if not rule_id:
        return None
    row = _get_conn().execute(
        "SELECT data_json FROM fixed_rules WHERE id = ?", (rule_id,)
    ).fetchone()
    return json.loads(row[0]) if row else None


def add_fixed_rule(rule: Dict[str, Any]) -> Dict[str, Any]:
    r = dict(rule or {})
    rid = r.get("id") or _new_rule_id()
    r["id"] = rid
    acc_id = r.get("account_id") or ""
    with _write_txn() as conn:
        max_ord_row = conn.execute(
            "SELECT COALESCE(MAX(ord), -1) FROM fixed_rules WHERE account_id = ?",
            (acc_id,),
        ).fetchone()
        next_ord = (max_ord_row[0] if max_ord_row else -1) + 1
        conn.execute(
            "INSERT INTO fixed_rules (id, account_id, ord, data_json) "
            "VALUES (?, ?, ?, ?)",
            (rid, acc_id, next_ord, json.dumps(r, ensure_ascii=False)),
        )
    return r


def update_fixed_rule(rule_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    with _write_txn() as conn:
        row = conn.execute(
            "SELECT data_json FROM fixed_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        if row is None:
            raise ValueError("rule not found")
        r = json.loads(row[0])
        for k, v in (fields or {}).items():
            if v is None:
                continue
            r[k] = v
        conn.execute(
            "UPDATE fixed_rules SET account_id = ?, data_json = ? WHERE id = ?",
            (r.get("account_id") or "", json.dumps(r, ensure_ascii=False), rule_id),
        )
        return r


def delete_fixed_rule(rule_id: str) -> None:
    if not rule_id:
        return
    with _write_txn() as conn:
        conn.execute("DELETE FROM fixed_rules WHERE id = ?", (rule_id,))


def reorder_fixed_rules(account_id: str, ordered_ids: List[str]) -> List[Dict[str, Any]]:
    """Rearrange this account's fixed rules to match `ordered_ids`. Rules
    not listed are appended after, preserving their current relative order.
    Raises ValueError if any id in `ordered_ids` doesn't belong to the
    account."""
    if not account_id:
        return []
    with _write_txn() as conn:
        rows = conn.execute(
            "SELECT id, ord, data_json FROM fixed_rules WHERE account_id = ? "
            "ORDER BY ord",
            (account_id,),
        ).fetchall()
        by_id = {row[0]: row for row in rows}
        unknown = [rid for rid in ordered_ids if rid not in by_id]
        if unknown:
            raise ValueError(f"未知规则 id：{unknown}")
        seen = set(ordered_ids)
        front = [by_id[rid] for rid in ordered_ids]
        back = [row for row in rows if row[0] not in seen]
        result: List[Dict[str, Any]] = []
        for new_ord, row in enumerate(front + back):
            conn.execute(
                "UPDATE fixed_rules SET ord = ? WHERE id = ?",
                (new_ord, row[0]),
            )
            result.append(json.loads(row[2]))
        return result


# ── experiences ─────────────────────────────────────────────────────────

def list_experiences_for_account(account_id: str) -> List[Dict[str, Any]]:
    if not account_id:
        return []
    return [json.loads(r[0]) for r in _get_conn().execute(
        "SELECT data_json FROM experiences WHERE account_id = ? ORDER BY rowid",
        (account_id,),
    )]


def get_experience(experience_id: str) -> Optional[Dict[str, Any]]:
    if not experience_id:
        return None
    row = _get_conn().execute(
        "SELECT data_json FROM experiences WHERE id = ?", (experience_id,)
    ).fetchone()
    return json.loads(row[0]) if row else None


def add_experience(experience: Dict[str, Any]) -> Dict[str, Any]:
    x = dict(experience or {})
    xid = x.get("id") or _new_experience_id()
    x["id"] = xid
    with _write_txn() as conn:
        conn.execute(
            "INSERT INTO experiences (id, account_id, data_json) VALUES (?, ?, ?)",
            (xid, x.get("account_id") or "", json.dumps(x, ensure_ascii=False)),
        )
    return x


def update_experience(experience_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    with _write_txn() as conn:
        row = conn.execute(
            "SELECT data_json FROM experiences WHERE id = ?", (experience_id,)
        ).fetchone()
        if row is None:
            raise ValueError("experience not found")
        x = json.loads(row[0])
        for k, v in (fields or {}).items():
            x[k] = v
        conn.execute(
            "UPDATE experiences SET account_id = ?, data_json = ? WHERE id = ?",
            (x.get("account_id") or "", json.dumps(x, ensure_ascii=False), experience_id),
        )
        return x


def delete_experience(experience_id: str) -> None:
    if not experience_id:
        return
    with _write_txn() as conn:
        conn.execute("DELETE FROM experiences WHERE id = ?", (experience_id,))


# ── per-account field config ────────────────────────────────────────────

def read_field_config_for_account(account_id: str) -> Optional[Dict[str, Any]]:
    if not account_id:
        return None
    row = _get_conn().execute(
        "SELECT data_json FROM field_configs WHERE account_id = ?", (account_id,)
    ).fetchone()
    return json.loads(row[0]) if row else None


def write_field_config_for_account(
    account_id: str, cfg: Dict[str, Any]
) -> None:
    if not account_id:
        return
    with _write_txn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO field_configs (account_id, data_json) "
            "VALUES (?, ?)",
            (account_id, json.dumps(cfg, ensure_ascii=False)),
        )


# ── LLM API key (cipher key lives under data/.llm_secret) ───────────────

def read_llm_api_key() -> str:
    """Decrypt and return the stored API key. Empty string when unconfigured
    (or when decryption fails because the cipher key file is gone)."""
    enc = _get_setting(_get_conn(), "llm_api_key_enc")
    if isinstance(enc, str) and enc:
        try:
            return (
                _get_local_cipher()
                .decrypt(enc.encode("utf-8"))
                .decode("utf-8")
            )
        except Exception:
            return ""
    return ""


def write_llm_api_key(api_key: str) -> None:
    """Encrypt and persist the API key. Passing an empty string clears it."""
    cleaned = (api_key or "").strip()
    with _write_txn() as conn:
        if cleaned:
            token = _get_local_cipher().encrypt(cleaned.encode("utf-8"))
            _set_setting(conn, "llm_api_key_enc", token.decode("utf-8"))
        else:
            _set_setting(conn, "llm_api_key_enc", None)


# ── IMAP sync watermark (per account, per mailbox) ──────────────────────
#
# For each account + IMAP mailbox we remember:
#   uidvalidity   — server-issued tag; if it changes, all UIDs were
#                   renumbered and our watermark is invalid (full re-fetch).
#   last_uid      — highest UID we've successfully ingested.
#   fetch_days_at — the widest `sync.fetch_days` window we've already
#                   covered end-to-end. Used so that when the user widens
#                   the window (e.g. 14 → 60) we drop the UID watermark for
#                   one run — otherwise older mail (UIDs < last_uid) would
#                   be invisible to incremental search.
#
# Stored as a dict on each account's row (kept inside the account's
# `data_json` blob, same shape as the old config.json).

def get_account_sync_state(account_id: str) -> Dict[str, Dict[str, str]]:
    acc = get_account(account_id) or {}
    raw = acc.get("sync_state") or {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for mailbox, entry in raw.items():
        if isinstance(entry, dict):
            out[str(mailbox)] = {
                "uidvalidity": str(entry.get("uidvalidity") or ""),
                "last_uid": str(entry.get("last_uid") or ""),
                "fetch_days_at": str(entry.get("fetch_days_at") or ""),
            }
    return out


def update_account_sync_state_entry(
    account_id: str,
    mailbox: str,
    uidvalidity: str,
    last_uid: str,
    fetch_days_at: str = "",
) -> None:
    with _write_txn() as conn:
        row = conn.execute(
            "SELECT data_json FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if row is None:
            return
        acc = json.loads(row[0])
        ss = dict(acc.get("sync_state") or {})
        prior = ss.get(str(mailbox)) if isinstance(ss.get(str(mailbox)), dict) else {}
        ss[str(mailbox)] = {
            "uidvalidity": str(uidvalidity or ""),
            "last_uid": str(last_uid or ""),
            "fetch_days_at": str(
                fetch_days_at
                or (prior.get("fetch_days_at") if isinstance(prior, dict) else "")
                or ""
            ),
        }
        acc["sync_state"] = ss
        conn.execute(
            "UPDATE accounts SET data_json = ? WHERE id = ?",
            (json.dumps(acc, ensure_ascii=False), account_id),
        )


def clear_account_sync_state(account_id: str) -> None:
    with _write_txn() as conn:
        row = conn.execute(
            "SELECT data_json FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if row is None:
            return
        acc = json.loads(row[0])
        if not acc.get("sync_state"):
            return
        acc["sync_state"] = {}
        conn.execute(
            "UPDATE accounts SET data_json = ? WHERE id = ?",
            (json.dumps(acc, ensure_ascii=False), account_id),
        )


# ── system runtime mode (dev / prod) ────────────────────────────────────

def read_system_mode() -> str:
    val = _get_setting(_get_conn(), "system_mode")
    mode = str(val or "").strip().lower()
    return mode if mode in SYSTEM_MODES else "dev"


def write_system_mode(mode: str) -> str:
    norm = (mode or "").strip().lower()
    if norm not in SYSTEM_MODES:
        raise ValueError(f"unsupported mode: {mode}")
    with _write_txn() as conn:
        _set_setting(conn, "system_mode", norm)
    return norm


# ── desktop settings ────────────────────────────────────────────────────

def read_desktop_settings() -> Dict[str, Any]:
    val = _get_setting(_get_conn(), "desktop_enable_tray")
    return {"enable_tray": bool(val) if val is not None else False}


def write_desktop_settings(*, enable_tray: Optional[bool] = None) -> Dict[str, Any]:
    with _write_txn() as conn:
        if enable_tray is not None:
            _set_setting(conn, "desktop_enable_tray", bool(enable_tray))
        cur = _get_setting(conn, "desktop_enable_tray")
    return {"enable_tray": bool(cur) if cur is not None else False}


# ── session secret (cookie signing key) ─────────────────────────────────

def get_session_secret() -> bytes:
    """Cookie signing key. Auto-generated on first use and persisted under
    data/ so server restarts don't invalidate every active session."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SESSION_SECRET_FILE.exists():
        import secrets
        SESSION_SECRET_FILE.write_bytes(secrets.token_bytes(32))
        try:
            SESSION_SECRET_FILE.chmod(0o600)
        except Exception:
            pass
    return SESSION_SECRET_FILE.read_bytes()


# ── contacts (address book) ─────────────────────────────────────────────
#
# Per-account contact records. Each record is keyed by (account_id,
# lower(email)) — at most one contact per address per account. Tags are a
# free-form list of short strings.

def list_contacts_for_account(account_id: str) -> List[Dict[str, Any]]:
    if not account_id:
        return []
    return [json.loads(r[0]) for r in _get_conn().execute(
        "SELECT data_json FROM contacts WHERE account_id = ? ORDER BY rowid",
        (account_id,),
    )]


def get_contact(contact_id: str) -> Optional[Dict[str, Any]]:
    if not contact_id:
        return None
    row = _get_conn().execute(
        "SELECT data_json FROM contacts WHERE id = ?", (contact_id,)
    ).fetchone()
    return json.loads(row[0]) if row else None


def find_contact_by_email(account_id: str, email: str) -> Optional[Dict[str, Any]]:
    if not account_id or not email:
        return None
    key = email.strip().lower()
    row = _get_conn().execute(
        "SELECT data_json FROM contacts WHERE account_id = ? AND lower(email) = ?",
        (account_id, key),
    ).fetchone()
    return json.loads(row[0]) if row else None


def add_contact(contact: Dict[str, Any]) -> Dict[str, Any]:
    c = dict(contact or {})
    cid = c.get("id") or _new_contact_id()
    c["id"] = cid
    with _write_txn() as conn:
        _insert_contact_row(conn, c)
    return c


def update_contact(contact_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    with _write_txn() as conn:
        row = conn.execute(
            "SELECT data_json FROM contacts WHERE id = ?", (contact_id,)
        ).fetchone()
        if row is None:
            raise ValueError("contact not found")
        c = json.loads(row[0])
        for k, v in (fields or {}).items():
            if v is None:
                continue
            c[k] = v
        conn.execute(
            "UPDATE contacts SET account_id = ?, email = ?, data_json = ? "
            "WHERE id = ?",
            (
                c.get("account_id") or "",
                c.get("email") or "",
                json.dumps(c, ensure_ascii=False),
                contact_id,
            ),
        )
        return c


def delete_contact(contact_id: str) -> None:
    if not contact_id:
        return
    with _write_txn() as conn:
        conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
