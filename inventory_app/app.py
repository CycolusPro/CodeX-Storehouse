"""Flask application providing a minimal inventory management API and UI."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from flask import Flask, jsonify, redirect, render_template, request, url_for

from .inventory import InventoryItem, InventoryManager


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
        items = list(manager.list_items().values())
        total_quantity = sum(item.quantity for item in items)
        latest_in = _latest_timestamp(items, key="last_in")
        latest_out = _latest_timestamp(items, key="last_out")
        summary = {
            "total_items": len(items),
            "total_quantity": total_quantity,
            "latest_in": latest_in,
            "latest_out": latest_out,
        }
        timeline = _recent_activity(items)
        return render_template("index.html", items=items, summary=summary, timeline=timeline)

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
        item = manager.set_quantity(name, quantity)
        return jsonify(item.to_dict()), 201

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

    @app.post("/submit")
    def submit_form() -> Any:
        action = request.form.get("action")
        name = request.form.get("name", "").strip()
        quantity_raw = request.form.get("quantity", "0")
        if not name:
            return redirect(url_for("index"))
        try:
            quantity = int(quantity_raw)
        except ValueError:
            return redirect(url_for("index"))

        if action == "create":
            manager.set_quantity(name, max(quantity, 0))
        elif action == "in":
            manager.adjust_quantity(name, max(quantity, 0))
        elif action == "out":
            try:
                manager.adjust_quantity(name, -max(quantity, 0))
            except ValueError:
                pass
        return redirect(url_for("index"))

    return app


def _get_payload(req: Any) -> Dict[str, Any]:
    if req.is_json:
        return req.get_json(silent=True) or {}
    if req.form:
        return req.form.to_dict()
    return req.get_json(silent=True) or {}


def _recent_activity(items: list[InventoryItem], limit: int = 5) -> list[Dict[str, Any]]:
    events: list[Dict[str, Any]] = []
    for item in items:
        if item.last_in is not None:
            events.append(
                {
                    "type": "入库",
                    "timestamp": item.last_in,
                    "name": item.name,
                    "badge": "success",
                }
            )
        if item.last_out is not None:
            events.append(
                {
                    "type": "出库",
                    "timestamp": item.last_out,
                    "name": item.name,
                    "badge": "warning",
                }
            )
    events.sort(key=lambda event: event["timestamp"], reverse=True)
    return events[:limit]


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=5000, debug=True)
