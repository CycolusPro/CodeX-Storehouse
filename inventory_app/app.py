"""Flask application providing a minimal inventory management API and UI."""
from __future__ import annotations

import csv
import json
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
    return str(value).strip().lower()


_CSV_FIELD_ALIASES: Dict[str, set[str]] = {
    "name": {"name", "名称"},
    "quantity": {"quantity", "数量"},
    "unit": {"unit", "单位"},
    "threshold": {"threshold", "阈值提醒", "阈值"},
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
        }

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
        items = sorted(manager.list_items().values(), key=lambda item: item.name)
        total_quantity = sum(item.quantity for item in items)
        latest_in = _latest_timestamp(items, key="last_in")
        latest_out = _latest_timestamp(items, key="last_out")
        summary = {
            "total_items": len(items),
            "total_quantity": total_quantity,
            "latest_in": latest_in,
            "latest_out": latest_out,
        }
        history_entries = manager.list_history(limit=5)
        timeline = _recent_activity(history_entries, limit=5)
        import_summary = _parse_import_summary(request)
        low_stock_items = [
            item for item in items if item.threshold is not None and item.quantity <= item.threshold
        ]
        return render_template(
            "index.html",
            items=items,
            summary=summary,
            timeline=timeline,
            import_summary=import_summary,
            low_stock_items=low_stock_items,
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
        items = manager.list_items()
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
        item = manager.set_quantity(
            name, quantity, unit=unit, threshold=threshold, user=_current_username()
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
        try:
            manager.get_item(name)
        except KeyError as exc:
            return {"error": str(exc)}, 404
        try:
            item = manager.set_quantity(
                name,
                quantity,
                unit=str(unit).strip() if unit is not None else None,
                threshold=threshold,
                keep_threshold=not threshold_provided,
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
        item = manager.adjust_quantity(name, delta, user=_current_username())
        return jsonify(item.to_dict())

    @app.post("/api/items/<string:name>/out")
    @login_required
    def stock_out(name: str) -> Any:
        payload = _get_payload(request)
        delta = int(payload.get("quantity", 0))
        if delta <= 0:
            return {"error": "Quantity must be greater than zero"}, 400
        try:
            item = manager.adjust_quantity(name, -delta, user=_current_username())
        except ValueError as exc:
            return {"error": str(exc)}, 400
        return jsonify(item.to_dict())

    @app.delete("/api/items/<string:name>")
    @role_required("admin", "super_admin")
    def delete_item(name: str) -> Any:
        try:
            manager.delete_item(name, user=_current_username())
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
        history_entries = manager.list_history(limit=limit)
        return jsonify([entry.to_dict() for entry in history_entries])

    @app.get("/api/items/template")
    @login_required
    def download_template() -> Response:
        rows = [
            {"名称": "示例SKU", "数量": 50, "单位": "件", "阈值提醒": 10},
        ]
        content = _rows_to_csv(["名称", "数量", "单位", "阈值提醒"], rows)
        filename = _timestamped_filename("inventory_template")
        return _csv_response(content, filename)

    @app.get("/api/items/export")
    @login_required
    def export_inventory() -> Response:
        rows = manager.export_items()
        fieldnames = [
            "name",
            "quantity",
            "unit",
            "created_at",
            "last_in",
            "last_in_delta",
            "last_out",
            "last_out_delta",
            "threshold",
        ]
        content = _rows_to_csv(fieldnames, rows)
        filename = _timestamped_filename("inventory_export")
        return _csv_response(content, filename)

    @app.get("/api/history/export")
    @login_required
    def export_history() -> Response:
        entries = manager.list_history()
        action_labels = {
            "in": "入库",
            "out": "出库",
            "create": "新增",
            "set": "盘点",
            "delete": "删除",
        }
        rows = []
        for entry in entries:
            local_time = entry.timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            user = str(entry.meta.get("user") or "系统")
            rows.append(
                {
                    "时间": local_time,
                    "操作类型": action_labels.get(entry.action, entry.action or "—"),
                    "SKU 名称": entry.name,
                    "操作用户": user,
                }
            )
        fieldnames = ["时间", "操作类型", "SKU 名称", "操作用户"]
        content = _rows_to_csv(fieldnames, rows)
        filename = _timestamped_filename("inventory_history")
        return _csv_response(content, filename)

    @app.get("/analytics")
    @role_required("admin", "super_admin")
    def analytics_dashboard() -> str:
        mode, start_dt, end_dt, start_value, end_value = _resolve_history_filters(request.args)
        entries = manager.list_history()
        stats_rows = _history_statistics(entries, mode=mode, start=start_dt, end=end_dt)
        total_inbound = sum(row["inbound"] for row in stats_rows)
        total_outbound = sum(row["outbound"] for row in stats_rows)
        totals = {
            "inbound": total_inbound,
            "outbound": total_outbound,
            "net": total_inbound - total_outbound,
        }
        chart_data = {
            "labels": [row["label"] for row in stats_rows],
            "inbound": [row["inbound"] for row in stats_rows],
            "outbound": [row["outbound"] for row in stats_rows],
        }
        export_params = {"mode": mode}
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
            chart_data=chart_data,
            has_data=bool(stats_rows),
            export_url=export_url,
            range_label=range_label,
        )

    @app.get("/api/history/stats/export")
    @role_required("admin", "super_admin")
    def export_history_stats() -> Response:
        mode, start_dt, end_dt, start_value, end_value = _resolve_history_filters(request.args)
        entries = manager.list_history()
        stats_rows = _history_statistics(entries, mode=mode, start=start_dt, end=end_dt)
        csv_rows = [
            {
                "时间": row["label"],
                "入库数量": row["inbound"],
                "出库数量": row["outbound"],
                "净变动": row["net"],
            }
            for row in stats_rows
        ]
        if not csv_rows:
            csv_rows.append(
                {
                    "时间": f"{start_value} 至 {end_value}",
                    "入库数量": 0,
                    "出库数量": 0,
                    "净变动": 0,
                }
            )
        else:
            total_inbound = sum(row["入库数量"] for row in csv_rows)
            total_outbound = sum(row["出库数量"] for row in csv_rows)
            csv_rows.append(
                {
                    "时间": "合计",
                    "入库数量": total_inbound,
                    "出库数量": total_outbound,
                    "净变动": total_inbound - total_outbound,
                }
            )
        fieldnames = ["时间", "入库数量", "出库数量", "净变动"]
        content = _rows_to_csv(fieldnames, csv_rows)
        filename = _timestamped_filename("inventory_history_stats")
        return _csv_response(content, filename)

    @app.post("/api/items/import")
    @role_required("admin", "super_admin")
    def import_inventory_api() -> Any:
        try:
            rows = _extract_import_rows(request)
        except ValueError as exc:
            return {"error": str(exc)}, 400
        imported = manager.import_items(rows, user=_current_username())
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
        imported = manager.import_items(rows, user=_current_username())
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

        if action == "create":
            if not permissions["can_manage_items"]:
                return redirect(url_for("index"))
            manager.set_quantity(
                name,
                max(quantity or 0, 0),
                unit=unit or "",
                threshold=_parse_threshold_value(threshold_raw),
                user=username,
            )
        elif action == "in":
            if not permissions["can_adjust_in"] or quantity is None:
                return redirect(url_for("index"))
            manager.adjust_quantity(name, max(quantity, 0), user=username)
        elif action == "out":
            if not permissions["can_adjust_out"] or quantity is None:
                return redirect(url_for("index"))
            try:
                manager.adjust_quantity(name, -max(quantity, 0), user=username)
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
                user=username,
            )
        elif action == "delete":
            if not permissions["can_manage_items"]:
                return redirect(url_for("index"))
            try:
                manager.delete_item(name, user=username)
            except KeyError:
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


def _rows_to_csv(fieldnames: Sequence[str], rows: Iterable[Dict[str, Any]]) -> str:
    buffer = StringIO()
    buffer.write("\ufeff")
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


def _resolve_history_filters(args: Mapping[str, str], mode_hint: Optional[str] = None) -> tuple[str, Optional[datetime], Optional[datetime], str, str]:
    mode_raw = mode_hint or args.get("mode", "month")
    mode = "day" if mode_raw == "day" else "month"
    start_raw = args.get("start")
    end_raw = args.get("end")
    today_local = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    today = today_local.replace(tzinfo=None)
    span = timedelta(days=13) if mode == "day" else timedelta(days=180)
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
    mode: str = "month",
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    normalized_mode = "day" if mode == "day" else "month"
    buckets: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        if entry.action not in {"in", "out"}:
            continue
        local_time = entry.timestamp.astimezone()
        naive_time = local_time.replace(tzinfo=None)
        if start and naive_time < start:
            continue
        if end and naive_time >= end:
            continue
        if normalized_mode == "day":
            label = local_time.strftime("%Y-%m-%d")
            sort_key = datetime(local_time.year, local_time.month, local_time.day)
        else:
            label = local_time.strftime("%Y-%m")
            sort_key = datetime(local_time.year, local_time.month, 1)
        bucket = buckets.setdefault(
            label,
            {"label": label, "inbound": 0, "outbound": 0, "sort_key": sort_key},
        )
        delta = entry.meta.get("delta")
        try:
            delta_value = int(delta)
        except (TypeError, ValueError):
            continue
        if delta_value < 0:
            delta_value = abs(delta_value)
        if entry.action == "in":
            bucket["inbound"] += delta_value
        else:
            bucket["outbound"] += delta_value
    rows: List[Dict[str, Any]] = []
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
def _recent_activity(entries: list[InventoryHistoryEntry], limit: int = 20) -> list[Dict[str, Any]]:
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
        elif entry.action == "out":
            badge = "warning"
            label = "出库"
            delta = meta.get("delta")
            new_quantity = meta.get("new_quantity")
            if delta is not None:
                details.append(f"数量 -{delta}{suffix}".strip())
            if new_quantity is not None:
                details.append(f"现有库存 {new_quantity}{suffix}".strip())
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
    return events[:limit]


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=5000, debug=True)
