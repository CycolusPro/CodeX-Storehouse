"""Flask application providing a minimal inventory management API and UI."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, redirect, render_template, request, url_for

from .inventory import (
    InventoryHistoryEntry,
    InventoryItem,
    InventoryManager,
)


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
        history_entries = manager.list_history(limit=20)
        timeline = _recent_activity(history_entries)
        return render_template(
            "index.html",
            items=items,
            summary=summary,
            timeline=timeline,
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
        item = manager.set_quantity(name, quantity, unit=unit)
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
        try:
            manager.get_item(name)
        except KeyError as exc:
            return {"error": str(exc)}, 404
        try:
            item = manager.set_quantity(name, quantity, unit=str(unit).strip() if unit is not None else None)
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

    @app.post("/submit")
    def submit_form() -> Any:
        action = request.form.get("action")
        name = request.form.get("name", "").strip()
        quantity_raw = request.form.get("quantity")
        unit_raw = request.form.get("unit")
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
            manager.set_quantity(name, max(quantity or 0, 0), unit=unit or "")
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
                manager.set_quantity(name, max(quantity, 0), unit=unit)
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
