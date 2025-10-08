"""User management utilities for inventory app authentication."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Dict, Optional

from werkzeug.security import check_password_hash, generate_password_hash


_VALID_ROLES = {"super_admin", "admin", "staff"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass
class User:
    """Representation of an authenticated user."""

    username: str
    password_hash: str
    role: str
    created_at: datetime
    updated_at: datetime

    def to_record(self) -> Dict[str, str]:
        return {
            "username": self.username,
            "password_hash": self.password_hash,
            "role": self.role,
            "created_at": _serialize_timestamp(self.created_at),
            "updated_at": _serialize_timestamp(self.updated_at),
        }

    def to_public_dict(self) -> Dict[str, str]:
        return {
            "username": self.username,
            "role": self.role,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_record(cls, record: Dict[str, str]) -> "User":
        created_at = _parse_timestamp(record.get("created_at")) or _now()
        updated_at = _parse_timestamp(record.get("updated_at")) or created_at
        username = str(record.get("username") or "").strip()
        role = str(record.get("role") or "staff").strip()
        password_hash = str(record.get("password_hash") or "")
        if username == "":
            raise ValueError("User record missing username")
        if role not in _VALID_ROLES:
            role = "staff"
        return cls(
            username=username,
            password_hash=password_hash,
            role=role,
            created_at=created_at,
            updated_at=updated_at,
        )


class UserManager:
    """Manages user records stored in a JSON document."""

    def __init__(self, storage_path: Path) -> None:
        self.storage_path = Path(storage_path)
        self._lock = RLock()
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.storage_path.exists():
            self._write_data({})
        self._ensure_super_admin()

    # public API ---------------------------------------------------------
    def list_users(self) -> Dict[str, User]:
        data = self._read_data()
        users: Dict[str, User] = {}
        for username, record in data.items():
            try:
                users[username] = User.from_record(record)
            except ValueError:
                continue
        return users

    def get_user(self, username: str) -> User:
        users = self.list_users()
        if username not in users:
            raise KeyError(f"User '{username}' not found")
        return users[username]

    def authenticate(self, username: str, password: str) -> Optional[User]:
        try:
            user = self.get_user(username)
        except KeyError:
            return None
        if user.password_hash and check_password_hash(user.password_hash, password):
            return user
        return None

    def create_user(self, username: str, password: str, role: str) -> User:
        username = username.strip()
        self._validate_username(username)
        self._validate_role(role)
        if not password:
            raise ValueError("密码不能为空")
        with self._lock:
            data = self._read_data()
            if username in data:
                raise ValueError("用户名已存在")
            now = _now()
            record = {
                "username": username,
                "password_hash": generate_password_hash(password),
                "role": role,
                "created_at": _serialize_timestamp(now),
                "updated_at": _serialize_timestamp(now),
            }
            data[username] = record
            self._write_data(data)
        return User.from_record(record)

    def update_user(
        self,
        original_username: str,
        *,
        new_username: Optional[str] = None,
        new_password: Optional[str] = None,
        new_role: Optional[str] = None,
    ) -> User:
        with self._lock:
            data = self._read_data()
            if original_username not in data:
                raise KeyError(f"User '{original_username}' not found")
            record = data[original_username]
            username = original_username
            if new_username is not None:
                candidate = new_username.strip()
                self._validate_username(candidate)
                if candidate != original_username and candidate in data:
                    raise ValueError("用户名已存在")
                username = candidate
            role = record.get("role", "staff")
            if new_role is not None:
                self._validate_role(new_role)
                role = new_role
            password_hash = record.get("password_hash", "")
            if new_password:
                password_hash = generate_password_hash(new_password)
            now = _now()
            updated_record = {
                "username": username,
                "password_hash": password_hash,
                "role": role,
                "created_at": record.get("created_at") or _serialize_timestamp(now),
                "updated_at": _serialize_timestamp(now),
            }
            if username != original_username:
                del data[original_username]
            data[username] = updated_record
            self._enforce_super_admin_presence(data, username=username, role=role)
            self._write_data(data)
        return User.from_record(updated_record)

    def delete_user(self, username: str) -> None:
        with self._lock:
            data = self._read_data()
            if username not in data:
                raise KeyError(f"User '{username}' not found")
            del data[username]
            self._enforce_super_admin_presence(data)
            self._write_data(data)

    # helpers ------------------------------------------------------------
    def _read_data(self) -> Dict[str, Dict[str, str]]:
        if not self.storage_path.exists():
            return {}
        raw = self.storage_path.read_text(encoding="utf-8") or "{}"
        try:
            import json

            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(loaded, dict):
            return {}
        return loaded

    def _write_data(self, data: Dict[str, Dict[str, str]]) -> None:
        import json

        temp_path = self.storage_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp_path.replace(self.storage_path)

    def _ensure_super_admin(self) -> None:
        with self._lock:
            data = self._read_data()
            has_super_admin = any(
                isinstance(record, dict) and record.get("role") == "super_admin"
                for record in data.values()
            )
            if not has_super_admin:
                now = _serialize_timestamp(_now())
                data["admin"] = {
                    "username": "admin",
                    "password_hash": generate_password_hash("admin"),
                    "role": "super_admin",
                    "created_at": now,
                    "updated_at": now,
                }
            else:
                admin_record = data.get("admin")
                if isinstance(admin_record, dict):
                    admin_record.setdefault("role", "super_admin")
                    if not admin_record.get("password_hash"):
                        admin_record["password_hash"] = generate_password_hash("admin")
            self._write_data(data)

    def _enforce_super_admin_presence(
        self,
        data: Dict[str, Dict[str, str]],
        *,
        username: Optional[str] = None,
        role: Optional[str] = None,
    ) -> None:
        has_super_admin = False
        for key, record in data.items():
            if not isinstance(record, dict):
                continue
            if record.get("role") == "super_admin":
                has_super_admin = True
                break
        if not has_super_admin:
            raise ValueError("至少需要保留一位超级管理员账号")
        if role != "super_admin":
            return
        if username is None:
            return
        # ensure updated record flagged as super admin exists
        record = data.get(username)
        if not isinstance(record, dict) or record.get("role") != "super_admin":
            raise ValueError("至少需要保留一位超级管理员账号")

    @staticmethod
    def _validate_username(username: str) -> None:
        if not username:
            raise ValueError("用户名不能为空")
        if len(username) < 3:
            raise ValueError("用户名至少需要 3 个字符")

    @staticmethod
    def _validate_role(role: str) -> None:
        if role not in _VALID_ROLES:
            raise ValueError("无效的角色类型")
