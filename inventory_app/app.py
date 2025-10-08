"""Flask application providing a minimal inventory management API and UI."""
from __future__ import annotations

import csv
import json
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for

from .inventory import (
    InventoryHistoryEntry,
    InventoryItem,
    InventoryManager,
)


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


def create_app(storage_path: str | Path = "inventory_data.json") -> Flask:
    app = Flask(__name__)
    manager = InventoryManager(storage_path=storage_path)

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

    @app.get("/api/items")
    def list_items() -> Any:
        items = manager.list_items()
        return jsonify([item.to_dict() for item in items.values()])

    @app.post("/api/items")
    def add_item() -> Any:
        payload = _get_payload(request)
        name = payload.get("name")
        quantity = int(payload.get("quantity", 0))
        if not name:
            return {"error": "Missing item name"}, 400
        unit = str(payload.get("unit", "") or "").strip()
        threshold = _parse_threshold_value(payload.get("threshold"))
        item = manager.set_quantity(name, quantity, unit=unit, threshold=threshold)
        return jsonify(item.to_dict()), 201

    @app.put("/api/items/<string:name>")
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
            )
        except ValueError as exc:
            return {"error": str(exc)}, 400
        return jsonify(item.to_dict())

    @app.post("/api/items/<string:name>/in")
    def stock_in(name: str) -> Any:
        payload = _get_payload(request)
        delta = int(payload.get("quantity", 0))
        if delta <= 0:
            return {"error": "Quantity must be greater than zero"}, 400
        item = manager.adjust_quantity(name, delta)
        return jsonify(item.to_dict())

    @app.post("/api/items/<string:name>/out")
    def stock_out(name: str) -> Any:
        payload = _get_payload(request)
        delta = int(payload.get("quantity", 0))
        if delta <= 0:
            return {"error": "Quantity must be greater than zero"}, 400
        try:
            item = manager.adjust_quantity(name, -delta)
        except ValueError as exc:
            return {"error": str(exc)}, 400
        return jsonify(item.to_dict())

    @app.delete("/api/items/<string:name>")
    def delete_item(name: str) -> Any:
        try:
            manager.delete_item(name)
        except KeyError as exc:
            return {"error": str(exc)}, 404
        return "", 204

    @app.get("/api/history")
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
    def download_template() -> Response:
        rows = [
            {"名称": "示例SKU", "数量": 50, "单位": "件", "阈值提醒": 10},
        ]
        content = _rows_to_csv(["名称", "数量", "单位", "阈值提醒"], rows)
        filename = _timestamped_filename("inventory_template")
        return _csv_response(content, filename)

    @app.get("/api/items/export")
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
    def export_history() -> Response:
        entries = manager.list_history()
        rows = []
        for entry in entries:
            rows.append(
                {
                    "timestamp": entry.timestamp.astimezone().isoformat(),
                    "action": entry.action,
                    "name": entry.name,
                    "details": _history_meta_to_text(entry.meta),
                    "meta": json.dumps(entry.meta, ensure_ascii=False, sort_keys=True),
                }
            )
        fieldnames = ["timestamp", "action", "name", "details", "meta"]
        content = _rows_to_csv(fieldnames, rows)
        filename = _timestamped_filename("inventory_history")
        return _csv_response(content, filename)

    @app.post("/api/items/import")
    def import_inventory_api() -> Any:
        try:
            rows = _extract_import_rows(request)
        except ValueError as exc:
            return {"error": str(exc)}, 400
        imported = manager.import_items(rows)
        return jsonify(
            {
                "imported": [item.to_dict() for item in imported],
                "count": len(imported),
            }
        )

    @app.post("/import")
    def import_inventory_form() -> Any:
        upload = request.files.get("file")
        if upload is None or upload.filename == "":
            return redirect(url_for("index", import_error=1))
        try:
            rows = _extract_rows_from_filestorage(upload)
        except ValueError:
            return redirect(url_for("index", import_error=1))
        total_rows = len(rows)
        imported = manager.import_items(rows)
        return redirect(
            url_for(
                "index",
                imported=len(imported),
                skipped=max(total_rows - len(imported), 0),
            )
        )

    @app.post("/submit")
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

        if action == "create":
            manager.set_quantity(
                name,
                max(quantity or 0, 0),
                unit=unit or "",
                threshold=_parse_threshold_value(threshold_raw),
            )
        elif action == "in":
            if quantity is not None:
                manager.adjust_quantity(name, max(quantity, 0))
        elif action == "out":
            if quantity is not None:
                try:
                    manager.adjust_quantity(name, -max(quantity, 0))
                except ValueError:
                    pass
        elif action == "update":
            if quantity is not None:
                manager.set_quantity(
                    name,
                    max(quantity, 0),
                    unit=unit,
                    threshold=_parse_threshold_value(threshold_raw),
                )
        elif action == "delete":
            try:
                manager.delete_item(name)
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
            }
        )

    events.sort(key=lambda event: event["timestamp"], reverse=True)
    return events[:limit]


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=5000, debug=True)
