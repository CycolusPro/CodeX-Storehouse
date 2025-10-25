"""Flask application providing a minimal inventory management API and UI."""
from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Mapping, Tuple
from functools import wraps
from urllib.parse import urlsplit, urljoin
import os
import time

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
import xlrd
import xlwt
from itsdangerous import BadData, URLSafeSerializer

from .inventory import (
    InventoryHistoryEntry,
    InventoryItem,
    InventoryManager,
)
from .auth import UserManager


def _normalize_csv_key(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    if "\ufeff" in text:
        text = text.replace("\ufeff", "")
    return text


_CSV_FIELD_ALIASES: Dict[str, set[str]] = {
    "name": {"name", "名称"},
    "quantity": {"quantity", "数量"},
    "unit": {"unit", "单位"},
    "threshold": {"threshold", "阈值提醒", "阈值"},
    "category": {"category", "分类", "库存分类"},
}

_CSV_FIELD_ALIASES_NORMALIZED: Dict[str, set[str]] = {
    key: {_normalize_csv_key(alias) for alias in aliases}
    for key, aliases in _CSV_FIELD_ALIASES.items()
}


def _resolve_csv_field(normalized: Dict[str, Any], canonical: str) -> Any:
    for alias in _CSV_FIELD_ALIASES_NORMALIZED.get(canonical, set()):
        if alias in normalized:
            return normalized[alias]
    return ""


ROLE_LABELS = {
    "super_admin": "超级管理员",
    "admin": "管理员",
    "staff": "普通员工",
}


def create_app(
    storage_path: str | Path = "inventory_data.json",
    user_storage_path: str | Path | None = None,
) -> Flask:
    storage_path = Path(storage_path)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get(
        "INVENTORY_APP_SECRET", "inventory-secret-key"
    )
    app.permanent_session_lifetime = timedelta(days=14)

    app.config.setdefault("API_TOKEN_SALT", "inventory-api-token")
    app.config.setdefault("API_TOKEN_DEFAULT_AGE", 3600)
    app.config.setdefault("API_TOKEN_MAX_AGE", 60 * 60 * 24 * 30)

    token_serializer = URLSafeSerializer(
        app.config["SECRET_KEY"], salt=app.config["API_TOKEN_SALT"]
    )

    manager = InventoryManager(storage_path=storage_path)
    user_storage = (
        Path(user_storage_path)
        if user_storage_path is not None
        else storage_path.with_name("users_data.json")
    )
    user_manager = UserManager(user_storage)

    def _is_safe_redirect(target: Optional[str]) -> bool:
        if not target:
            return False
        ref_url = urlsplit(request.host_url)
        test_url = urlsplit(urljoin(request.host_url, target))
        return (
            test_url.scheme in {"http", "https"}
            and ref_url.netloc == test_url.netloc
        )

    def _current_user():
        return getattr(g, "current_user", None)

    def _current_username() -> Optional[str]:
        user = _current_user()
        return None if user is None else user.username

    def _collect_login_metadata() -> Dict[str, Optional[str]]:
        forwarded_for = request.headers.get("X-Forwarded-For", "")
        ip_address = (forwarded_for.split(",")[0].strip() if forwarded_for else None)
        if not ip_address:
            ip_address = request.remote_addr
        user_agent_string = request.headers.get("User-Agent", "")
        user_agent = request.user_agent
        platform_raw = getattr(user_agent, "platform", None)
        platform = None
        if platform_raw:
            platform_labels = {
                "macos": "macOS",
                "mac": "macOS",
                "darwin": "macOS",
                "windows": "Windows",
                "linux": "Linux",
                "iphone": "iOS",
                "ipad": "iPadOS",
                "android": "Android",
            }
            platform = platform_labels.get(platform_raw.lower(), platform_raw)
        browser_raw = getattr(user_agent, "browser", None)
        browser = None
        if browser_raw:
            browser_labels = {
                "edge": "Microsoft Edge",
                "edg": "Microsoft Edge",
                "chrome": "Chrome",
                "firefox": "Firefox",
                "safari": "Safari",
                "msie": "Internet Explorer",
            }
            browser = browser_labels.get(browser_raw.lower(), browser_raw)
        client_type: Optional[str]
        if platform_raw and platform_raw.lower() == "ipad":
            client_type = "平板端"
        elif getattr(user_agent, "mobile", None):
            client_type = "移动端"
        elif user_agent_string:
            client_type = "桌面端"
        else:
            client_type = None
        return {
            "ip_address": ip_address,
            "user_agent": user_agent_string,
            "client_type": client_type,
            "platform": platform,
            "browser": browser,
        }

    def _json_error(
        message: str,
        status: int = 400,
        *,
        code: Optional[str] = None,
        status_field: bool = False,
    ) -> Any:
        payload: Dict[str, Any] = {"error": message}
        if code:
            payload["code"] = code
        if status_field:
            payload["status"] = "error"
        return jsonify(payload), status

    def _parse_token_expiration(value: Any, default: int) -> int:
        if value is None or value == "":
            return default
        candidate: Optional[int]
        if isinstance(value, bool):
            candidate = None
        elif isinstance(value, int):
            candidate = value
        elif isinstance(value, float):
            candidate = int(value)
        else:
            try:
                candidate = int(str(value).strip())
            except (TypeError, ValueError):
                candidate = None
        if candidate is None or candidate <= 0:
            return default
        max_age = int(app.config.get("API_TOKEN_MAX_AGE", default))
        return min(candidate, max_age)

    def _extract_api_token() -> Optional[str]:
        auth_header = request.headers.get("Authorization")
        if isinstance(auth_header, str):
            scheme, _, token_value = auth_header.partition(" ")
            if scheme.lower() == "bearer" and token_value.strip():
                return token_value.strip()
        header_token = request.headers.get("X-API-Token") or request.headers.get(
            "X-Api-Token"
        )
        if isinstance(header_token, str) and header_token.strip():
            return header_token.strip()
        query_token = request.args.get("api_token")
        if isinstance(query_token, str) and query_token.strip():
            return query_token.strip()
        return None

    def _issue_api_token(username: str, lifetime: int) -> Tuple[str, int, int]:
        issued_at = int(time.time())
        expires_at = issued_at + lifetime
        payload = {"u": username, "iat": issued_at, "exp": expires_at}
        token = token_serializer.dumps(payload)
        return token, issued_at, expires_at

    def _authenticate_api_token(token: str) -> Optional[Any]:
        try:
            payload = token_serializer.loads(token)
        except BadData:
            return None
        username = payload.get("u")
        exp_value = payload.get("exp")
        if not username or exp_value is None:
            return None
        try:
            expires_at = int(exp_value)
        except (TypeError, ValueError):
            return None
        if time.time() > expires_at:
            return None
        try:
            user = user_manager.get_user(str(username))
        except KeyError:
            return None
        g.api_token_payload = payload
        return user

    def _parse_int_value(value: Any) -> Optional[int]:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    def _build_item_snapshot(item: InventoryItem) -> Dict[str, Any]:
        stores_map = _list_stores()
        categories_map = _list_categories()
        payload = item.to_dict()
        category_id = payload.get("category") or ""
        category_entry = categories_map.get(category_id, {})
        payload["category_id"] = category_id or None
        payload["category_name"] = category_entry.get("name") or (
            category_id or "未分类"
        )
        store_entry = stores_map.get(item.store_id, {})
        payload["store_name"] = store_entry.get("name") or item.store_id
        threshold_value = item.threshold
        payload["low_stock"] = (
            threshold_value is not None and item.quantity <= threshold_value
        )
        return payload

    def _build_permissions(user: Optional[Any]) -> Dict[str, bool]:
        role = getattr(user, "role", None)
        can_adjust_in = role in {"admin", "super_admin"}
        can_adjust_out = role in {"staff", "admin", "super_admin"}
        can_manage_items = role in {"admin", "super_admin"}
        is_super_admin = role == "super_admin"
        return {
            "can_adjust": can_adjust_in or can_adjust_out,
            "can_adjust_in": can_adjust_in,
            "can_adjust_out": can_adjust_out,
            "can_manage_items": can_manage_items,
            "can_manage_threshold": can_manage_items,
            "can_manage_users": is_super_admin,
            "can_view_history": can_manage_items,
            "can_clear_history": is_super_admin,
            "can_manage_stores": is_super_admin,
            "can_manage_categories": role in {"admin", "super_admin"},
        }

    def _list_stores() -> Dict[str, Dict[str, Any]]:
        return manager.list_stores()

    def _list_categories() -> Dict[str, Dict[str, Any]]:
        return manager.list_categories()

    def _parse_positive_int(value: Optional[str], default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    def _build_timeline_context(
        history_entries: Sequence[InventoryHistoryEntry],
    ) -> Dict[str, Any]:
        timeline_sku = (request.args.get("timeline_sku") or "").strip()
        if timeline_sku:
            filtered_history = [
                entry for entry in history_entries if entry.name == timeline_sku
            ]
        else:
            filtered_history = list(history_entries)

        timeline_per_page = _parse_positive_int(
            request.args.get("timeline_per_page"), 5
        )
        timeline_page = _parse_positive_int(request.args.get("timeline_page"), 1)
        timeline_total = len(filtered_history)
        timeline_pages = (
            max(1, math.ceil(timeline_total / timeline_per_page))
            if timeline_total
            else 1
        )
        if timeline_page > timeline_pages:
            timeline_page = timeline_pages
        timeline_start = (timeline_page - 1) * timeline_per_page
        timeline_end = timeline_start + timeline_per_page
        timeline_entries = filtered_history[timeline_start:timeline_end]
        timeline = _recent_activity(timeline_entries, limit=None)
        timeline_is_demo = False
        if not timeline:
            timeline = _demo_timeline_events()
            timeline_is_demo = True
            timeline_pagination = {
                "page": 1,
                "per_page": len(timeline) or timeline_per_page,
                "total": len(timeline),
                "pages": 1,
                "has_prev": False,
                "has_next": False,
                "prev_page": 1,
                "next_page": 1,
                "start_index": 1 if timeline else 0,
                "end_index": len(timeline),
            }
        else:
            timeline_pagination = {
                "page": timeline_page,
                "per_page": timeline_per_page,
                "total": timeline_total,
                "pages": timeline_pages,
                "has_prev": timeline_page > 1,
                "has_next": timeline_page < timeline_pages,
                "prev_page": max(1, timeline_page - 1),
                "next_page": min(timeline_pages, timeline_page + 1),
                "start_index": timeline_start + 1 if timeline_total else 0,
                "end_index": min(timeline_end, timeline_total),
            }

        timeline_sku_options = sorted({entry.name for entry in history_entries})
        return {
            "timeline": timeline,
            "timeline_pagination": timeline_pagination,
            "timeline_sku": timeline_sku,
            "timeline_sku_options": timeline_sku_options,
            "timeline_is_demo": timeline_is_demo,
        }

    def _match_store_identifier(
        value: Any, stores: Optional[Mapping[str, Mapping[str, Any]]] = None
    ) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            candidate = value.strip()
        else:
            candidate = str(value).strip()
        if not candidate:
            return None
        if stores is None:
            stores = _list_stores()
        if candidate in stores:
            return candidate
        normalized = candidate.casefold()
        for store_id, entry in stores.items():
            if store_id.casefold() == normalized:
                return store_id
            name = str(entry.get("name") or "").strip()
            if name and name.casefold() == normalized:
                return store_id
        return None

    def _resolve_store_id(store_id: Optional[str] = None) -> str:
        stores = _list_stores()
        use_session = not getattr(g, "auth_via_token", False)
        matched_store = _match_store_identifier(store_id, stores)
        if matched_store:
            if use_session:
                session["store_id"] = matched_store
            return matched_store
        selected = session.get("store_id") if use_session else None
        if selected in stores:
            return selected
        if stores:
            first = next(iter(stores))
            if use_session:
                session["store_id"] = first
            return first
        created = manager.create_store("默认门店")
        if use_session:
            session["store_id"] = created["id"]
        return created["id"]

    def _resolve_category_id(category_id: Optional[str]) -> Optional[str]:
        if not category_id:
            return None
        categories = _list_categories()
        if category_id in categories:
            return category_id
        return None

    def _is_api_request() -> bool:
        if request.path.startswith("/api/"):
            return True
        best = request.accept_mimetypes.best
        return best == "application/json"

    def _unauthorized_response():
        if _is_api_request():
            return _json_error(
                "Unauthorized", 401, code="unauthorized", status_field=True
            )
        next_target = request.full_path if request.query_string else request.path
        return redirect(url_for("login", next=next_target))

    def _forbidden_response():
        if _is_api_request():
            return _json_error("Forbidden", 403, code="forbidden", status_field=True)
        abort(403)

    @app.before_request
    def load_current_user() -> None:
        g.current_user = None
        g.auth_via_token = False
        g.api_token_payload = None
        username = session.get("user")
        if username:
            try:
                g.current_user = user_manager.get_user(username)
                return
            except KeyError:
                session.pop("user", None)
        token = _extract_api_token()
        if token:
            user = _authenticate_api_token(token)
            if user is not None:
                g.current_user = user
                g.auth_via_token = True

    @app.before_request
    def audit_authenticated_request() -> None:
        user = _current_user()
        if user is None:
            return
        if getattr(g, "auth_via_token", False):
            return
        if request.method in {"OPTIONS", "HEAD"}:
            return
        endpoint = request.endpoint or ""
        if endpoint.startswith("static") or (request.blueprint or "") == "static":
            return
        if request.path.startswith("/static/"):
            return
        now_ts = time.time()
        last_event = session.get("_last_access_audit")
        should_record = False
        if not isinstance(last_event, dict):
            should_record = True
        else:
            last_path = last_event.get("path")
            last_method = last_event.get("method")
            last_timestamp = last_event.get("ts")
            if not isinstance(last_timestamp, (int, float)):
                try:
                    last_timestamp = float(last_timestamp)
                except (TypeError, ValueError):
                    last_timestamp = None
            if last_timestamp is None:
                should_record = True
            else:
                time_elapsed = now_ts - float(last_timestamp)
                if last_path != request.path or last_method != request.method:
                    should_record = True
                elif time_elapsed >= 300:
                    should_record = True
        if not should_record:
            return
        metadata = _collect_login_metadata()
        user_manager.record_login(
            user.username,
            ip_address=str(metadata.get("ip_address") or ""),
            user_agent=str(metadata.get("user_agent") or ""),
            client_type=metadata.get("client_type"),
            platform=metadata.get("platform"),
            browser=metadata.get("browser"),
            event_type="access",
            path=request.path,
            method=request.method,
            referrer=str(request.headers.get("Referer") or ""),
        )
        session["_last_access_audit"] = {
            "path": request.path,
            "method": request.method,
            "ts": now_ts,
        }

    @app.context_processor
    def inject_globals() -> Dict[str, Any]:
        user = _current_user()
        return {
            "current_user": user,
            "permissions": _build_permissions(user),
            "role_labels": ROLE_LABELS,
        }

    def login_required(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if _current_user() is None:
                return _unauthorized_response()
            return func(*args, **kwargs)

        return wrapper

    def role_required(*roles: str):
        def decorator(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                user = _current_user()
                if user is None:
                    return _unauthorized_response()
                if user.role not in roles:
                    return _forbidden_response()
                return func(*args, **kwargs)

            return wrapper

        return decorator

    @app.route("/login", methods=["GET", "POST"])
    def login() -> Any:
        if _current_user() is not None:
            next_target = request.args.get("next")
            if _is_safe_redirect(next_target):
                return redirect(next_target)
            return redirect(url_for("index"))

        error: Optional[str] = None
        next_target = request.args.get("next")
        if not _is_safe_redirect(next_target):
            next_target = None

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = user_manager.authenticate(username, password)
            if user is None:
                error = "账号或密码错误"
            else:
                session["user"] = user.username
                session.permanent = True
                metadata = _collect_login_metadata()
                user_manager.record_login(
                    user.username,
                    ip_address=str(metadata.get("ip_address") or ""),
                    user_agent=str(metadata.get("user_agent") or ""),
                    client_type=metadata.get("client_type"),
                    platform=metadata.get("platform"),
                    browser=metadata.get("browser"),
                    event_type="login",
                    path=request.path,
                    method=request.method,
                    referrer=str(request.headers.get("Referer") or ""),
                )
                redirect_target = request.form.get("next") or request.args.get("next")
                if not _is_safe_redirect(redirect_target):
                    redirect_target = url_for("index")
                return redirect(redirect_target)
        return render_template("login.html", error=error, next=next_target)

    @app.post("/logout")
    @login_required
    def logout() -> Any:
        session.pop("user", None)
        return redirect(url_for("login"))

    @app.post("/api/auth/token")
    def issue_api_token_route() -> Any:
        payload = _get_payload(request)
        username = str(
            payload.get("username")
            or request.form.get("username")
            or ""
        ).strip()
        password = str(
            payload.get("password")
            or request.form.get("password")
            or ""
        )
        if not username or not password:
            return _json_error(
                "Missing username or password",
                400,
                code="missing_credentials",
                status_field=True,
            )
        user = user_manager.authenticate(username, password)
        if user is None:
            return _json_error(
                "Invalid username or password",
                401,
                code="invalid_credentials",
                status_field=True,
            )
        default_age = int(app.config.get("API_TOKEN_DEFAULT_AGE", 3600))
        expires_source = payload.get("expires_in")
        if expires_source is None:
            expires_source = request.form.get("expires_in")
        expires_in = _parse_token_expiration(expires_source, default=default_age)
        token, issued_at, expires_at = _issue_api_token(user.username, expires_in)
        metadata = _collect_login_metadata()
        user_manager.record_login(
            user.username,
            ip_address=str(metadata.get("ip_address") or ""),
            user_agent=str(metadata.get("user_agent") or ""),
            client_type=metadata.get("client_type"),
            platform=metadata.get("platform"),
            browser=metadata.get("browser"),
            event_type="login",
            path=request.path,
            method=request.method,
            referrer=str(request.headers.get("Referer") or ""),
        )
        return jsonify(
            {
                "status": "success",
                "token": token,
                "token_type": "Bearer",
                "issued_at": issued_at,
                "expires_at": expires_at,
                "expires_in": expires_in,
                "user": {"username": user.username, "role": user.role},
            }
        )

    @app.post("/stores/select")
    @login_required
    def select_store() -> Any:
        payload = _get_payload(request)
        target = (
            payload.get("store_id")
            if isinstance(payload, Mapping)
            else None
        )
        if not target:
            target = request.form.get("store_id") or request.args.get("store_id")
        stores = _list_stores()
        if target in stores:
            session["store_id"] = target
            if request.is_json:
                return jsonify({"store_id": target})
            next_target = request.form.get("next") or request.args.get("next")
            if not next_target:
                next_target = request.referrer
            if not _is_safe_redirect(next_target):
                next_target = url_for("index")
            return redirect(next_target)
        if request.is_json:
            return jsonify({"error": "指定门店不存在"}), 404
        return redirect(url_for("index"))

    @app.post("/stores")
    @role_required("super_admin")
    def create_store_route() -> Any:
        payload = _get_payload(request)
        name = str(payload.get("name") or request.form.get("name") or "").strip()
        try:
            created = manager.create_store(name)
        except ValueError as exc:
            if request.is_json:
                return {"error": str(exc)}, 400
            flash(str(exc), "error")
            return redirect(url_for("index"))
        if request.is_json:
            return jsonify(created), 201
        flash("已新增门店", "success")
        return redirect(url_for("index"))

    @app.post("/stores/<string:store_id>/delete")
    @role_required("super_admin")
    def delete_store_route(store_id: str) -> Any:
        payload = _get_payload(request)
        cascade_value = payload.get("cascade") if isinstance(payload, Mapping) else None
        if cascade_value is None:
            cascade_value = request.form.get("cascade") or request.args.get("cascade")
        cascade = str(cascade_value).lower() in {"1", "true", "yes", "on"}
        try:
            manager.delete_store(store_id, cascade=cascade, user=_current_username())
        except ValueError as exc:
            if request.is_json:
                return {"error": str(exc)}, 400
            flash(str(exc), "error")
            return redirect(url_for("index"))
        except KeyError as exc:
            if request.is_json:
                return {"error": str(exc)}, 404
            flash("门店不存在", "error")
            return redirect(url_for("index"))
        if session.get("store_id") == store_id:
            session.pop("store_id", None)
        if request.is_json:
            return "", 204
        flash("已删除门店", "success")
        return redirect(url_for("index"))

    @app.post("/categories")
    @role_required("admin", "super_admin")
    def create_category_route() -> Any:
        payload = _get_payload(request)
        name = str(payload.get("name") or request.form.get("name") or "").strip()
        try:
            created = manager.create_category(name)
        except ValueError as exc:
            if request.is_json:
                return {"error": str(exc)}, 400
            flash(str(exc), "error")
            return redirect(url_for("index"))
        if request.is_json:
            return jsonify(created), 201
        flash("已新增分类", "success")
        return redirect(url_for("index"))

    @app.post("/categories/<string:category_id>/delete")
    @role_required("admin", "super_admin")
    def delete_category_route(category_id: str) -> Any:
        payload = _get_payload(request)
        cascade_value = payload.get("cascade") if isinstance(payload, Mapping) else None
        if cascade_value is None:
            cascade_value = request.form.get("cascade") or request.args.get("cascade")
        cascade = str(cascade_value).lower() in {"1", "true", "yes", "on"}
        store_value = payload.get("store_id") if isinstance(payload, Mapping) else None
        if store_value is None:
            store_value = request.form.get("store_id") or request.args.get("store_id")
        resolved_store = None
        if store_value is not None:
            resolved_store = _resolve_store_id(store_value)
        try:
            manager.delete_category(
                category_id,
                cascade=cascade,
                user=_current_username(),
                store_id=resolved_store,
            )
        except ValueError as exc:
            if request.is_json:
                return {"error": str(exc)}, 400
            flash(str(exc), "error")
            return redirect(url_for("index"))
        except KeyError as exc:
            if request.is_json:
                return {"error": str(exc)}, 404
            flash("分类不存在", "error")
            return redirect(url_for("index"))
        if request.is_json:
            return "", 204
        flash("已删除分类", "success")
        return redirect(url_for("index"))

    def _format_datetime(value: Optional[datetime], fmt: str = "%Y-%m-%d %H:%M") -> str:
        if value is None:
            return "—"
        return value.astimezone().strftime(fmt)

    app.jinja_env.filters["format_datetime"] = _format_datetime

    def _latest_timestamp(items: list[InventoryItem], key: str) -> Optional[datetime]:
        values = [getattr(item, key) for item in items if getattr(item, key) is not None]
        if not values:
            return None
        return max(values)

    @app.get("/")
    @login_required
    def index() -> str:
        selected_store = _resolve_store_id(request.args.get("store"))
        selected_category = _resolve_category_id(request.args.get("category"))
        stores = _list_stores()
        categories = _list_categories()
        all_items = manager.list_items(
            store_id=selected_store, category_id=selected_category
        ).values()

        def _is_low_stock(item: InventoryItem) -> bool:
            return item.threshold is not None and item.quantity <= item.threshold

        items_sorted = sorted(
            all_items,
            key=lambda item: (
                not _is_low_stock(item),
                item.name.casefold(),
            ),
        )
        low_stock_items = [item for item in items_sorted if _is_low_stock(item)]
        inventory_search = (request.args.get("inventory_search") or "").strip()

        if inventory_search:
            search_term = inventory_search.casefold()
            items_filtered = [
                item for item in items_sorted if search_term in item.name.casefold()
            ]
        else:
            items_filtered = list(items_sorted)
        total_quantity = sum(item.quantity for item in items_sorted)
        latest_in = _latest_timestamp(items_sorted, key="last_in")
        latest_out = _latest_timestamp(items_sorted, key="last_out")
        summary = {
            "total_items": len(items_sorted),
            "total_quantity": total_quantity,
            "latest_in": latest_in,
            "latest_out": latest_out,
            "low_stock_count": len(low_stock_items),
        }
        inventory_per_page = _parse_positive_int(
            request.args.get("inventory_per_page"), 10
        )
        inventory_page = _parse_positive_int(request.args.get("inventory_page"), 1)
        inventory_total = len(items_filtered)
        inventory_pages = (
            max(1, math.ceil(inventory_total / inventory_per_page))
            if inventory_total
            else 1
        )
        if inventory_page > inventory_pages:
            inventory_page = inventory_pages
        inventory_start = (inventory_page - 1) * inventory_per_page
        inventory_end = inventory_start + inventory_per_page
        items = items_filtered[inventory_start:inventory_end]
        inventory_pagination = {
            "page": inventory_page,
            "per_page": inventory_per_page,
            "total": inventory_total,
            "pages": inventory_pages,
            "has_prev": inventory_page > 1,
            "has_next": inventory_page < inventory_pages,
            "prev_page": max(1, inventory_page - 1),
            "next_page": min(inventory_pages, inventory_page + 1),
            "start_index": inventory_start + 1 if inventory_total else 0,
            "end_index": min(inventory_end, inventory_total),
        }

        preserved_query = {key: request.args.getlist(key) for key in request.args}

        def build_query(**updates: Any) -> str:
            params = request.args.to_dict()
            for key, value in updates.items():
                if value is None:
                    params.pop(key, None)
                else:
                    params[key] = value
            return url_for("index", **params)

        import_summary = _parse_import_summary(request)
        return render_template(
            "index.html",
            items=items,
            summary=summary,
            import_summary=import_summary,
            low_stock_items=low_stock_items,
            stores=stores,
            categories=categories,
            selected_store=selected_store,
            selected_category=selected_category,
            inventory_pagination=inventory_pagination,
            preserved_query=preserved_query,
            build_query=build_query,
            inventory_search=inventory_search,
        )

    @app.get("/history")
    @role_required("admin", "super_admin")
    def recent_activity() -> Any:
        stores = _list_stores()
        selected_store = _resolve_store_id(request.args.get("store_id"))
        categories = _list_categories()
        timeline_context = _build_timeline_context(
            manager.list_history(store_id=selected_store)
        )

        preserved_query = {key: request.args.getlist(key) for key in request.args}

        def build_query(**updates: Any) -> str:
            params = request.args.to_dict()
            for key, value in updates.items():
                if value is None:
                    params.pop(key, None)
                else:
                    params[key] = value
            return url_for("recent_activity", **params)

        return render_template(
            "recent_activity.html",
            stores=stores,
            categories=categories,
            selected_store=selected_store,
            preserved_query=preserved_query,
            build_query=build_query,
            **timeline_context,
        )

    @app.post("/history/clear")
    @role_required("super_admin")
    def clear_history() -> Any:
        manager.clear_history()
        flash("已清除最近动态记录", "success")
        next_target = request.form.get("next") or request.args.get("next")
        if not _is_safe_redirect(next_target):
            next_target = url_for("recent_activity")
        return redirect(next_target)

    @app.get("/api/items")
    @login_required
    def list_items() -> Any:
        store_id = _resolve_store_id(request.args.get("store_id"))
        category_id = _resolve_category_id(request.args.get("category_id"))
        items = manager.list_items(store_id=store_id, category_id=category_id)
        return jsonify([item.to_dict() for item in items.values()])

    @app.post("/api/items")
    @role_required("admin", "super_admin")
    def add_item() -> Any:
        payload = _get_payload(request)
        name = payload.get("name")
        quantity = int(payload.get("quantity", 0))
        if not name:
            return {"error": "Missing item name"}, 400
        unit = str(payload.get("unit", "") or "").strip()
        threshold = _parse_threshold_value(payload.get("threshold"))
        store_id = _resolve_store_id(payload.get("store_id"))
        category_id = payload.get("category")
        item = manager.set_quantity(
            name,
            quantity,
            unit=unit,
            threshold=threshold,
            store_id=store_id,
            category=category_id,
            user=_current_username(),
        )
        return jsonify(item.to_dict()), 201

    @app.put("/api/items/<string:name>")
    @role_required("admin", "super_admin")
    def update_item(name: str) -> Any:
        payload = _get_payload(request)
        if "quantity" not in payload:
            return {"error": "Missing quantity"}, 400
        try:
            quantity = int(payload.get("quantity", 0))
        except (TypeError, ValueError):
            return {"error": "Invalid quantity"}, 400
        unit = payload.get("unit")
        threshold_provided = "threshold" in payload
        threshold = _parse_threshold_value(payload.get("threshold"))
        store_id = _resolve_store_id(payload.get("store_id"))
        category_id = payload.get("category")
        try:
            manager.get_item(name, store_id=store_id)
        except KeyError as exc:
            return {"error": str(exc)}, 404
        try:
            item = manager.set_quantity(
                name,
                quantity,
                unit=str(unit).strip() if unit is not None else None,
                threshold=threshold,
                keep_threshold=not threshold_provided,
                category=category_id,
                store_id=store_id,
                user=_current_username(),
            )
        except ValueError as exc:
            return {"error": str(exc)}, 400
        return jsonify(item.to_dict())

    @app.post("/api/items/<string:name>/in")
    @role_required("admin", "super_admin")
    def stock_in(name: str) -> Any:
        payload = _get_payload(request)
        delta = int(payload.get("quantity", 0))
        if delta <= 0:
            return {"error": "Quantity must be greater than zero"}, 400
        store_id = _resolve_store_id(payload.get("store_id"))
        item = manager.adjust_quantity(
            name, delta, store_id=store_id, user=_current_username()
        )
        return jsonify(item.to_dict())

    @app.post("/api/items/<string:name>/out")
    @login_required
    def stock_out(name: str) -> Any:
        payload = _get_payload(request)
        delta = int(payload.get("quantity", 0))
        if delta <= 0:
            return {"error": "Quantity must be greater than zero"}, 400
        store_id = _resolve_store_id(payload.get("store_id"))
        try:
            item = manager.adjust_quantity(
                name, -delta, store_id=store_id, user=_current_username()
            )
        except ValueError as exc:
            return {"error": str(exc)}, 400
        return jsonify(item.to_dict())

    @app.post("/api/items/<string:name>/transfer")
    @role_required("admin", "super_admin")
    def transfer_item_api(name: str) -> Any:
        payload = _get_payload(request)
        try:
            quantity = int(payload.get("quantity", 0))
        except (TypeError, ValueError):
            return {"error": "Invalid quantity"}, 400
        if quantity <= 0:
            return {"error": "Quantity must be greater than zero"}, 400
        source_store_id = _resolve_store_id(payload.get("source_store_id"))
        target_store_id = payload.get("target_store_id")
        stores_map = _list_stores()
        if not target_store_id or target_store_id not in stores_map:
            return {"error": "Invalid target store"}, 400
        if source_store_id == target_store_id:
            return {"error": "Source and target stores must differ"}, 400
        try:
            source_item, target_item = manager.transfer_item(
                name,
                quantity,
                source_store_id=source_store_id,
                target_store_id=target_store_id,
                user=_current_username(),
            )
        except KeyError as exc:
            return {"error": str(exc)}, 404
        except ValueError as exc:
            return {"error": str(exc)}, 400
        return jsonify(
            {
                "source": source_item.to_dict(),
                "target": target_item.to_dict(),
            }
        )

    @app.delete("/api/items/<string:name>")
    @role_required("admin", "super_admin")
    def delete_item(name: str) -> Any:
        store_id = _resolve_store_id(request.args.get("store_id"))
        try:
            manager.delete_item(name, store_id=store_id, user=_current_username())
        except KeyError as exc:
            return {"error": str(exc)}, 404
        return "", 204

    @app.get("/api/history")
    @login_required
    def list_history() -> Any:
        limit_raw = request.args.get("limit")
        limit: Optional[int]
        if limit_raw is None or limit_raw == "":
            limit = None
        else:
            try:
                limit_value = int(limit_raw)
            except (TypeError, ValueError):
                return {"error": "Invalid limit"}, 400
            if limit_value < 0:
                return {"error": "Invalid limit"}, 400
            limit = limit_value
        store_id = _resolve_store_id(request.args.get("store_id"))
        history_entries = manager.list_history(store_id=store_id, limit=limit)
        return jsonify([entry.to_dict() for entry in history_entries])

    @app.get("/api/shortcuts/profile")
    @login_required
    def shortcuts_profile() -> Any:
        user = _current_user()
        stores_map = _list_stores()
        categories_map = _list_categories()
        stores = [
            {
                "id": store_id,
                "name": entry.get("name") or store_id,
                "items_count": entry.get("items_count", 0),
            }
            for store_id, entry in sorted(
                stores_map.items(),
                key=lambda item: str(item[1].get("name") or item[0]).lower(),
            )
        ]
        categories = [
            {
                "id": category_id,
                "name": entry.get("name") or category_id,
            }
            for category_id, entry in sorted(
                categories_map.items(),
                key=lambda item: str(item[1].get("name") or item[0]).lower(),
            )
        ]
        return jsonify(
            {
                "status": "success",
                "user": {
                    "username": getattr(user, "username", None),
                    "role": getattr(user, "role", None),
                },
                "permissions": _build_permissions(user),
                "stores": stores,
                "categories": categories,
            }
        )

    @app.get("/api/shortcuts/items/summary")
    @login_required
    def shortcuts_item_summary() -> Any:
        name_value = request.args.get("name") or request.args.get("item") or ""
        name = str(name_value).strip()
        if not name:
            return _json_error(
                "Missing item name",
                code="missing_name",
                status_field=True,
            )
        stores_map = _list_stores()
        store_hint_raw = (
            request.args.get("store_id")
            or request.args.get("store")
            or request.args.get("store_name")
        )
        store_hint = str(store_hint_raw).strip() if store_hint_raw else None
        resolved_store: Optional[str]
        if store_hint:
            resolved_store = _match_store_identifier(store_hint, stores_map)
            if resolved_store is None:
                return _json_error(
                    "Store not found",
                    404,
                    code="store_not_found",
                    status_field=True,
                )
        else:
            resolved_store = _resolve_store_id(None)
        if not resolved_store:
            return _json_error(
                "Store not found",
                404,
                code="store_not_found",
                status_field=True,
            )
        try:
            item = manager.get_item(name, store_id=resolved_store)
        except KeyError:
            return _json_error(
                "Item not found",
                404,
                code="item_not_found",
                status_field=True,
            )
        return jsonify({"status": "success", "item": _build_item_snapshot(item)})

    @app.post("/api/shortcuts/items/adjust")
    @login_required
    def shortcuts_adjust_item() -> Any:
        raw_payload = _get_payload(request)
        payload: Mapping[str, Any]
        if isinstance(raw_payload, Mapping):
            payload = raw_payload
        else:
            payload = {}
        name_value = (
            payload.get("name")
            or payload.get("item")
            or request.args.get("name")
            or ""
        )
        name = str(name_value).strip()
        if not name:
            return _json_error(
                "Missing item name",
                code="missing_name",
                status_field=True,
            )
        action_raw = payload.get("action") or payload.get("type") or "set"
        action_key = str(action_raw).strip().lower()
        action_aliases = {
            "update": "set",
            "create": "set",
            "increase": "in",
            "increment": "in",
            "add": "in",
            "decrease": "out",
            "decrement": "out",
            "remove": "out",
            "subtract": "out",
        }
        normalized_action = action_aliases.get(action_key, action_key or "set")
        store_hint_value = (
            payload.get("store_id")
            or payload.get("store")
            or payload.get("store_name")
            or request.args.get("store_id")
            or request.args.get("store")
            or request.args.get("store_name")
        )
        if isinstance(store_hint_value, str):
            store_hint = store_hint_value.strip()
        elif store_hint_value is None:
            store_hint = None
        else:
            store_hint = str(store_hint_value).strip()
        if store_hint == "":
            store_hint = None
        stores_map = _list_stores()
        if store_hint:
            resolved_store = _match_store_identifier(store_hint, stores_map)
            if resolved_store is None:
                return _json_error(
                    "Store not found",
                    404,
                    code="store_not_found",
                    status_field=True,
                )
        else:
            resolved_store = _resolve_store_id(None)
        permissions = _build_permissions(_current_user())
        username = _current_username()
        if normalized_action in {"set"}:
            if not permissions["can_manage_items"]:
                return _json_error(
                    "Permission denied",
                    403,
                    code="forbidden",
                    status_field=True,
                )
            quantity_value = payload.get("quantity")
            quantity = _parse_int_value(quantity_value)
            if quantity is None or quantity < 0:
                return _json_error(
                    "Quantity must be zero or greater",
                    code="invalid_quantity",
                    status_field=True,
                )
            unit_value = payload.get("unit")
            unit = str(unit_value).strip() if unit_value is not None else None
            threshold_provided = "threshold" in payload
            threshold = _parse_threshold_value(payload.get("threshold"))
            category_value = (
                payload.get("category_id")
                or payload.get("category")
                or payload.get("category_name")
            )
            try:
                item = manager.set_quantity(
                    name,
                    quantity,
                    unit=unit,
                    threshold=threshold,
                    keep_threshold=not threshold_provided,
                    category=category_value,
                    store_id=resolved_store,
                    user=username,
                )
            except ValueError as exc:
                return _json_error(
                    str(exc),
                    code="invalid_quantity",
                    status_field=True,
                )
            return jsonify(
                {
                    "status": "success",
                    "action": "set",
                    "store_id": resolved_store,
                    "item": _build_item_snapshot(item),
                }
            )
        if normalized_action == "in":
            if not permissions["can_adjust_in"]:
                return _json_error(
                    "Permission denied",
                    403,
                    code="forbidden",
                    status_field=True,
                )
            quantity = _parse_int_value(payload.get("quantity"))
            if quantity is None or quantity <= 0:
                return _json_error(
                    "Quantity must be greater than zero",
                    code="invalid_quantity",
                    status_field=True,
                )
            try:
                item = manager.adjust_quantity(
                    name,
                    quantity,
                    store_id=resolved_store,
                    user=username,
                )
            except KeyError:
                return _json_error(
                    "Item not found",
                    404,
                    code="item_not_found",
                    status_field=True,
                )
            except ValueError as exc:
                return _json_error(
                    str(exc),
                    code="invalid_quantity",
                    status_field=True,
                )
            return jsonify(
                {
                    "status": "success",
                    "action": "in",
                    "store_id": resolved_store,
                    "item": _build_item_snapshot(item),
                }
            )
        if normalized_action == "out":
            if not permissions["can_adjust_out"]:
                return _json_error(
                    "Permission denied",
                    403,
                    code="forbidden",
                    status_field=True,
                )
            quantity = _parse_int_value(payload.get("quantity"))
            if quantity is None or quantity <= 0:
                return _json_error(
                    "Quantity must be greater than zero",
                    code="invalid_quantity",
                    status_field=True,
                )
            try:
                item = manager.adjust_quantity(
                    name,
                    -quantity,
                    store_id=resolved_store,
                    user=username,
                )
            except KeyError:
                return _json_error(
                    "Item not found",
                    404,
                    code="item_not_found",
                    status_field=True,
                )
            except ValueError as exc:
                return _json_error(
                    str(exc),
                    code="invalid_quantity",
                    status_field=True,
                )
            return jsonify(
                {
                    "status": "success",
                    "action": "out",
                    "store_id": resolved_store,
                    "item": _build_item_snapshot(item),
                }
            )
        if normalized_action == "transfer":
            if not permissions["can_manage_items"]:
                return _json_error(
                    "Permission denied",
                    403,
                    code="forbidden",
                    status_field=True,
                )
            quantity = _parse_int_value(payload.get("quantity"))
            if quantity is None or quantity <= 0:
                return _json_error(
                    "Quantity must be greater than zero",
                    code="invalid_quantity",
                    status_field=True,
                )
            target_store_raw = (
                payload.get("target_store_id")
                or payload.get("target_store")
                or payload.get("target")
            )
            target_store = str(target_store_raw).strip() if target_store_raw else ""
            if not target_store:
                return _json_error(
                    "Target store is required",
                    code="missing_target_store",
                    status_field=True,
                )
            stores_map = _list_stores()
            if target_store not in stores_map:
                return _json_error(
                    "Target store not found",
                    404,
                    code="target_store_not_found",
                    status_field=True,
                )
            if resolved_store == target_store:
                return _json_error(
                    "Source and target stores must differ",
                    code="invalid_target_store",
                    status_field=True,
                )
            try:
                source_item, target_item = manager.transfer_item(
                    name,
                    quantity,
                    source_store_id=resolved_store,
                    target_store_id=target_store,
                    user=username,
                )
            except KeyError:
                return _json_error(
                    "Item not found",
                    404,
                    code="item_not_found",
                    status_field=True,
                )
            except ValueError as exc:
                return _json_error(
                    str(exc),
                    code="invalid_quantity",
                    status_field=True,
                )
            return jsonify(
                {
                    "status": "success",
                    "action": "transfer",
                    "source": _build_item_snapshot(source_item),
                    "target": _build_item_snapshot(target_item),
                }
            )
        return _json_error(
            "Unsupported action",
            code="unsupported_action",
            status_field=True,
        )

    @app.get("/api/items/template")
    @login_required
    def download_template() -> Response:
        rows = [
            {"名称": "示例SKU", "数量": 50, "单位": "件", "阈值提醒": 10, "库存分类": "未分类"},
        ]
        content = _rows_to_xls(["名称", "数量", "单位", "阈值提醒", "库存分类"], rows)
        filename = _timestamped_filename("inventory_template")
        return _xls_response(content, filename)

    @app.get("/api/items/export")
    @login_required
    def export_inventory() -> Response:
        selected_store = _resolve_store_id(request.args.get("store_id"))
        rows = manager.export_items(store_id=selected_store)
        report_rows: List[Dict[str, Any]] = []
        stores_map = _list_stores()
        store_label = "全部门店"
        if selected_store:
            store_entry = stores_map.get(selected_store, {})
            store_label = store_entry.get("name") or selected_store or "全部门店"
        for row in rows:
            if not isinstance(row, dict):
                continue
            quantity_raw = row.get("quantity", 0)
            try:
                quantity_value = int(quantity_raw)
            except (TypeError, ValueError):
                quantity_value = quantity_raw if quantity_raw is not None else 0
            report_rows.append(
                {
                    "门店": row.get("store_name") or row.get("store_id") or "—",
                    "分类": row.get("category_name") or "未分类",
                    "商品名称": row.get("name") or "",
                    "库存数量": quantity_value,
                    "单位": row.get("unit") or "",
                }
            )
        generated_at = datetime.now().astimezone()
        generated_label = generated_at.strftime("%Y年%m月%d日 %H:%M")
        username = _current_username() or "—"
        content = _inventory_report_to_xls(
            report_rows,
            generated_label=generated_label,
            username=username,
            store_label=store_label if selected_store else None,
        )
        filename = _timestamped_filename("inventory_export")
        return _xls_response(content, filename)

    @app.get("/api/history/export")
    @login_required
    def export_history() -> Response:
        selected_store = _resolve_store_id(request.args.get("store_id"))
        entries = manager.list_history(store_id=selected_store)
        action_labels = {
            "in": "入库",
            "out": "出库",
            "create": "新增",
            "set": "盘点",
            "delete": "删除",
        }
        rows = []
        
        def _parse_int(value: Any) -> Optional[int]:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        for entry in entries:
            local_time = entry.timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            user = str(entry.meta.get("user") or "系统")
            store_name = str(
                entry.meta.get("store_name")
                or entry.meta.get("store_id")
                or "—"
            )
            category_name = str(
                entry.meta.get("category_name")
                or entry.meta.get("category_id")
                or "—"
            )
            meta = entry.meta or {}
            previous_quantity = _parse_int(meta.get("previous_quantity"))
            new_quantity = _parse_int(meta.get("new_quantity"))
            delta_value = _parse_int(meta.get("delta"))
            quantity_value = _parse_int(meta.get("quantity"))
            operation_label = action_labels.get(entry.action, entry.action or "—")
            if meta.get("transfer"):
                if entry.action == "in":
                    operation_label = "调拨入库"
                elif entry.action == "out":
                    operation_label = "调拨出库"

            initial_quantity = previous_quantity
            current_quantity = new_quantity
            change_quantity: Optional[int] = None

            if entry.action == "in":
                if delta_value is not None:
                    change_quantity = abs(delta_value)
                if current_quantity is None:
                    current_quantity = new_quantity
                if initial_quantity is None and current_quantity is not None and change_quantity is not None:
                    initial_quantity = current_quantity - change_quantity
            elif entry.action == "out":
                if delta_value is not None:
                    change_quantity = -abs(delta_value)
                if current_quantity is None:
                    current_quantity = new_quantity
                if initial_quantity is None and current_quantity is not None and change_quantity is not None:
                    initial_quantity = current_quantity - change_quantity
            elif entry.action == "set":
                if delta_value is not None:
                    change_quantity = delta_value
                if current_quantity is None:
                    current_quantity = new_quantity
            elif entry.action == "create":
                if current_quantity is None:
                    current_quantity = quantity_value
                if change_quantity is None and current_quantity is not None:
                    change_quantity = current_quantity
                if initial_quantity is None:
                    initial_quantity = 0
            elif entry.action == "delete":
                if change_quantity is None and previous_quantity is not None:
                    change_quantity = -previous_quantity
                if current_quantity is None:
                    current_quantity = 0
                if initial_quantity is None:
                    initial_quantity = previous_quantity

            if change_quantity is None and delta_value is not None:
                change_quantity = delta_value
            if current_quantity is None and quantity_value is not None:
                current_quantity = quantity_value
            if initial_quantity is None and current_quantity is not None and change_quantity is not None:
                initial_quantity = current_quantity - change_quantity

            rows.append(
                {
                    "时间": local_time,
                    "操作类型": operation_label,
                    "SKU 名称": entry.name,
                    "操作用户": user,
                    "门店": store_name,
                    "分类": category_name,
                    "初始量": initial_quantity if initial_quantity is not None else "",
                    "增减量": change_quantity if change_quantity is not None else "",
                    "当前量": current_quantity if current_quantity is not None else "",
                }
            )
        fieldnames = ["时间", "操作类型", "SKU 名称", "操作用户", "门店", "分类", "初始量", "增减量", "当前量"]
        content = _rows_to_xls(fieldnames, rows)
        filename = _timestamped_filename("inventory_history")
        return _xls_response(content, filename)

    @app.get("/analytics")
    @role_required("admin", "super_admin")
    def analytics_dashboard() -> str:
        selected_store = _resolve_store_id(request.args.get("store"))
        stores_map = _list_stores()
        mode, start_dt, end_dt, start_value, end_value = _resolve_history_filters(
            request.args, mode_hint="sku"
        )
        mode = "sku"
        entries = manager.list_history(store_id=selected_store)
        stats_rows = _history_statistics(entries, mode=mode, start=start_dt, end=end_dt)
        total_inbound = sum(row["inbound"] for row in stats_rows)
        total_outbound = sum(row["outbound"] for row in stats_rows)
        total_ending = sum(
            row.get("ending_quantity") or 0 for row in stats_rows if isinstance(row, dict)
        )
        totals = {
            "inbound": total_inbound,
            "outbound": total_outbound,
            "net": total_inbound - total_outbound,
            "ending_quantity": total_ending,
        }
        export_params = {"mode": mode}
        export_params["store_id"] = selected_store
        if start_value:
            export_params["start"] = start_value
        if end_value:
            export_params["end"] = end_value
        export_url = url_for("export_history_stats", **export_params)
        range_label = f"{start_value} 至 {end_value}"
        return render_template(
            "analytics.html",
            mode=mode,
            start_value=start_value,
            end_value=end_value,
            stats_rows=stats_rows,
            totals=totals,
            has_data=bool(stats_rows),
            export_url=export_url,
            range_label=range_label,
            selected_store=selected_store,
            selected_store_name=stores_map.get(selected_store, {}).get(
                "name", selected_store
            ),
            stores=stores_map,
        )

    @app.get("/api/history/stats/export")
    @role_required("admin", "super_admin")
    def export_history_stats() -> Response:
        selected_store = _resolve_store_id(request.args.get("store_id"))
        mode, start_dt, end_dt, start_value, end_value = _resolve_history_filters(
            request.args, mode_hint="sku"
        )
        mode = "sku"
        entries = manager.list_history(store_id=selected_store)
        stats_rows = _history_statistics(entries, mode=mode, start=start_dt, end=end_dt)
        csv_rows: List[Dict[str, Any]] = []
        total_ending = 0
        for row in stats_rows:
            ending_quantity = row.get("ending_quantity")
            if isinstance(ending_quantity, int):
                total_ending += ending_quantity
            csv_rows.append(
                {
                    "SKU 名称": row.get("sku") or row.get("label", ""),
                    "分类": row.get("category", ""),
                    "单位": row.get("unit", ""),
                    "入库数量": row["inbound"],
                    "出库数量": row["outbound"],
                    "净变动": row["net"],
                    "截止库存": "" if ending_quantity is None else ending_quantity,
                }
            )
        if not csv_rows:
            csv_rows.append(
                {
                    "SKU 名称": "（无数据）",
                    "分类": "",
                    "单位": "",
                    "入库数量": 0,
                    "出库数量": 0,
                    "净变动": 0,
                    "截止库存": 0,
                }
            )
        else:
            total_inbound = sum(row["入库数量"] for row in csv_rows)
            total_outbound = sum(row["出库数量"] for row in csv_rows)
            total_net = total_inbound - total_outbound
            csv_rows.append(
                {
                    "SKU 名称": "合计",
                    "分类": "",
                    "单位": "",
                    "入库数量": total_inbound,
                    "出库数量": total_outbound,
                    "净变动": total_net,
                    "截止库存": total_ending,
                }
            )
        fieldnames = [
            "SKU 名称",
            "分类",
            "单位",
            "入库数量",
            "出库数量",
            "净变动",
            "截止库存",
        ]
        stores_map = _list_stores()
        store_name = stores_map.get(selected_store, {}).get("name", selected_store)
        range_label = f"{start_value} 至 {end_value}" if start_value or end_value else "—"
        metadata = [
            ("门店", store_name),
            ("统计时间范围", range_label),
            ("导出时间", datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")),
        ]
        content = _rows_to_xls(fieldnames, csv_rows, metadata=metadata)
        filename = _timestamped_filename("inventory_history_stats")
        return _xls_response(content, filename)

    @app.post("/api/items/import/preview")
    @role_required("admin", "super_admin")
    def preview_import_inventory_api() -> Any:
        try:
            rows = _extract_import_rows(request)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        payload = _get_payload(request)
        store_id = payload.get("store_id") or request.args.get("store_id")
        resolved_store = _resolve_store_id(store_id)
        preview_rows = manager.preview_import_rows(
            rows, store_id=resolved_store
        )
        valid_count = sum(1 for row in preview_rows if row.get("valid"))
        return jsonify(
            {
                "rows": preview_rows,
                "total": len(preview_rows),
                "valid": valid_count,
                "invalid": len(preview_rows) - valid_count,
            }
        )

    @app.post("/api/items/import")
    @role_required("admin", "super_admin")
    def import_inventory_api() -> Any:
        try:
            rows = _extract_import_rows(request)
        except ValueError as exc:
            return {"error": str(exc)}, 400
        payload = _get_payload(request)
        store_id = payload.get("store_id") or request.args.get("store_id")
        resolved_store = _resolve_store_id(store_id)
        imported = manager.import_items(
            rows, store_id=resolved_store, user=_current_username()
        )
        return jsonify(
            {
                "imported": [item.to_dict() for item in imported],
                "count": len(imported),
            }
        )

    @app.post("/import")
    @role_required("admin", "super_admin")
    def import_inventory_form() -> Any:
        upload = request.files.get("file")
        if upload is None or upload.filename == "":
            return redirect(url_for("index", import_error=1))
        try:
            rows = _extract_rows_from_filestorage(upload)
        except ValueError:
            return redirect(url_for("index", import_error=1))
        total_rows = len(rows)
        resolved_store = _resolve_store_id(request.form.get("store_id"))
        imported = manager.import_items(
            rows, store_id=resolved_store, user=_current_username()
        )
        return redirect(
            url_for(
                "index",
                imported=len(imported),
                skipped=max(total_rows - len(imported), 0),
            )
        )

    @app.get("/users")
    @role_required("super_admin")
    def manage_users() -> Any:
        users = sorted(
            user_manager.list_users().values(), key=lambda u: u.username.lower()
        )
        login_records = user_manager.list_login_records()
        login_user_count = len({record.username for record in login_records})
        return render_template(
            "users.html",
            users=users,
            login_records=login_records,
            login_user_count=login_user_count,
        )

    @app.post("/admin/login-logs/clear")
    @role_required("super_admin")
    def clear_login_logs_route() -> Any:
        user_manager.clear_login_records()
        session.pop("_last_access_audit", None)
        flash("已清空登录日志", "success")
        return redirect(url_for("manage_users"))

    @app.post("/users")
    @role_required("super_admin")
    def create_user_route() -> Any:
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "staff")
        try:
            user_manager.create_user(username, password, role)
            flash("已创建用户", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("manage_users"))

    @app.post("/users/<string:username>/update")
    @role_required("super_admin")
    def update_user_route(username: str) -> Any:
        new_username = request.form.get("username", "").strip() or username
        new_password = request.form.get("password") or None
        role = request.form.get("role") or None
        try:
            updated_user = user_manager.update_user(
                username,
                new_username=new_username,
                new_password=new_password,
                new_role=role,
            )
            if _current_username() == username:
                session["user"] = updated_user.username
            flash("已更新用户信息", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        except KeyError:
            flash("用户不存在", "error")
        return redirect(url_for("manage_users"))

    @app.post("/users/<string:username>/delete")
    @role_required("super_admin")
    def delete_user_route(username: str) -> Any:
        if _current_username() == username:
            flash("不能删除当前登录账号", "error")
            return redirect(url_for("manage_users"))
        try:
            user_manager.delete_user(username)
            flash("已删除用户", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        except KeyError:
            flash("用户不存在", "error")
        return redirect(url_for("manage_users"))

    @app.post("/submit")
    @login_required
    def submit_form() -> Any:
        action = request.form.get("action")
        name = request.form.get("name", "").strip()
        quantity_raw = request.form.get("quantity")
        unit_raw = request.form.get("unit")
        threshold_raw = request.form.get("threshold")
        if not name:
            return redirect(url_for("index"))
        quantity: Optional[int]
        if quantity_raw is None or quantity_raw == "":
            quantity = None
        else:
            try:
                quantity = int(quantity_raw)
            except ValueError:
                return redirect(url_for("index"))

        unit = None if unit_raw is None else str(unit_raw).strip()

        user = _current_user()
        permissions = _build_permissions(user)
        username = _current_username()
        selected_store = _resolve_store_id(request.form.get("store_id"))
        category_id = request.form.get("category") or None
        target_store_id = request.form.get("target_store_id") or None

        if action == "create":
            if not permissions["can_manage_items"]:
                return redirect(url_for("index"))
            manager.set_quantity(
                name,
                max(quantity or 0, 0),
                unit=unit or "",
                threshold=_parse_threshold_value(threshold_raw),
                category=category_id,
                store_id=selected_store,
                user=username,
            )
        elif action == "in":
            if not permissions["can_adjust_in"] or quantity is None:
                return redirect(url_for("index"))
            manager.adjust_quantity(
                name, max(quantity, 0), store_id=selected_store, user=username
            )
        elif action == "out":
            if not permissions["can_adjust_out"] or quantity is None:
                return redirect(url_for("index"))
            try:
                manager.adjust_quantity(
                    name, -max(quantity, 0), store_id=selected_store, user=username
                )
            except ValueError:
                pass
        elif action == "update":
            if not permissions["can_manage_items"]:
                return redirect(url_for("index"))
            if quantity is None:
                try:
                    current_item = manager.get_item(name)
                except KeyError:
                    return redirect(url_for("index"))
                quantity_to_set = max(current_item.quantity, 0)
            else:
                quantity_to_set = max(quantity, 0)
            manager.set_quantity(
                name,
                quantity_to_set,
                unit=unit,
                threshold=_parse_threshold_value(threshold_raw),
                category=category_id,
                store_id=selected_store,
                user=username,
            )
        elif action == "delete":
            if not permissions["can_manage_items"]:
                return redirect(url_for("index"))
            try:
                manager.delete_item(name, store_id=selected_store, user=username)
            except KeyError:
                pass
        elif action == "transfer":
            if not permissions["can_manage_items"] or quantity is None or quantity <= 0:
                return redirect(url_for("index"))
            stores_map = _list_stores()
            if not target_store_id or target_store_id not in stores_map:
                return redirect(url_for("index"))
            try:
                manager.transfer_item(
                    name,
                    quantity,
                    source_store_id=selected_store,
                    target_store_id=target_store_id,
                    user=username,
                )
            except (ValueError, KeyError):
                pass
        next_target = request.form.get("next") or request.args.get("next")
        if not _is_safe_redirect(next_target):
            next_target = request.referrer if _is_safe_redirect(request.referrer) else None
        return redirect(next_target or url_for("index"))

    return app


def _get_payload(req: Any) -> Dict[str, Any]:
    if req.is_json:
        return req.get_json(silent=True) or {}
    if req.form:
        return req.form.to_dict()
    return req.get_json(silent=True) or {}


def _parse_threshold_value(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip()
        if raw == "":
            return None
        try:
            parsed = int(raw)
        except ValueError:
            return None
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
    if parsed < 0:
        return None
    return parsed


def _inventory_report_to_xls(
    rows: Sequence[Mapping[str, Any]],
    *,
    generated_label: str,
    username: str,
    store_label: Optional[str] = None,
) -> bytes:
    fieldnames = ["门店", "分类", "商品名称", "库存数量", "单位"]
    workbook = xlwt.Workbook()
    sheet = workbook.add_sheet("库存盘点")

    title_style = xlwt.easyxf(
        "font: bold on, height 360; align: horiz center, vert center"
    )
    metadata_style = xlwt.easyxf(
        "font: height 220; align: horiz left, vert center"
    )
    header_style = xlwt.easyxf(
        "font: bold on; align: horiz center, vert center;"
        "borders: left thin, right thin, top thin, bottom thin"
    )
    text_style = xlwt.easyxf(
        "align: horiz left, vert center;"
        "borders: left thin, right thin, top thin, bottom thin"
    )
    number_style = xlwt.easyxf(
        "align: horiz center, vert center;"
        "borders: left thin, right thin, top thin, bottom thin"
    )
    merged_label_style = xlwt.easyxf(
        "align: horiz center, vert center;"
        "borders: left thin, right thin, top thin, bottom thin"
    )

    column_widths = [14, 14, 28, 12, 10]
    for index, width in enumerate(column_widths):
        sheet.col(index).width = 256 * width

    sheet.write_merge(0, 0, 0, len(fieldnames) - 1, "星选送库存盘点表", title_style)

    meta_parts = [f"制表时间：{generated_label}", f"用户：{username or '—'}"]
    if store_label:
        meta_parts.append(f"门店：{store_label}")
    sheet.write_merge(
        1,
        1,
        0,
        len(fieldnames) - 1,
        "    ".join(meta_parts),
        metadata_style,
    )

    header_row_index = 3
    for col_index, field in enumerate(fieldnames):
        sheet.write(header_row_index, col_index, field, header_style)

    if rows:
        data_start_row = header_row_index + 1
        category_col_index = fieldnames.index("分类")
        data_end_row = data_start_row + len(rows) - 1

        category_labels: List[str] = []
        for offset, entry in enumerate(rows):
            row_index = data_start_row + offset
            entry_mapping = entry if isinstance(entry, Mapping) else {}
            category_labels.append(str(entry_mapping.get("分类") or "未分类"))
            for col_index, field in enumerate(fieldnames):
                if field in {"门店", "分类"}:
                    continue
                value = entry_mapping.get(field, "")
                if field == "库存数量" and isinstance(value, (int, float)):
                    sheet.write(row_index, col_index, value, number_style)
                else:
                    sheet.write(row_index, col_index, value, text_style)

        if isinstance(rows[0], Mapping):
            default_store_value = rows[0].get("门店")
        else:
            default_store_value = ""
        store_value = store_label or str(default_store_value or "全部门店")
        sheet.write_merge(
            data_start_row,
            data_end_row,
            0,
            0,
            store_value,
            merged_label_style,
        )

        group_start = data_start_row
        current_label = category_labels[0]
        for offset, label in enumerate(category_labels[1:], start=1):
            row_index = data_start_row + offset
            if label != current_label:
                sheet.write_merge(
                    group_start,
                    row_index - 1,
                    category_col_index,
                    category_col_index,
                    current_label,
                    merged_label_style,
                )
                group_start = row_index
                current_label = label
        sheet.write_merge(
            group_start,
            data_end_row,
            category_col_index,
            category_col_index,
            current_label,
            merged_label_style,
        )

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _rows_to_xls(
    fieldnames: Sequence[str],
    rows: Iterable[Dict[str, Any]],
    *,
    metadata: Optional[Sequence[tuple[str, Any]]] = None,
) -> bytes:
    workbook = xlwt.Workbook()
    sheet = workbook.add_sheet("Sheet1")
    row_index = 0
    if metadata:
        for key, value in metadata:
            sheet.write(row_index, 0, key)
            sheet.write(row_index, 1, "" if value is None else value)
            row_index += 1
        row_index += 1
    for col_index, field in enumerate(fieldnames):
        sheet.write(row_index, col_index, field)
    row_index += 1
    for row in rows:
        for col_index, field in enumerate(fieldnames):
            value = row.get(field, "") if isinstance(row, dict) else ""
            if value is None:
                value = ""
            sheet.write(row_index, col_index, value)
        row_index += 1
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _xls_response(content: bytes, filename: str) -> Response:
    response = Response(content, mimetype="application/vnd.ms-excel")
    response.headers["Content-Disposition"] = f"attachment; filename={filename}.xls"
    return response


def _timestamped_filename(prefix: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{prefix}_{timestamp}"


def _history_meta_to_text(meta: Dict[str, Any]) -> str:
    if not meta:
        return ""
    parts = []
    for key, value in sorted(meta.items()):
        parts.append(f"{key}: {value}")
    return "; ".join(parts)


def _extract_import_rows(req: Any) -> List[Dict[str, Any]]:
    if req.files:
        upload = req.files.get("file")
        if upload is None or upload.filename == "":
            raise ValueError("Missing upload file")
        return _extract_rows_from_filestorage(upload)
    payload = req.get_json(silent=True)
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            return [row for row in items if isinstance(row, dict)]
    raise ValueError("Unsupported import payload")


def _extract_rows_from_filestorage(upload: Any) -> List[Dict[str, Any]]:
    try:
        raw_bytes = upload.read()
    finally:
        try:
            upload.close()
        except Exception:
            pass
    if not raw_bytes:
        raise ValueError("Empty file")
    filename = getattr(upload, "filename", "") or ""
    extension = Path(filename).suffix.lower()
    if extension == ".xls":
        return _parse_xls_rows(raw_bytes)
    if isinstance(raw_bytes, str):
        text = raw_bytes
        return _parse_csv_rows(text)
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return _parse_xls_rows(raw_bytes)
        except ValueError as exc:
            raise ValueError("File must be UTF-8 encoded or valid XLS") from exc
    return _parse_csv_rows(text)


def _parse_csv_rows(text: str) -> List[Dict[str, Any]]:
    reader = csv.DictReader(StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("Missing header row")
    header_keys = {_normalize_csv_key(name) for name in reader.fieldnames}
    threshold_column_present = bool(
        header_keys & _CSV_FIELD_ALIASES_NORMALIZED.get("threshold", set())
    )
    category_column_present = bool(
        header_keys & _CSV_FIELD_ALIASES_NORMALIZED.get("category", set())
    )

    rows: List[Dict[str, Any]] = []
    for row in reader:
        if not row:
            continue
        normalized = {
            _normalize_csv_key(key): value for key, value in row.items()
        }
        if not any(str(value or "").strip() for value in normalized.values()):
            continue
        record: Dict[str, Any] = {
            "name": _resolve_csv_field(normalized, "name"),
            "quantity": _resolve_csv_field(normalized, "quantity"),
            "unit": _resolve_csv_field(normalized, "unit"),
        }
        if threshold_column_present:
            record["threshold"] = _resolve_csv_field(normalized, "threshold")
        if category_column_present:
            record["category"] = _resolve_csv_field(normalized, "category")
        rows.append(record)
    return rows


def _parse_xls_rows(data: bytes) -> List[Dict[str, Any]]:
    try:
        workbook = xlrd.open_workbook(file_contents=data)
    except Exception as exc:
        raise ValueError("Invalid XLS file") from exc
    if workbook.nsheets == 0:
        raise ValueError("Missing worksheet")
    sheet = workbook.sheet_by_index(0)
    if sheet.nrows == 0:
        raise ValueError("Missing header row")
    header_values = [sheet.cell_value(0, col) for col in range(sheet.ncols)]
    header_labels = [str(value).strip() if value is not None else "" for value in header_values]
    if not any(header_labels):
        raise ValueError("Missing header row")
    header_keys = {_normalize_csv_key(label) for label in header_labels if label}
    threshold_column_present = bool(
        header_keys & _CSV_FIELD_ALIASES_NORMALIZED.get("threshold", set())
    )
    category_column_present = bool(
        header_keys & _CSV_FIELD_ALIASES_NORMALIZED.get("category", set())
    )

    rows: List[Dict[str, Any]] = []
    for row_index in range(1, sheet.nrows):
        normalized: Dict[str, Any] = {}
        for col_index, label in enumerate(header_labels):
            normalized_key = _normalize_csv_key(label)
            if not normalized_key:
                continue
            cell = sheet.cell(row_index, col_index)
            value = cell.value
            if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                processed = ""
            elif cell.ctype == xlrd.XL_CELL_NUMBER:
                processed = str(int(value)) if float(value).is_integer() else str(value)
            else:
                processed = str(value).strip()
            normalized[normalized_key] = processed
        if not any(str(value or "").strip() for value in normalized.values()):
            continue
        record: Dict[str, Any] = {
            "name": _resolve_csv_field(normalized, "name"),
            "quantity": _resolve_csv_field(normalized, "quantity"),
            "unit": _resolve_csv_field(normalized, "unit"),
        }
        if threshold_column_present:
            record["threshold"] = _resolve_csv_field(normalized, "threshold")
        if category_column_present:
            record["category"] = _resolve_csv_field(normalized, "category")
        rows.append(record)
    return rows


def _parse_import_summary(req: Any) -> Optional[Dict[str, Any]]:
    imported = req.args.get("imported")
    skipped = req.args.get("skipped")
    error = req.args.get("import_error")
    if not imported and not skipped and not error:
        return None
    summary: Dict[str, Any] = {}
    if imported:
        try:
            summary["imported"] = int(imported)
        except ValueError:
            summary["imported"] = imported
    if skipped:
        try:
            summary["skipped"] = int(skipped)
        except ValueError:
            summary["skipped"] = skipped
    if error:
        summary["error"] = True
    return summary


def _parse_date_arg(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def _resolve_history_filters(
    args: Mapping[str, str], mode_hint: Optional[str] = None
) -> tuple[str, Optional[datetime], Optional[datetime], str, str]:
    mode_raw = (mode_hint or args.get("mode") or "sku").lower()
    if mode_raw == "day":
        mode = "day"
    elif mode_raw == "month":
        mode = "month"
    else:
        mode = "sku"
    start_raw = args.get("start")
    end_raw = args.get("end")
    today_local = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    today = today_local.replace(tzinfo=None)
    if mode == "day":
        span = timedelta(days=13)
    elif mode == "month":
        span = timedelta(days=180)
    else:
        span = timedelta(days=0)
    if mode == "sku":
        default_start = today
    else:
        default_start = today - span
    start_dt = _parse_date_arg(start_raw) or default_start
    end_dt = _parse_date_arg(end_raw) or today
    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt
    start_value = start_dt.strftime("%Y-%m-%d")
    end_value = end_dt.strftime("%Y-%m-%d")
    end_boundary = end_dt + timedelta(days=1)
    return mode, start_dt, end_boundary, start_value, end_value


def _history_statistics(
    entries: Iterable[InventoryHistoryEntry],
    *,
    mode: str = "sku",
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    normalized_mode = "sku" if mode == "sku" else "day" if mode == "day" else "month"
    buckets: Dict[str, Dict[str, Any]] = {}
    ordered_entries = sorted(entries, key=lambda entry: entry.timestamp)
    for entry in ordered_entries:
        local_time = entry.timestamp.astimezone()
        naive_time = local_time.replace(tzinfo=None)
        if end and naive_time >= end:
            continue

        meta = entry.meta or {}

        def _parse_int(value: Any) -> Optional[int]:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        if normalized_mode == "sku":
            sku_name = (entry.name or "").strip() or "未命名 SKU"
            bucket = buckets.setdefault(
                sku_name,
                {
                    "label": sku_name,
                    "sku": sku_name,
                    "unit": "",
                    "category": "",
                    "store_name": "",
                    "inbound": 0,
                    "outbound": 0,
                    "last_activity": None,
                    "ending_quantity": None,
                    "ending_time": None,
                },
            )
            unit = str(meta.get("unit") or "").strip()
            if unit:
                bucket["unit"] = unit
            category_name = str(meta.get("category_name") or meta.get("category_id") or "").strip()
            if category_name:
                bucket["category"] = category_name
            store_name = str(meta.get("store_name") or meta.get("store_id") or "").strip()
            if store_name:
                bucket["store_name"] = store_name

            result_quantity = _parse_int(meta.get("new_quantity"))
            if result_quantity is None:
                if entry.action == "create":
                    result_quantity = _parse_int(meta.get("quantity"))
                elif entry.action == "delete":
                    result_quantity = 0
                else:
                    previous_quantity = _parse_int(meta.get("previous_quantity"))
                    delta_value = _parse_int(meta.get("delta"))
                    if previous_quantity is not None and delta_value is not None:
                        if entry.action in ("in", "set") and delta_value >= 0:
                            result_quantity = previous_quantity + abs(delta_value)
                        elif entry.action in ("out", "set"):
                            result_quantity = max(previous_quantity - abs(delta_value), 0)
                    if result_quantity is None:
                        result_quantity = previous_quantity
            if result_quantity is not None:
                last_time = bucket.get("ending_time")
                if last_time is None or naive_time >= last_time:
                    bucket["ending_quantity"] = result_quantity
                    bucket["ending_time"] = naive_time

        if start and naive_time < start:
            continue

        inbound_delta = 0
        outbound_delta = 0
        include_entry = True
        if entry.action == "in":
            delta_value = _parse_int(meta.get("delta"))
            if delta_value is None:
                include_entry = False
            else:
                inbound_delta = abs(delta_value)
        elif entry.action == "out":
            delta_value = _parse_int(meta.get("delta"))
            if delta_value is None:
                include_entry = False
            else:
                outbound_delta = abs(delta_value)
        elif entry.action == "set":
            delta_value = _parse_int(meta.get("delta"))
            if delta_value in (None, 0):
                include_entry = False
            elif delta_value > 0:
                inbound_delta = delta_value
            else:
                outbound_delta = abs(delta_value)
        elif entry.action == "create":
            quantity_value = _parse_int(meta.get("quantity"))
            if quantity_value is None or quantity_value <= 0:
                include_entry = False
            else:
                inbound_delta = quantity_value
        else:
            include_entry = False

        if not include_entry:
            continue

        if normalized_mode == "day":
            label = local_time.strftime("%Y-%m-%d")
            sort_key = datetime(local_time.year, local_time.month, local_time.day)
            bucket = buckets.setdefault(
                label,
                {
                    "label": label,
                    "inbound": 0,
                    "outbound": 0,
                    "sort_key": sort_key,
                },
            )
        elif normalized_mode == "month":
            label = local_time.strftime("%Y-%m")
            sort_key = datetime(local_time.year, local_time.month, 1)
            bucket = buckets.setdefault(
                label,
                {
                    "label": label,
                    "inbound": 0,
                    "outbound": 0,
                    "sort_key": sort_key,
                },
            )
        bucket["inbound"] += inbound_delta
        bucket["outbound"] += outbound_delta
        if normalized_mode == "sku":
            last_activity = bucket.get("last_activity")
            if last_activity is None or naive_time > last_activity:
                bucket["last_activity"] = naive_time

    rows: List[Dict[str, Any]] = []
    if normalized_mode == "sku":
        for bucket in buckets.values():
            rows.append(
                {
                    "label": bucket["label"],
                    "sku": bucket["sku"],
                    "category": bucket.get("category", ""),
                    "unit": bucket.get("unit", ""),
                    "store_name": bucket.get("store_name", ""),
                    "inbound": bucket["inbound"],
                    "outbound": bucket["outbound"],
                    "net": bucket["inbound"] - bucket["outbound"],
                    "last_activity": bucket.get("last_activity"),
                    "ending_quantity": bucket.get("ending_quantity"),
                }
            )
        rows.sort(
            key=lambda item: (
                -(item["inbound"] + item["outbound"]),
                item["sku"].lower(),
            )
        )
    else:
        for bucket in sorted(buckets.values(), key=lambda item: item["sort_key"]):
            rows.append(
                {
                    "label": bucket["label"],
                    "inbound": bucket["inbound"],
                    "outbound": bucket["outbound"],
                    "net": bucket["inbound"] - bucket["outbound"],
                }
            )
    return rows


def _recent_activity(
    entries: list[InventoryHistoryEntry], limit: Optional[int] = 20
) -> list[Dict[str, Any]]:
    def _unit_suffix(unit: str) -> str:
        return f" {unit}" if unit else ""

    events: list[Dict[str, Any]] = []
    for entry in entries:
        meta = entry.meta
        unit = str(meta.get("unit") or "")
        suffix = _unit_suffix(unit)
        badge = "secondary"
        label = "动态"
        details: List[str] = []
        operator = str(meta.get("user") or "系统")
        store_name = str(meta.get("store_name") or meta.get("store_id") or "")
        category_name = str(meta.get("category_name") or meta.get("category_id") or "")
        if store_name:
            details.append(f"门店：{store_name}")
        if category_name:
            details.append(f"分类：{category_name}")

        if entry.action == "create":
            badge = "info"
            label = "新增"
            quantity = meta.get("quantity")
            if quantity is not None:
                details.append(f"初始数量 {quantity}{suffix}".strip())
            if unit:
                details.append(f"单位：{unit}")
        elif entry.action == "set":
            badge = "primary"
            label = "盘点"
            new_quantity = meta.get("new_quantity")
            previous_quantity = meta.get("previous_quantity")
            delta = meta.get("delta")
            if new_quantity is not None and previous_quantity is not None:
                details.append(
                    f"库存 {previous_quantity}{suffix} → {new_quantity}{suffix}".strip()
                )
            elif new_quantity is not None:
                details.append(f"库存调整至 {new_quantity}{suffix}".strip())
            if delta:
                sign = "+" if delta > 0 else ""
                details.append(f"差值 {sign}{delta}")
            previous_unit = meta.get("previous_unit")
            if previous_unit and previous_unit != unit:
                details.append(f"单位 {previous_unit} → {unit or '（空）'}")
        elif entry.action == "in":
            badge = "success"
            label = "入库"
            delta = meta.get("delta")
            new_quantity = meta.get("new_quantity")
            if delta is not None:
                details.append(f"数量 +{delta}{suffix}".strip())
            if new_quantity is not None:
                details.append(f"现有库存 {new_quantity}{suffix}".strip())
            if meta.get("transfer"):
                label = "调入"
                source_store = meta.get("transfer_source_name") or meta.get(
                    "transfer_source_id"
                )
                if source_store:
                    details.append(f"来源门店：{source_store}")
        elif entry.action == "out":
            badge = "warning"
            label = "出库"
            delta = meta.get("delta")
            new_quantity = meta.get("new_quantity")
            if delta is not None:
                details.append(f"数量 -{delta}{suffix}".strip())
            if new_quantity is not None:
                details.append(f"现有库存 {new_quantity}{suffix}".strip())
            if meta.get("transfer"):
                label = "调出"
                target_store = meta.get("transfer_target_name") or meta.get(
                    "transfer_target_id"
                )
                if target_store:
                    details.append(f"调往门店：{target_store}")
        elif entry.action == "delete":
            badge = "danger"
            label = "删除"
            previous_quantity = meta.get("previous_quantity")
            if previous_quantity is not None:
                details.append(f"移除前库存 {previous_quantity}{suffix}".strip())
            if unit:
                details.append(f"单位：{unit}")

        events.append(
            {
                "type": label,
                "timestamp": entry.timestamp,
                "name": entry.name,
                "badge": badge,
                "details": details,
                "user": operator,
            }
        )

    events.sort(key=lambda event: event["timestamp"], reverse=True)
    if limit is not None:
        return events[:limit]
    return events


def _demo_timeline_events() -> list[Dict[str, Any]]:
    """Provide illustrative timeline events when no history exists."""

    now = datetime.now(timezone.utc)
    return [
        {
            "type": "入库",
            "badge": "success",
            "timestamp": now - timedelta(hours=2, minutes=15),
            "name": "黑曜石深烘咖啡豆",
            "user": "王雪",
            "details": [
                "数量 +80 袋",
                "现有库存 230 袋",
                "门店：旗舰店",
                "分类：咖啡豆",
            ],
        },
        {
            "type": "出库",
            "badge": "warning",
            "timestamp": now - timedelta(hours=5, minutes=40),
            "name": "燕麦奶",
            "user": "陈刚",
            "details": [
                "数量 -24 箱",
                "现有库存 96 箱",
                "门店：旗舰店",
                "分类：饮品原料",
            ],
        },
        {
            "type": "调拨",
            "badge": "neutral",
            "timestamp": now - timedelta(hours=9, minutes=5),
            "name": "可可碎粒",
            "user": "李倩",
            "details": [
                "从旗舰店 → 益田路门店",
                "数量 15 千克",
                "调拨后库存：旗舰店 40 千克 / 益田路 28 千克",
            ],
        },
        {
            "type": "盘点",
            "badge": "neutral",
            "timestamp": now - timedelta(days=1, hours=1),
            "name": "黑芝麻麻薯",
            "user": "李敏",
            "details": [
                "库存 180 盒 → 172 盒",
                "差值 -8",
                "门店：旗舰店",
                "分类：烘焙",
            ],
        },
        {
            "type": "新增",
            "badge": "neutral",
            "timestamp": now - timedelta(days=2, hours=3),
            "name": "草莓奶昔基底",
            "user": "系统",
            "details": [
                "初始数量 60 瓶",
                "门店：旗舰店",
                "分类：饮品原料",
            ],
        },
        {
            "type": "出库",
            "badge": "warning",
            "timestamp": now - timedelta(days=2, hours=6, minutes=10),
            "name": "榛果糖浆",
            "user": "赵彤",
            "details": [
                "数量 -12 瓶",
                "用于门店促销活动",
                "现有库存 48 瓶",
            ],
        },
        {
            "type": "调整",
            "badge": "success",
            "timestamp": now - timedelta(days=3, minutes=45),
            "name": "手工饼干礼盒",
            "user": "周琪",
            "details": [
                "补录供应商赠品 +30 盒",
                "新的库存基线 120 盒",
                "门店：益田路门店",
            ],
        },
    ]


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=5000, debug=True)
