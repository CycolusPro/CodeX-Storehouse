"""Inventory management logic for simple Flask app."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Optional
import json


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


def _serialize_timestamp(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class InventoryItem:
    """Represents a single inventory item."""

    name: str
    quantity: int = 0
    unit: str = ""
    last_in: Optional[datetime] = None
    last_out: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "quantity": self.quantity,
            "unit": self.unit,
            "last_in": _serialize_timestamp(self.last_in),
            "last_out": _serialize_timestamp(self.last_out),
        }


@dataclass
class InventoryManager:
    """Manages inventory data persisted to a JSON file."""

    storage_path: Path
    _lock: RLock = field(default_factory=RLock, init=False)

    def __post_init__(self) -> None:
        self.storage_path = Path(self.storage_path)
        if not self.storage_path.exists():
            self._write_data({})

    def list_items(self) -> Dict[str, InventoryItem]:
        data = self._read_data()
        items: Dict[str, InventoryItem] = {}
        for name, record in data.items():
            normalized = self._coerce_record(record)
            items[name] = self._record_to_item(name, normalized)
        return items

    def get_item(self, name: str) -> InventoryItem:
        items = self.list_items()
        if name not in items:
            raise KeyError(f"Item '{name}' not found")
        return items[name]

    def set_quantity(self, name: str, quantity: int, unit: Optional[str] = None) -> InventoryItem:
        if quantity < 0:
            raise ValueError("Quantity cannot be negative")
        with self._lock:
            data = self._read_data()
            record = self._coerce_record(data.get(name))
            previous_quantity = record["quantity"]
            record["quantity"] = quantity
            if unit is not None:
                record["unit"] = str(unit).strip()
            now = _now()
            if quantity > previous_quantity:
                record["last_in"] = _serialize_timestamp(now)
            elif quantity < previous_quantity:
                record["last_out"] = _serialize_timestamp(now)
            elif quantity > 0 and record["last_in"] is None and record["last_out"] is None:
                record["last_in"] = _serialize_timestamp(now)
            data[name] = record
            self._write_data(data)
            item = self._record_to_item(name, record)
        return item

    def adjust_quantity(self, name: str, delta: int) -> InventoryItem:
        with self._lock:
            data = self._read_data()
            record = self._coerce_record(data.get(name))
            current_quantity = record["quantity"]
            new_quantity = current_quantity + delta
            if new_quantity < 0:
                raise ValueError("Insufficient stock for this operation")
            record["quantity"] = new_quantity
            now = _now()
            if delta > 0:
                record["last_in"] = _serialize_timestamp(now)
            elif delta < 0:
                record["last_out"] = _serialize_timestamp(now)
            data[name] = record
            self._write_data(data)
            item = self._record_to_item(name, record)
        return item

    def delete_item(self, name: str) -> None:
        with self._lock:
            data = self._read_data()
            if name not in data:
                raise KeyError(f"Item '{name}' not found")
            del data[name]
            self._write_data(data)

    def _read_data(self) -> Dict[str, Any]:
        with self._lock:
            if not self.storage_path.exists():
                return {}
            raw = self.storage_path.read_text(encoding="utf-8") or "{}"
            return json.loads(raw)

    def _write_data(self, data: Dict[str, Any]) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.storage_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        temp_path.replace(self.storage_path)

    @staticmethod
    def _coerce_record(raw: Any) -> Dict[str, Any]:
        if isinstance(raw, dict):
            quantity = int(raw.get("quantity", 0))
            raw_unit = raw.get("unit", "")
            unit = "" if raw_unit is None else str(raw_unit).strip()
            last_in = raw.get("last_in")
            last_out = raw.get("last_out")
        else:
            quantity = int(raw or 0)
            unit = ""
            last_in = None
            last_out = None
        return {
            "quantity": quantity,
            "unit": unit,
            "last_in": last_in,
            "last_out": last_out,
        }

    @staticmethod
    def _record_to_item(name: str, record: Dict[str, Any]) -> InventoryItem:
        return InventoryItem(
            name=name,
            quantity=int(record.get("quantity", 0)),
            unit=str(record.get("unit", "") or "").strip(),
            last_in=_parse_timestamp(record.get("last_in")),
            last_out=_parse_timestamp(record.get("last_out")),
        )
