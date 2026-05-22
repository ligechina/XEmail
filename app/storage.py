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
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


log = logging.getLogger(__name__)


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = DATA_DIR / "config.json"
EMAILS_FILE = DATA_DIR / "emails.json"
DRAFTS_FILE = DATA_DIR / "drafts.json"
SENT_FILE = DATA_DIR / "sent.json"
FOLDERS_FILE = DATA_DIR / "folders.json"
USERS_FILE = DATA_DIR / "users.json"
PROMPTS_FILE = DATA_DIR / "prompts.json"
CONTACTS_FILE = DATA_DIR / "contacts.json"
SESSION_SECRET_FILE = DATA_DIR / ".session_secret"
LLM_SECRET_FILE = DATA_DIR / ".llm_secret"
ATTACHMENTS_DIR = DATA_DIR / "attachments"

MAX_FILENAME_LEN = 200
UNCLASSIFIED_FOLDER = "未分类"
# Baseline folders the system always ensures exist on every account. Kept
# minimal: only the two buckets the classifier guarantees (垃圾邮件 for
# spam, 未分类 for "no rule matched"). Anything else is user-defined.
DEFAULT_FOLDERS: List[str] = [
    "垃圾邮件",
    UNCLASSIFIED_FOLDER,
]


def _ensure_data_files() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            json.dumps({"active_account_id": None, "accounts": []}, ensure_ascii=False),
            encoding="utf-8",
        )
    for list_file in (EMAILS_FILE, DRAFTS_FILE, SENT_FILE):
        if not list_file.exists():
            list_file.write_text("[]", encoding="utf-8")
    if not FOLDERS_FILE.exists():
        FOLDERS_FILE.write_text("{}", encoding="utf-8")
    if not USERS_FILE.exists():
        USERS_FILE.write_text(
            json.dumps({"users": []}, ensure_ascii=False),
            encoding="utf-8",
        )
    if not PROMPTS_FILE.exists():
        # `system: null` means "use the built-in default from spam_filter".
        PROMPTS_FILE.write_text(
            json.dumps({"system": None, "items": []}, ensure_ascii=False),
            encoding="utf-8",
        )
    if not CONTACTS_FILE.exists():
        CONTACTS_FILE.write_text("[]", encoding="utf-8")
    _migrate_if_needed()


def _new_account_id() -> str:
    return f"acc_{uuid.uuid4().hex[:12]}"


def _atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write `text` to `path` atomically — write to a sibling `.tmp` file, then
    `os.replace` it onto the target. Prevents the spliced-content corruption
    pattern we saw in config.json (first N bytes of a new write leaving the
    tail of the old file behind). os.replace is atomic on POSIX and Windows.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, path)


def _rotate_config_backup() -> None:
    """Best-effort: keep a recent valid backup before overwriting config.json,
    so corruption can be recovered. Bounded to the 5 most recent .bak files.
    """
    try:
        if not CONFIG_FILE.exists():
            return
        raw = CONFIG_FILE.read_text(encoding="utf-8")
        # Only back up files that themselves parse — never archive corruption.
        json.loads(raw)
        bak = CONFIG_FILE.with_suffix(f".json.bak.{int(time.time())}")
        bak.write_text(raw, encoding="utf-8")
        # Prune older backups, keep newest 5.
        baks = sorted(
            DATA_DIR.glob("config.json.bak.*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in baks[5:]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        # Rotation is best-effort — never fail the caller's write because of it.
        pass


def _recover_corrupt_config() -> Optional[str]:
    """Called when config.json fails to parse. Quarantine the bad file with a
    timestamped `.corrupted.*` name and try to restore the newest valid
    `config.json.bak.*`. Returns the recovered raw text, or None if no usable
    backup exists (in which case the caller should treat the config as empty).
    """
    try:
        bad_bytes = CONFIG_FILE.read_text(encoding="utf-8")
    except OSError:
        bad_bytes = ""
    ts = int(time.time())
    quarantine = CONFIG_FILE.with_suffix(f".json.corrupted.{ts}")
    try:
        os.replace(CONFIG_FILE, quarantine)
        log.error(
            "config.json failed to parse; quarantined to %s (%d bytes)",
            quarantine.name,
            len(bad_bytes),
        )
    except OSError as e:
        log.error("config.json parse failed and could not be quarantined: %s", e)

    baks = sorted(
        DATA_DIR.glob("config.json.bak.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for bak in baks:
        try:
            text = bak.read_text(encoding="utf-8")
            json.loads(text)  # validate
        except Exception:  # noqa: BLE001
            continue
        try:
            _atomic_write_text(CONFIG_FILE, text)
            log.warning("Restored config.json from backup %s", bak.name)
            return text
        except OSError as e:
            log.error("Failed to restore from %s: %s", bak.name, e)
            continue

    # No valid backup — create an empty shell so subsequent reads don't
    # keep raising. The user will see "no accounts" and can re-add them.
    empty = json.dumps(
        {"active_account_id": None, "accounts": []}, ensure_ascii=False, indent=2
    )
    try:
        _atomic_write_text(CONFIG_FILE, empty)
        log.error("No valid backup found; wrote empty config.json")
    except OSError as e:
        log.error("Could not write empty config.json: %s", e)
    return empty


def _read_config_raw() -> str:
    """Read config.json with recovery: on JSON parse failure, quarantine the
    bad file and fall back to the most recent valid backup.
    """
    raw = CONFIG_FILE.read_text(encoding="utf-8").strip() or "{}"
    try:
        json.loads(raw)
        return raw
    except json.JSONDecodeError:
        recovered = _recover_corrupt_config()
        return (recovered or "{}").strip() or "{}"


def _migrate_if_needed() -> None:
    """One-shot, idempotent migration from the single-account schema:
      - config.json: {settings, rules, sync}  -> {active_account_id, accounts:[...]}
      - folders.json: ["A", "B", ...]         -> {"<acc_id>": ["A", "B", ...]}
      - emails/drafts/sent records w/o account_id are bound to the migrated id.
    """
    raw = _read_config_raw()
    cfg = json.loads(raw)

    if "accounts" not in cfg:
        legacy_settings = cfg.get("settings")
        if legacy_settings:
            acc_id = _new_account_id()
            new_cfg = {
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
            }
        else:
            new_cfg = {"active_account_id": None, "accounts": []}
            acc_id = None
        _atomic_write_text(
            CONFIG_FILE, json.dumps(new_cfg, ensure_ascii=False, indent=2)
        )
        cfg = new_cfg
    else:
        acc_id = cfg.get("active_account_id")

    # folders: legacy list -> dict keyed by active account
    folders_raw = FOLDERS_FILE.read_text(encoding="utf-8").strip() or "{}"
    folders_data = json.loads(folders_raw)
    if isinstance(folders_data, list):
        wrapped = {acc_id: folders_data} if acc_id else {}
        _atomic_write_text(
            FOLDERS_FILE, json.dumps(wrapped, ensure_ascii=False, indent=2)
        )

    # records: legacy records get account_id stamped onto them
    if acc_id:
        for path in (EMAILS_FILE, DRAFTS_FILE, SENT_FILE):
            content = path.read_text(encoding="utf-8").strip() or "[]"
            items = json.loads(content)
            changed = False
            for item in items:
                if not item.get("account_id"):
                    item["account_id"] = acc_id
                    changed = True
            if changed:
                _atomic_write_text(
                    path, json.dumps(items, ensure_ascii=False, indent=2)
                )


def _read_list(path: Path) -> List[Dict[str, Any]]:
    _ensure_data_files()
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return []
    return json.loads(content)


def _write_list(path: Path, data: List[Dict[str, Any]]) -> None:
    _ensure_data_files()
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def _decrypt_account_settings(settings: Dict[str, Any]) -> bool:
    """In-place: if `sender_password_enc` is present, decrypt it into
    `sender_password` and remove the ciphertext field. Returns True when a
    decryption actually happened (used by the migration detector below to
    distinguish "was already encrypted" from "was plaintext on disk")."""
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
    password is already absent / empty, both fields are cleared."""
    if not isinstance(settings, dict):
        return
    pwd = settings.get("sender_password")
    if isinstance(pwd, str) and pwd:
        token = _get_local_cipher().encrypt(pwd.encode("utf-8"))
        settings["sender_password_enc"] = token.decode("utf-8")
    settings.pop("sender_password", None)


def _purge_legacy_plaintext_backups() -> None:
    """Delete every existing config.json.bak.* — they were rotated off the
    pre-migration cleartext config and still contain the password we're
    trying to remove from disk. Best-effort; never raises."""
    for bak in DATA_DIR.glob("config.json.bak.*"):
        try:
            bak.unlink()
        except OSError:
            pass


def _persist_config(data: Dict[str, Any], *, rotate: bool) -> None:
    """Encrypt every account's `sender_password` and write config.json
    atomically. The caller decides whether to rotate a backup first — the
    migration path passes `rotate=False` so the cleartext we're eliminating
    is not preserved as a .bak side-effect."""
    _ensure_data_files()
    out: Dict[str, Any] = dict(data)
    new_accounts: List[Dict[str, Any]] = []
    for acc in (data.get("accounts") or []):
        acc = dict(acc)
        settings = acc.get("settings")
        if isinstance(settings, dict):
            acc["settings"] = dict(settings)
            _encrypt_account_settings(acc["settings"])
        new_accounts.append(acc)
    out["accounts"] = new_accounts

    if rotate:
        # Rotate a backup of the current valid file before overwriting, so a
        # crash mid-write (or future corruption) has a known-good snapshot to
        # recover from. Best-effort: rotation never blocks the write.
        _rotate_config_backup()
    _atomic_write_text(
        CONFIG_FILE, json.dumps(out, ensure_ascii=False, indent=2)
    )


def read_config() -> Dict[str, Any]:
    """Return the in-memory config shape. Every `sender_password_enc` on disk
    is decrypted into a cleartext `sender_password` so callers see the same
    shape they always have. If any account is still stored with cleartext on
    disk (an upgrade from an older version), this call triggers a one-shot
    rewrite that encrypts everything and purges the plaintext backups."""
    _ensure_data_files()
    raw = _read_config_raw()
    if not raw or raw == "{}":
        return {"active_account_id": None, "accounts": []}
    cfg = json.loads(raw)

    needs_migration = False
    for acc in (cfg.get("accounts") or []):
        settings = acc.get("settings")
        if not isinstance(settings, dict):
            continue
        had_plaintext = bool(
            isinstance(settings.get("sender_password"), str)
            and settings.get("sender_password")
        )
        decrypted = _decrypt_account_settings(settings)
        # If plaintext was present on disk and we did *not* end up overwriting
        # it from a ciphertext field, this account is in the legacy form and
        # the file needs to be rewritten in the encrypted shape.
        if had_plaintext and not decrypted:
            needs_migration = True

    if needs_migration:
        _purge_legacy_plaintext_backups()
        _persist_config(cfg, rotate=False)

    return cfg


def write_config(data: Dict[str, Any]) -> None:
    _persist_config(data, rotate=True)


# -------- accounts --------

def list_accounts() -> List[Dict[str, Any]]:
    return list(read_config().get("accounts") or [])


def get_account(account_id: str) -> Optional[Dict[str, Any]]:
    for acc in list_accounts():
        if acc.get("id") == account_id:
            return acc
    return None


def get_active_account_id() -> Optional[str]:
    return read_config().get("active_account_id")


def get_active_account() -> Optional[Dict[str, Any]]:
    acc_id = get_active_account_id()
    return get_account(acc_id) if acc_id else None


def set_active_account_id(account_id: str) -> None:
    cfg = read_config()
    if not any(a.get("id") == account_id for a in (cfg.get("accounts") or [])):
        raise ValueError("account not found")
    cfg["active_account_id"] = account_id
    write_config(cfg)


def add_account(account: Dict[str, Any]) -> str:
    cfg = read_config()
    accounts = list(cfg.get("accounts") or [])
    acc_id = account.get("id") or _new_account_id()
    account["id"] = acc_id
    accounts.append(account)
    cfg["accounts"] = accounts
    if not cfg.get("active_account_id"):
        cfg["active_account_id"] = acc_id
    write_config(cfg)
    return acc_id


def update_account(account_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    cfg = read_config()
    accounts = list(cfg.get("accounts") or [])
    for acc in accounts:
        if acc.get("id") == account_id:
            for k, v in fields.items():
                if v is not None:
                    acc[k] = v
            cfg["accounts"] = accounts
            write_config(cfg)
            return acc
    raise ValueError("account not found")


def delete_account(account_id: str) -> None:
    """Removes the account from config and purges all of its data
    (emails, drafts, sent, attachments, and folders)."""
    cfg = read_config()
    accounts = [a for a in (cfg.get("accounts") or []) if a.get("id") != account_id]
    cfg["accounts"] = accounts
    if cfg.get("active_account_id") == account_id:
        cfg["active_account_id"] = accounts[0]["id"] if accounts else None
    write_config(cfg)

    # Purge emails/drafts/sent records belonging to this account, plus their
    # attachment folders.
    for path in (EMAILS_FILE, DRAFTS_FILE, SENT_FILE):
        items = _read_list(path)
        kept: List[Dict[str, Any]] = []
        for item in items:
            if item.get("account_id") == account_id:
                rid = item.get("id")
                if rid:
                    try:
                        delete_attachments_folder(rid)
                    except Exception:
                        pass
            else:
                kept.append(item)
        _write_list(path, kept)

    # Drop this account's folder list.
    folders_map = _read_folders_map()
    folders_map.pop(account_id, None)
    _write_folders_map(folders_map)


def read_emails() -> List[Dict[str, Any]]:
    return _read_list(EMAILS_FILE)


def write_emails(data: List[Dict[str, Any]]) -> None:
    _write_list(EMAILS_FILE, data)


def read_drafts() -> List[Dict[str, Any]]:
    return _read_list(DRAFTS_FILE)


def write_drafts(data: List[Dict[str, Any]]) -> None:
    _write_list(DRAFTS_FILE, data)


def read_sent() -> List[Dict[str, Any]]:
    return _read_list(SENT_FILE)


def write_sent(data: List[Dict[str, Any]]) -> None:
    _write_list(SENT_FILE, data)


def import_prompts_for_account(
    account_id: str, data: Dict[str, Any]
) -> Dict[str, int]:
    """Per-account import. Replaces THIS account's prompts / fixed_rules /
    experiences / field_config with whatever's in `data`, leaving other
    accounts and the global system prompt untouched.

    Remapping behaviour: imported items are stripped of their original
    `account_id` and `id`, then re-tagged with `account_id = account_id`
    and fresh ids. This lets you transplant settings from a backup made on
    a different machine without account-id-mismatch surprises.

    `field_configs` (if any in the file) — first non-empty entry is taken
    and written under `field_configs[account_id]`.

    `system` is intentionally ignored: it's a global admin-managed prompt.

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
            copy.pop("id", None)  # caller regenerates
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

    cur = _read_prompts()
    cur["items"] = [
        p for p in (cur.get("items") or []) if p.get("account_id") != account_id
    ]
    cur["fixed_rules"] = [
        r for r in (cur.get("fixed_rules") or []) if r.get("account_id") != account_id
    ]
    cur["experiences"] = [
        x for x in (cur.get("experiences") or []) if x.get("account_id") != account_id
    ]

    for p in new_items:
        p["id"] = _new_prompt_id()
        cur["items"].append(p)
    for r in new_rules:
        r["id"] = _new_rule_id()
        cur["fixed_rules"].append(r)
    for x in new_experiences:
        x["id"] = _new_experience_id()
        cur["experiences"].append(x)

    if new_field_config is not None:
        fc_map = dict(cur.get("field_configs") or {})
        fc_map[account_id] = new_field_config
        cur["field_configs"] = fc_map

    _write_prompts(cur)
    return {
        "items": len(new_items),
        "fixed_rules": len(new_rules),
        "experiences": len(new_experiences),
        "field_configs": 1 if new_field_config is not None else 0,
    }


def import_folders_for_account(
    account_id: str, data: Dict[str, Any]
) -> Dict[str, int]:
    """Per-account folders import. Pulls every folder path from every value
    in `data`, dedupes (case-sensitive), and writes the result under
    `folders[account_id]`. Other accounts' folder lists are left intact.

    Accepts both:
      - `{<some_account_id>: ["A", "B/sub", ...]}` — the normal export shape
      - `["A", "B/sub", ...]` — a bare list, treated as folders for the
        target account directly
    """
    if not account_id:
        raise ValueError("account_id 不能为空")

    collected: List[str] = []
    if isinstance(data, list):
        candidates: List[Any] = list(data)
        for f in candidates:
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

    folders_map = _read_folders_map()
    folders_map[account_id] = deduped
    _write_folders_map(folders_map)
    return {"total_folders": len(deduped)}


def replace_prompts_file(data: Dict[str, Any]) -> Dict[str, int]:
    """Overwrite prompts.json wholesale with `data` after normalising shape.
    Returns a summary `{system, items, fixed_rules, experiences, field_configs}`
    of how many of each were imported, for UI feedback. Used by the admin
    import endpoint — caller is responsible for authorising."""
    if not isinstance(data, dict):
        raise ValueError("prompts.json 顶层必须是一个对象 / dict")

    normalised: Dict[str, Any] = {
        "system": data.get("system") if isinstance(data.get("system"), (str, type(None))) else None,
        "items": list(data.get("items") or []) if isinstance(data.get("items"), list) else [],
        "field_configs": dict(data.get("field_configs") or {})
            if isinstance(data.get("field_configs"), dict) else {},
        "fixed_rules": list(data.get("fixed_rules") or [])
            if isinstance(data.get("fixed_rules"), list) else [],
        "experiences": list(data.get("experiences") or [])
            if isinstance(data.get("experiences"), list) else [],
    }
    _write_prompts(normalised)
    return {
        "system": 1 if normalised["system"] else 0,
        "items": len(normalised["items"]),
        "fixed_rules": len(normalised["fixed_rules"]),
        "experiences": len(normalised["experiences"]),
        "field_configs": len(normalised["field_configs"]),
    }


def replace_folders_file(data: Dict[str, List[str]]) -> Dict[str, int]:
    """Overwrite folders.json wholesale with `data` after light validation.
    Returns `{accounts, total_folders}` for UI feedback."""
    if not isinstance(data, dict):
        raise ValueError("folders.json 顶层必须是一个 {account_id: [folder, ...]} 形式的对象")

    cleaned: Dict[str, List[str]] = {}
    total = 0
    for acc_id, entries in data.items():
        if not isinstance(acc_id, str) or not acc_id:
            continue
        if not isinstance(entries, list):
            raise ValueError(f"账号 {acc_id} 对应的值必须是字符串数组")
        folder_list: List[str] = []
        for f in entries:
            if isinstance(f, str) and f.strip():
                folder_list.append(f.strip())
        cleaned[acc_id] = folder_list
        total += len(folder_list)
    _write_folders_map(cleaned)
    return {"accounts": len(cleaned), "total_folders": total}


def _read_folders_map() -> Dict[str, List[str]]:
    _ensure_data_files()
    content = FOLDERS_FILE.read_text(encoding="utf-8").strip()
    if not content:
        return {}
    data = json.loads(content)
    if isinstance(data, list):  # safety net; migration should have handled this
        return {}
    return data


def _write_folders_map(data: Dict[str, List[str]]) -> None:
    _ensure_data_files()
    FOLDERS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_folders(account_id: Optional[str] = None) -> List[str]:
    """Folders for the given account (active account if omitted). Defaults are
    always merged in so the classifier never lacks the baseline categories."""
    if account_id is None:
        account_id = get_active_account_id() or ""
    folders_map = _read_folders_map()
    items: List[str] = list(folders_map.get(account_id) or [])
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
    folders_map = _read_folders_map()
    folders_map[account_id] = data
    _write_folders_map(folders_map)


# -------- attachment storage --------

def sanitize_filename(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "attachment"
    # Drop directory components and collapse traversal sequences.
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


# -------- users --------

def _new_user_id() -> str:
    return f"u_{uuid.uuid4().hex[:12]}"


def _read_users() -> Dict[str, Any]:
    _ensure_data_files()
    content = USERS_FILE.read_text(encoding="utf-8").strip() or "{}"
    data = json.loads(content)
    if "users" not in data:
        data = {"users": []}
    return data


def _write_users(data: Dict[str, Any]) -> None:
    _ensure_data_files()
    USERS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def list_users() -> List[Dict[str, Any]]:
    return list(_read_users().get("users") or [])


def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    if not user_id:
        return None
    for u in list_users():
        if u.get("id") == user_id:
            return u
    return None


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    target = (username or "").strip().lower()
    if not target:
        return None
    for u in list_users():
        if (u.get("username") or "").strip().lower() == target:
            return u
    return None


def has_any_user() -> bool:
    return bool(list_users())


def add_user(user: Dict[str, Any]) -> str:
    data = _read_users()
    users = list(data.get("users") or [])
    uid = user.get("id") or _new_user_id()
    user["id"] = uid
    users.append(user)
    data["users"] = users
    _write_users(data)
    return uid


def update_user(user_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    data = _read_users()
    users = list(data.get("users") or [])
    for u in users:
        if u.get("id") == user_id:
            for k, v in fields.items():
                u[k] = v
            data["users"] = users
            _write_users(data)
            return u
    raise ValueError("user not found")


def delete_user(user_id: str) -> None:
    """Remove a user and cascade-delete all email accounts they own."""
    data = _read_users()
    users = [u for u in (data.get("users") or []) if u.get("id") != user_id]
    data["users"] = users
    _write_users(data)
    for acc in list_accounts():
        if acc.get("owner_user_id") == user_id:
            try:
                delete_account(acc["id"])
            except Exception:
                continue


def set_user_active_account(user_id: str, account_id: Optional[str]) -> None:
    update_user(user_id, {"active_account_id": account_id})


def get_user_active_account_id(user_id: str) -> Optional[str]:
    u = get_user(user_id)
    if not u:
        return None
    return u.get("active_account_id")


# -------- ownership migration --------

def assign_orphan_accounts_to(user_id: str) -> int:
    """Stamp owner_user_id on every account that has none. Used at first-admin
    setup so legacy accounts created before the user system land under the
    bootstrap admin."""
    cfg = read_config()
    accounts = list(cfg.get("accounts") or [])
    touched = 0
    for acc in accounts:
        if not acc.get("owner_user_id"):
            acc["owner_user_id"] = user_id
            touched += 1
    if touched:
        cfg["accounts"] = accounts
        write_config(cfg)
    return touched


# -------- spam classification prompts --------

def _new_prompt_id() -> str:
    return f"p_{uuid.uuid4().hex[:12]}"


def _read_prompts() -> Dict[str, Any]:
    _ensure_data_files()
    content = PROMPTS_FILE.read_text(encoding="utf-8").strip() or "{}"
    data = json.loads(content)
    if "items" not in data:
        data["items"] = []
    if "system" not in data:
        data["system"] = None
    if "field_configs" not in data:
        data["field_configs"] = {}
    if "fixed_rules" not in data:
        data["fixed_rules"] = []
    if "experiences" not in data:
        data["experiences"] = []
    return data


def _write_prompts(data: Dict[str, Any]) -> None:
    _ensure_data_files()
    PROMPTS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_system_spam_prompt() -> Optional[str]:
    """Admin-edited override for the built-in spam-detection prompt. Returns
    None when admin hasn't customised it yet — callers should fall back to
    the hard-coded default in app.services.spam_filter."""
    return _read_prompts().get("system")


def write_system_spam_prompt(text: Optional[str]) -> None:
    data = _read_prompts()
    data["system"] = text or None
    _write_prompts(data)


def list_prompts_for_account(account_id: str) -> List[Dict[str, Any]]:
    if not account_id:
        return []
    items = _read_prompts().get("items") or []
    return [
        dict(p) for p in items if (p.get("account_id") or "") == account_id
    ]


def get_prompt(prompt_id: str) -> Optional[Dict[str, Any]]:
    if not prompt_id:
        return None
    for p in _read_prompts().get("items") or []:
        if p.get("id") == prompt_id:
            return dict(p)
    return None


def add_prompt(prompt: Dict[str, Any]) -> Dict[str, Any]:
    data = _read_prompts()
    items = list(data.get("items") or [])
    pid = prompt.get("id") or _new_prompt_id()
    prompt["id"] = pid
    items.append(prompt)
    data["items"] = items
    _write_prompts(data)
    return prompt


def update_prompt(prompt_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    data = _read_prompts()
    items = list(data.get("items") or [])
    for p in items:
        if p.get("id") == prompt_id:
            for k, v in fields.items():
                p[k] = v
            data["items"] = items
            _write_prompts(data)
            return dict(p)
    raise ValueError("prompt not found")


def delete_prompt(prompt_id: str) -> None:
    data = _read_prompts()
    items = [p for p in (data.get("items") or []) if p.get("id") != prompt_id]
    data["items"] = items
    _write_prompts(data)


# -------- fixed rules --------

def _new_rule_id() -> str:
    return f"r_{uuid.uuid4().hex[:12]}"


def list_fixed_rules_for_account(account_id: str) -> List[Dict[str, Any]]:
    if not account_id:
        return []
    rules = _read_prompts().get("fixed_rules") or []
    return [dict(r) for r in rules if (r.get("account_id") or "") == account_id]


def get_fixed_rule(rule_id: str) -> Optional[Dict[str, Any]]:
    if not rule_id:
        return None
    for r in _read_prompts().get("fixed_rules") or []:
        if r.get("id") == rule_id:
            return dict(r)
    return None


def add_fixed_rule(rule: Dict[str, Any]) -> Dict[str, Any]:
    data = _read_prompts()
    rules = list(data.get("fixed_rules") or [])
    rid = rule.get("id") or _new_rule_id()
    rule["id"] = rid
    rules.append(rule)
    data["fixed_rules"] = rules
    _write_prompts(data)
    return rule


def update_fixed_rule(rule_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    data = _read_prompts()
    rules = list(data.get("fixed_rules") or [])
    for r in rules:
        if r.get("id") == rule_id:
            for k, v in fields.items():
                if v is None:
                    continue
                r[k] = v
            data["fixed_rules"] = rules
            _write_prompts(data)
            return dict(r)
    raise ValueError("rule not found")


def delete_fixed_rule(rule_id: str) -> None:
    data = _read_prompts()
    rules = [r for r in (data.get("fixed_rules") or []) if r.get("id") != rule_id]
    data["fixed_rules"] = rules
    _write_prompts(data)


def reorder_fixed_rules(account_id: str, ordered_ids: List[str]) -> List[Dict[str, Any]]:
    """Rearrange this account's fixed rules to match `ordered_ids`. Rules not
    listed are appended after, preserving their current relative order.
    Rules belonging to other accounts are left untouched.

    Returns the new ordered list of rules for this account.
    Raises ValueError if any id in `ordered_ids` doesn't belong to the account.
    """
    if not account_id:
        return []
    data = _read_prompts()
    rules = list(data.get("fixed_rules") or [])

    account_rules: List[Dict[str, Any]] = []
    other_rules: List[Dict[str, Any]] = []
    for r in rules:
        if (r.get("account_id") or "") == account_id:
            account_rules.append(r)
        else:
            other_rules.append(r)

    by_id = {r["id"]: r for r in account_rules}
    unknown = [rid for rid in ordered_ids if rid not in by_id]
    if unknown:
        raise ValueError(f"未知规则 id：{unknown}")

    seen = set(ordered_ids)
    front = [by_id[rid] for rid in ordered_ids]
    back = [r for r in account_rules if r["id"] not in seen]

    new_account_rules = front + back
    # Splice back in the order: keep other accounts where they were before
    # the account_rules block. Simplest correct approach: account_rules come
    # first then others — the relative position between accounts doesn't
    # affect anything since reads filter by account_id.
    data["fixed_rules"] = new_account_rules + other_rules
    _write_prompts(data)
    return new_account_rules


# -------- experiences --------
#
# An "experience" is a short, free-form lesson the user (or the LLM, on the
# user's behalf) wants the classifier to remember. It's structurally
# identical to a prompt without a target_folder — fed to the LLM as
# additional general guidance during classification. Created mostly via
# the「标为重要 / 取消重要」dialog, where the user explains *why* and the
# LLM distills it into a one-liner.

def _new_experience_id() -> str:
    return f"x_{uuid.uuid4().hex[:12]}"


def list_experiences_for_account(account_id: str) -> List[Dict[str, Any]]:
    if not account_id:
        return []
    items = _read_prompts().get("experiences") or []
    return [
        dict(x) for x in items if (x.get("account_id") or "") == account_id
    ]


def get_experience(experience_id: str) -> Optional[Dict[str, Any]]:
    if not experience_id:
        return None
    for x in _read_prompts().get("experiences") or []:
        if x.get("id") == experience_id:
            return dict(x)
    return None


def add_experience(experience: Dict[str, Any]) -> Dict[str, Any]:
    data = _read_prompts()
    items = list(data.get("experiences") or [])
    xid = experience.get("id") or _new_experience_id()
    experience["id"] = xid
    items.append(experience)
    data["experiences"] = items
    _write_prompts(data)
    return experience


def update_experience(experience_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    data = _read_prompts()
    items = list(data.get("experiences") or [])
    for x in items:
        if x.get("id") == experience_id:
            for k, v in fields.items():
                x[k] = v
            data["experiences"] = items
            _write_prompts(data)
            return dict(x)
    raise ValueError("experience not found")


def delete_experience(experience_id: str) -> None:
    data = _read_prompts()
    items = [
        x for x in (data.get("experiences") or []) if x.get("id") != experience_id
    ]
    data["experiences"] = items
    _write_prompts(data)


def read_field_config_for_account(account_id: str) -> Optional[Dict[str, Any]]:
    if not account_id:
        return None
    cfgs = _read_prompts().get("field_configs") or {}
    cfg = cfgs.get(account_id)
    return dict(cfg) if cfg else None


def write_field_config_for_account(
    account_id: str, cfg: Dict[str, Any]
) -> None:
    if not account_id:
        return
    data = _read_prompts()
    cfgs = dict(data.get("field_configs") or {})
    cfgs[account_id] = cfg
    data["field_configs"] = cfgs
    _write_prompts(data)


# -------- Local secrets: encryption-at-rest --------
#
# Two pieces of config.json must never live on disk in cleartext:
#   - each account's SMTP/IMAP `sender_password` (mail server auth code)
#   - the LLM API key
# Both are encrypted with Fernet (AES-128-CBC + HMAC-SHA256). The symmetric
# key lives in data/.llm_secret (mode 0o600), separate from config.json so
# that leaking config.json alone (backups, accidental shares, screenshots)
# does not disclose the secrets. The cleartext is only ever materialised
# in-memory and is never written to logs.


def _get_local_cipher():
    """Lazy-load the Fernet cipher backed by data/.llm_secret. The secret file
    is generated on first use and locked down to owner-only (0o600).

    Despite the legacy filename, this cipher protects every at-rest secret
    in this codebase — both the LLM API key and account SMTP/IMAP passwords.
    The filename is retained so existing installs don't lose their key after
    upgrade.
    """
    from cryptography.fernet import Fernet  # local import: not all callers need it

    _ensure_data_files()
    if not LLM_SECRET_FILE.exists():
        LLM_SECRET_FILE.write_bytes(Fernet.generate_key())
        try:
            LLM_SECRET_FILE.chmod(0o600)
        except Exception:
            pass
    return Fernet(LLM_SECRET_FILE.read_bytes())


def read_llm_api_key() -> str:
    """Decrypt and return the stored DeepSeek API key. Empty string when
    unconfigured (or when decryption fails because the secret file is gone)."""
    cfg = read_config()
    llm = cfg.get("llm") or {}

    enc = str(llm.get("api_key_enc") or "").strip()
    if enc:
        try:
            return (
                _get_local_cipher()
                .decrypt(enc.encode("utf-8"))
                .decode("utf-8")
            )
        except Exception:
            # Secret file rotated/corrupted, or token is malformed — treat as
            # unconfigured rather than crashing the request pipeline.
            return ""

    # One-shot migration: any earlier plaintext field gets re-encrypted and
    # the cleartext removed from config.json on the spot.
    legacy = str(llm.get("api_key") or "").strip()
    if legacy:
        write_llm_api_key(legacy)
        return legacy
    return ""


def write_llm_api_key(api_key: str) -> None:
    """Encrypt and persist the API key. Passing an empty string clears it.
    Always strips any legacy plaintext field as a side-effect."""
    cfg = read_config()
    llm = dict(cfg.get("llm") or {})
    cleaned = (api_key or "").strip()
    if cleaned:
        token = _get_local_cipher().encrypt(cleaned.encode("utf-8"))
        llm["api_key_enc"] = token.decode("utf-8")
    else:
        llm.pop("api_key_enc", None)
    # Never leave legacy plaintext lying around.
    llm.pop("api_key", None)
    cfg["llm"] = llm
    write_config(cfg)


# -------- IMAP sync watermark --------
#
# For each account + IMAP mailbox we remember:
#   uidvalidity   — server-issued tag; if it changes, all UIDs were renumbered
#                   and our watermark is invalid (full re-fetch required).
#   last_uid      — highest UID we've successfully ingested.
#   fetch_days_at — the widest `sync.fetch_days` window we've already covered
#                   end-to-end. Used so that when the user widens the window
#                   (e.g. 14 → 60) we can detect it and drop the UID watermark
#                   for one run — otherwise the older mail (which has UIDs
#                   LESS than `last_uid`) would be invisible to incremental
#                   search.
#
# Receive logic uses this to do `UID SEARCH UID <last+1>:*` instead of
# `UID SEARCH ALL`, so re-clicking 收取邮件 only pulls new mail.


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
    cfg = read_config()
    accounts = list(cfg.get("accounts") or [])
    for acc in accounts:
        if acc.get("id") == account_id:
            ss = dict(acc.get("sync_state") or {})
            prior = ss.get(str(mailbox)) if isinstance(ss.get(str(mailbox)), dict) else {}
            ss[str(mailbox)] = {
                "uidvalidity": str(uidvalidity or ""),
                "last_uid": str(last_uid or ""),
                # Keep the widest window we've already covered. Callers compute
                # max(prior, current) and pass that in; this fallback also
                # preserves a prior value when callers omit it (legacy).
                "fetch_days_at": str(
                    fetch_days_at
                    or (prior.get("fetch_days_at") if isinstance(prior, dict) else "")
                    or ""
                ),
            }
            acc["sync_state"] = ss
            cfg["accounts"] = accounts
            write_config(cfg)
            return


def clear_account_sync_state(account_id: str) -> None:
    cfg = read_config()
    accounts = list(cfg.get("accounts") or [])
    changed = False
    for acc in accounts:
        if acc.get("id") == account_id and acc.get("sync_state"):
            acc["sync_state"] = {}
            changed = True
    if changed:
        cfg["accounts"] = accounts
        write_config(cfg)


# -------- system runtime mode --------
#
# Two modes:
#   "dev"  — debug UI (重新分类、复位 等) is visible on the main page.
#   "prod" — debug UI hidden; the app behaves like a finished product.
# Default is "dev" so an out-of-the-box install still exposes the debug tools.

SYSTEM_MODES = ("dev", "prod")


def read_system_mode() -> str:
    cfg = read_config()
    mode = str((cfg.get("system") or {}).get("mode") or "").strip().lower()
    return mode if mode in SYSTEM_MODES else "dev"


def write_system_mode(mode: str) -> str:
    norm = (mode or "").strip().lower()
    if norm not in SYSTEM_MODES:
        raise ValueError(f"unsupported mode: {mode}")
    cfg = read_config()
    system = dict(cfg.get("system") or {})
    system["mode"] = norm
    cfg["system"] = system
    write_config(cfg)
    return norm


# -------- session secret --------

def get_session_secret() -> bytes:
    """Cookie signing key. Auto-generated on first use and persisted under data/
    so server restarts don't invalidate every active session."""
    _ensure_data_files()
    if not SESSION_SECRET_FILE.exists():
        import secrets
        SESSION_SECRET_FILE.write_bytes(secrets.token_bytes(32))
        try:
            SESSION_SECRET_FILE.chmod(0o600)
        except Exception:
            pass
    return SESSION_SECRET_FILE.read_bytes()


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


# -------- contacts (address book) --------
#
# Per-account contact records stored in data/contacts.json as a flat list.
# Each record is keyed by (account_id, lower(email)) — there's at most one
# contact per address per account. Tags are a free-form list of short
# strings; the page derives the tag-filter sidebar from the set of distinct
# tags across the active account's contacts.

def _new_contact_id() -> str:
    return f"ct_{uuid.uuid4().hex[:12]}"


def _read_contacts() -> List[Dict[str, Any]]:
    if not CONTACTS_FILE.exists():
        return []
    raw = CONTACTS_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return json.loads(raw)


def _write_contacts(items: List[Dict[str, Any]]) -> None:
    CONTACTS_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def list_contacts_for_account(account_id: str) -> List[Dict[str, Any]]:
    if not account_id:
        return []
    return [
        dict(c) for c in _read_contacts() if (c.get("account_id") or "") == account_id
    ]


def get_contact(contact_id: str) -> Optional[Dict[str, Any]]:
    if not contact_id:
        return None
    for c in _read_contacts():
        if c.get("id") == contact_id:
            return dict(c)
    return None


def find_contact_by_email(account_id: str, email: str) -> Optional[Dict[str, Any]]:
    if not account_id or not email:
        return None
    key = email.strip().lower()
    for c in _read_contacts():
        if (c.get("account_id") or "") != account_id:
            continue
        if (c.get("email") or "").strip().lower() == key:
            return dict(c)
    return None


def add_contact(contact: Dict[str, Any]) -> Dict[str, Any]:
    items = _read_contacts()
    cid = contact.get("id") or _new_contact_id()
    contact["id"] = cid
    items.append(contact)
    _write_contacts(items)
    return contact


def update_contact(contact_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    items = _read_contacts()
    for c in items:
        if c.get("id") == contact_id:
            for k, v in fields.items():
                if v is None:
                    continue
                c[k] = v
            _write_contacts(items)
            return dict(c)
    raise ValueError("contact not found")


def delete_contact(contact_id: str) -> None:
    items = [c for c in _read_contacts() if c.get("id") != contact_id]
    _write_contacts(items)
