# Copyright (c) 2026 Peking University & Beijing Siliconheart Technology Co., Ltd.
# XEmail is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#          http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Authentication primitives: password hashing + signed session cookie.

The app is intended to be self-hosted, so we keep dependencies minimal and
stick to stdlib (pbkdf2 for password hashing, hmac for cookie signing).
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Dict, Optional

from fastapi import Cookie, HTTPException, status

from app.models import User
from app.storage import get_session_secret, get_user

SESSION_COOKIE_NAME = "xemail_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days

_PBKDF2_ITERS = 200_000
_PBKDF2_ALGO = "sha256"


# -------- password hashing --------

def hash_password(plain: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        _PBKDF2_ALGO, plain.encode("utf-8"), salt, _PBKDF2_ITERS
    )
    return f"pbkdf2_{_PBKDF2_ALGO}${_PBKDF2_ITERS}${salt.hex()}${digest.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    try:
        scheme, iters_str, salt_hex, digest_hex = stored.split("$", 3)
        if not scheme.startswith("pbkdf2_"):
            return False
        algo = scheme.split("_", 1)[1]
        iters = int(iters_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    candidate = hashlib.pbkdf2_hmac(algo, plain.encode("utf-8"), salt, iters)
    return hmac.compare_digest(candidate, expected)


# -------- session cookie --------

def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def make_session_token(user_id: str) -> str:
    payload = {"uid": user_id, "exp": int(time.time()) + SESSION_TTL_SECONDS}
    body = _b64u_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(get_session_secret(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64u_encode(sig)}"


def read_session_token(token: str) -> Optional[str]:
    if not token or "." not in token:
        return None
    try:
        body, sig_b64 = token.rsplit(".", 1)
        expected_sig = hmac.new(
            get_session_secret(), body.encode("ascii"), hashlib.sha256
        ).digest()
        provided_sig = _b64u_decode(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected_sig, provided_sig):
        return None
    try:
        payload: Dict = json.loads(_b64u_decode(body))
    except Exception:
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    uid = payload.get("uid")
    return uid if isinstance(uid, str) and uid else None


# -------- FastAPI dependencies --------

def _user_dict_to_model(u: Dict) -> User:
    return User(
        id=u["id"],
        username=u["username"],
        role=u.get("role", "normal"),
        active_account_id=u.get("active_account_id"),
        created_at=u.get("created_at", ""),
    )


def optional_user(
    xemail_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> Optional[User]:
    if not xemail_session:
        return None
    uid = read_session_token(xemail_session)
    if not uid:
        return None
    u = get_user(uid)
    if not u:
        return None
    return _user_dict_to_model(u)


def current_user(
    xemail_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> User:
    user = optional_user(xemail_session)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录或会话已过期。"
        )
    return user


def require_admin(
    xemail_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> User:
    user = current_user(xemail_session)
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限。")
    return user
