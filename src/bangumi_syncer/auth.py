"""Web login for the LAN UI.

Accounts are kept in the state database (``meta.web_users``) as PBKDF2 hashes.
Sessions are stateless signed cookies, so a restart keeps people logged in
while deleting a user immediately invalidates their cookie.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
import urllib.parse
from typing import Any

from .state import StateStore

USERS_KEY = "web_users"
SECRET_KEY = "auth_secret"
COOKIE_NAME = "bs_session"
SESSION_TTL = 7 * 24 * 3600
ITERATIONS = 200_000
MIN_PASSWORD = 6
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
ROLES = ("admin", "member")


class AuthError(Exception):
    """User-facing authentication problem (bad password, duplicate name, ...)."""


def _hash(password: str, salt: str, iterations: int = ITERATIONS) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), iterations)
    return digest.hex()


class AuthManager:
    def __init__(self, store: StateStore) -> None:
        self.store = store
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ users
    def _users(self) -> dict[str, dict[str, Any]]:
        data = self.store.get_json(USERS_KEY)
        return data if isinstance(data, dict) else {}

    def _save(self, users: dict[str, dict[str, Any]]) -> None:
        self.store.set_json(USERS_KEY, users)

    def setup_required(self) -> bool:
        return not self._users()

    def count(self) -> int:
        return len(self._users())

    def list_users(self) -> list[dict[str, Any]]:
        users = self._users()
        return [
            {
                "username": name,
                "role": data.get("role") or "member",
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
            }
            for name, data in sorted(users.items())
        ]

    def get_user(self, username: str) -> dict[str, Any] | None:
        data = self._users().get(username)
        return dict(data) if isinstance(data, dict) else None

    def _validate_username(self, username: str) -> str:
        name = (username or "").strip()
        if not USERNAME_RE.match(name):
            raise AuthError("用户名需为 3–32 位字母、数字、_ . -")
        return name

    def _validate_password(self, password: str) -> str:
        value = password or ""
        if len(value) < MIN_PASSWORD:
            raise AuthError("密码至少 %d 位" % MIN_PASSWORD)
        if len(value) > 128:
            raise AuthError("密码过长")
        return value

    def verify(self, username: str, password: str) -> bool:
        user = self.get_user(username)
        if not user:
            # keep the timing roughly constant for unknown users
            _hash(password or "", "unknown", ITERATIONS)
            return False
        digest = _hash(password or "", str(user.get("salt") or ""), int(user.get("iterations") or ITERATIONS))
        return hmac.compare_digest(digest, str(user.get("hash") or ""))

    def create_user(self, username: str, password: str, role: str = "member") -> dict[str, Any]:
        name = self._validate_username(username)
        secret = self._validate_password(password)
        role = role if role in ROLES else "member"
        with self._lock:
            users = self._users()
            if name in users:
                raise AuthError("用户名已存在")
            if not users:
                role = "admin"  # the first account is always an administrator
            salt = secrets.token_hex(16)
            now = int(time.time())
            users[name] = {
                "salt": salt,
                "hash": _hash(secret, salt),
                "iterations": ITERATIONS,
                "role": role,
                "created_at": now,
                "updated_at": now,
            }
            self._save(users)
        return {"username": name, "role": role}

    def setup(self, username: str, password: str) -> dict[str, Any]:
        """Create the very first administrator (first-run page)."""
        with self._lock:
            if self._users():
                raise AuthError("已经完成初始化")
        return self.create_user(username, password, "admin")

    def change_password(self, username: str, current: str, new: str) -> None:
        value = self._validate_password(new)
        with self._lock:
            users = self._users()
            user = users.get(username)
            if not user:
                raise AuthError("用户不存在")
            digest = _hash(current or "", str(user.get("salt") or ""), int(user.get("iterations") or ITERATIONS))
            if not hmac.compare_digest(digest, str(user.get("hash") or "")):
                raise AuthError("当前密码不正确")
            salt = secrets.token_hex(16)
            user["salt"] = salt
            user["hash"] = _hash(value, salt)
            user["iterations"] = ITERATIONS
            user["updated_at"] = int(time.time())
            self._save(users)

    def reset_password(self, username: str, new: str) -> None:
        value = self._validate_password(new)
        with self._lock:
            users = self._users()
            user = users.get(username)
            if not user:
                raise AuthError("用户不存在")
            salt = secrets.token_hex(16)
            user["salt"] = salt
            user["hash"] = _hash(value, salt)
            user["iterations"] = ITERATIONS
            user["updated_at"] = int(time.time())
            self._save(users)

    def set_role(self, username: str, role: str) -> None:
        if role not in ROLES:
            raise AuthError("未知角色")
        with self._lock:
            users = self._users()
            user = users.get(username)
            if not user:
                raise AuthError("用户不存在")
            if user.get("role") == "admin" and role != "admin" and self._admin_count(users) <= 1:
                raise AuthError("至少保留一个管理员")
            user["role"] = role
            user["updated_at"] = int(time.time())
            self._save(users)

    def delete_user(self, username: str, actor: str) -> None:
        with self._lock:
            users = self._users()
            if username not in users:
                raise AuthError("用户不存在")
            if username == actor:
                raise AuthError("不能删除当前登录的账号")
            if users[username].get("role") == "admin" and self._admin_count(users) <= 1:
                raise AuthError("至少保留一个管理员")
            del users[username]
            self._save(users)

    @staticmethod
    def _admin_count(users: dict[str, dict[str, Any]]) -> int:
        return sum(1 for data in users.values() if data.get("role") == "admin")

    # --------------------------------------------------------------- sessions
    def _secret(self) -> str:
        value = self.store.meta(SECRET_KEY)
        if not value:
            value = secrets.token_hex(32)
            self.store.set_meta(SECRET_KEY, value)
        return value

    def _sign(self, payload: str) -> str:
        return hmac.new(self._secret().encode("ascii"), payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def issue_cookie(self, username: str) -> str:
        expires = int(time.time()) + SESSION_TTL
        payload = "%s|%d" % (username, expires)
        value = urllib.parse.quote(payload + "|" + self._sign(payload), safe="")
        return "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (COOKIE_NAME, value, SESSION_TTL)

    def clear_cookie(self) -> str:
        return "%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0" % COOKIE_NAME

    def user_from_cookie(self, header: str | None) -> dict[str, Any] | None:
        raw = ""
        for part in (header or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME:
                raw = value
                break
        if not raw:
            return None
        try:
            decoded = urllib.parse.unquote(raw)
            username, expires_raw, signature = decoded.rsplit("|", 2)
            expires = int(expires_raw)
        except (ValueError, TypeError):
            return None
        if expires < time.time():
            return None
        payload = "%s|%d" % (username, expires)
        if not hmac.compare_digest(self._sign(payload), signature):
            return None
        user = self.get_user(username)
        if not user:
            return None
        return {"username": username, "role": user.get("role") or "member"}
