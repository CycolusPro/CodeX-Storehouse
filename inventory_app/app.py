"""Flask application providing a minimal inventory management API and UI."""
from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Mapping
from functools import wraps
from urllib.parse import urlsplit, urljoin
import os

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

    def _resolve_store_id(store_id: Optional[str] = None) -> str:
        stores = _list_stores()
        if store_id and store_id in stores:
            session["store_id"] = store_id
            return store_id
        selected = session.get("store_id")
        if selected in stores:
            return selected
        if stores:
            first = next(iter(stores))
            session["store_id"] = first
            return first
        created = manager.create_store("默认门店")
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
            return jsonify({"error": "Unauthorized"}), 401
        next_target = request.full_path if request.query_string else request.path
        return redirect(url_for("login", next=next_target))

    def _forbidden_response():
        if _is_api_request():
            return jsonify({"error": "Forbidden"}), 403
        abort(403)

    @app.before_request
    def load_current_user() -> None:
        username = session.get("user")
        g.current_user = None
        if not username:
            return
        try:
            g.current_user = user_manager.get_user(username)
        except KeyError:
            session.pop("user", None)

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
            return redirect(url_for("index"))
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
        try:
            manager.delete_category(category_id, cascade=cascade, user=_current_username())
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
        }
        def _parse_positive_int(value: Optional[str], default: int) -> int:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return default
            return parsed if parsed > 0 else default

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

        history_entries = manager.list_history(store_id=selected_store)
        timeline_sku = (request.args.get("timeline_sku") or "").strip()
        if timeline_sku:
            filtered_history = [
                entry for entry in history_entries if entry.name == timeline_sku
            ]
        else:
            filtered_history = history_entries

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
        low_stock_items = [
            item
            for item in items_sorted
            if item.threshold is not None and item.quantity <= item.threshold
        ]
        return render_template(
            "index.html",
            items=items,
            summary=summary,
            timeline=timeline,
            import_summary=import_summary,
            low_stock_items=low_stock_items,
            stores=stores,
            categories=categories,
            selected_store=selected_store,
            selected_category=selected_category,
            inventory_pagination=inventory_pagination,
            timeline_pagination=timeline_pagination,
            timeline_sku=timeline_sku,
            timeline_sku_options=timeline_sku_options,
            preserved_query=preserved_query,
            build_query=build_query,
            inventory_search=inventory_search,
        )

    @app.post("/history/clear")
    @role_required("super_admin")
    def clear_history() -> Any:
        manager.clear_history()
        flash("已清除最近动态记录", "success")
        return redirect(url_for("index"))

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

    @app.get("/api/items/template")
    @login_required
    def download_template() -> Response:
        rows = [
            {"名称": "示例SKU", "数量": 50, "单位": "件", "阈值提醒": 10, "库存分类": "未分类"},
        ]
        content = _rows_to_csv(["名称", "数量", "单位", "阈值提醒", "库存分类"], rows)
        filename = _timestamped_filename("inventory_template")
        return _csv_response(content, filename)

    @app.get("/api/items/export")
    @login_required
    def export_inventory() -> Response:
        selected_store = _resolve_store_id(request.args.get("store_id"))
        rows = manager.export_items(store_id=selected_store)
        header_map = [
            ("store_id", "门店 ID"),
            ("store_name", "门店名称"),
            ("category_id", "分类 ID"),
            ("category_name", "分类名称"),
            ("name", "SKU 名称"),
            ("quantity", "库存数量"),
            ("unit", "单位"),
            ("created_at", "创建时间"),
            ("last_in", "最近入库时间"),
            ("last_in_delta", "最近入库数量"),
            ("last_out", "最近出库时间"),
            ("last_out_delta", "最近出库数量"),
            ("threshold", "库存阈值"),
        ]
        localized_rows = []
        for row in rows:
            localized_row = {}
            for key, label in header_map:
                value = row.get(key, "") if isinstance(row, dict) else ""
                localized_row[label] = "" if value is None else value
            localized_rows.append(localized_row)
        fieldnames = [label for _, label in header_map]
        content = _rows_to_csv(fieldnames, localized_rows)
        filename = _timestamped_filename("inventory_export")
        return _csv_response(content, filename)

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
        content = _rows_to_csv(fieldnames, rows)
        filename = _timestamped_filename("inventory_history")
        return _csv_response(content, filename)

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
        content = _rows_to_csv(fieldnames, csv_rows, metadata=metadata)
        filename = _timestamped_filename("inventory_history_stats")
        return _csv_response(content, filename)

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
        return render_template("users.html", users=users)

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
        return redirect(url_for("index"))

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


def _rows_to_csv(
    fieldnames: Sequence[str],
    rows: Iterable[Dict[str, Any]],
    *,
    metadata: Optional[Sequence[tuple[str, Any]]] = None,
) -> str:
    buffer = StringIO()
    buffer.write("\ufeff")
    if metadata:
        meta_writer = csv.writer(buffer)
        for key, value in metadata:
            meta_writer.writerow([key, "" if value is None else value])
        meta_writer.writerow([])
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        safe_row = {}
        for field in fieldnames:
            value = row.get(field, "") if isinstance(row, dict) else ""
            if value is None:
                value = ""
            safe_row[field] = value
        writer.writerow(safe_row)
    return buffer.getvalue()


def _csv_response(content: str, filename: str) -> Response:
    response = Response(content, mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = f"attachment; filename={filename}.csv"
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
    if isinstance(raw_bytes, str):
        text = raw_bytes
    else:
        try:
            text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("File must be UTF-8 encoded") from exc
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


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=5000, debug=True)
