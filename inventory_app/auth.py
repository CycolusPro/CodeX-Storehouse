"""User management utilities for inventory app authentication."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Dict, List, Optional

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


@dataclass
class LoginRecord:
    """Represents a single login attempt that succeeded."""

    username: str
    timestamp: datetime
    ip_address: str
    user_agent: str
    client_type: str
    platform: str
    browser: str
    event_type: str
    path: str
    method: str
    referrer: str

    def to_record(self) -> Dict[str, str]:
        return {
            "username": self.username,
            "timestamp": _serialize_timestamp(self.timestamp),
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "client_type": self.client_type,
            "platform": self.platform,
            "browser": self.browser,
            "event_type": self.event_type,
            "path": self.path,
            "method": self.method,
            "referrer": self.referrer,
        }

    @classmethod
    def from_record(cls, record: Dict[str, str]) -> "LoginRecord":
        timestamp = _parse_timestamp(record.get("timestamp")) or _now()
        event_type = str(record.get("event_type") or "login").strip() or "login"
        normalized_event_type = (
            event_type if event_type in {"login", "access", "activity"} else "login"
        )
        return cls(
            username=str(record.get("username") or ""),
            timestamp=timestamp,
            ip_address=str(record.get("ip_address") or ""),
            user_agent=str(record.get("user_agent") or ""),
            client_type=str(record.get("client_type") or "未知"),
            platform=str(record.get("platform") or ""),
            browser=str(record.get("browser") or ""),
            event_type=normalized_event_type,
            path=str(record.get("path") or ""),
            method=str(record.get("method") or ""),
            referrer=str(record.get("referrer") or ""),
        )


class UserManager:
    """Manages user records stored in a JSON document."""

    _MAX_LOGIN_RECORDS = 1000

    def __init__(self, storage_path: Path, *, login_log_path: Optional[Path] = None) -> None:
        self.storage_path = Path(storage_path)
        self.login_log_path = (
            Path(login_log_path)
            if login_log_path is not None
            else self.storage_path.with_name("login_logs.json")
        )
        self._lock = RLock()
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.login_log_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.storage_path.exists():
            self._write_data({})
        if not self.login_log_path.exists():
            self._write_login_data([])
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

    # login records -----------------------------------------------------
    def list_login_records(self, limit: Optional[int] = None) -> List[LoginRecord]:
        records_raw = self._read_login_data()
        records = [LoginRecord.from_record(entry) for entry in records_raw]
        records.sort(key=lambda entry: entry.timestamp, reverse=True)
        if limit is not None:
            return records[:limit]
        return records

    def record_login(
        self,
        username: str,
        *,
        ip_address: str = "",
        user_agent: str = "",
        client_type: Optional[str] = None,
        platform: Optional[str] = None,
        browser: Optional[str] = None,
        event_type: str = "login",
        path: Optional[str] = None,
        method: Optional[str] = None,
        referrer: Optional[str] = None,
    ) -> LoginRecord:
        client_meta = self._analyze_user_agent(user_agent)
        normalized_event_type = (
            event_type if event_type in {"login", "access", "activity"} else "login"
        )
        record = LoginRecord(
            username=username,
            timestamp=_now(),
            ip_address=ip_address,
            user_agent=user_agent,
            client_type=client_type or client_meta["client_type"],
            platform=platform or client_meta["platform"],
            browser=browser or client_meta["browser"],
            event_type=normalized_event_type,
            path=(path or ""),
            method=(method or ""),
            referrer=(referrer or ""),
        )
        with self._lock:
            existing = self._read_login_data()
            existing.append(record.to_record())
            if len(existing) > self._MAX_LOGIN_RECORDS:
                existing = existing[-self._MAX_LOGIN_RECORDS :]
            self._write_login_data(existing)
        return record

    def clear_login_records(self) -> None:
        with self._lock:
            self._write_login_data([])

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

    def _read_login_data(self) -> List[Dict[str, str]]:
        if not self.login_log_path.exists():
            return []
        raw = self.login_log_path.read_text(encoding="utf-8") or "[]"
        try:
            import json

            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(loaded, list):
            return []
        return [entry for entry in loaded if isinstance(entry, dict)]

    def _write_login_data(self, entries: List[Dict[str, str]]) -> None:
        import json

        temp_path = self.login_log_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp_path.replace(self.login_log_path)

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

    @staticmethod
    def _analyze_user_agent(user_agent: str) -> Dict[str, str]:
        ua_lower = user_agent.lower()
        client_type = "未知"
        if ua_lower:
            tablet_indicators = ["tablet", "ipad"]
            mobile_indicators = ["iphone", "android", "mobile", "micromessenger"]
            if any(indicator in ua_lower for indicator in tablet_indicators):
                client_type = "平板端"
            elif any(indicator in ua_lower for indicator in mobile_indicators):
                client_type = "移动端"
            else:
                client_type = "桌面端"

        platform = ""
        platform_mappings = {
            "windows nt": "Windows",
            "mac os x": "macOS",
            "iphone": "iOS",
            "ipad": "iPadOS",
            "android": "Android",
            "linux": "Linux",
        }
        for signature, label in platform_mappings.items():
            if signature in ua_lower:
                platform = label
                break

        browser = ""
        browser_mappings = {
            "edg": "Microsoft Edge",
            "chrome": "Chrome",
            "safari": "Safari",
            "firefox": "Firefox",
            "msie": "Internet Explorer",
            "trident": "Internet Explorer",
        }
        for signature, label in browser_mappings.items():
            if signature in ua_lower:
                browser = label
                break

        return {
            "client_type": client_type,
            "platform": platform,
            "browser": browser,
        }
