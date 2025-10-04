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
    created_at: Optional[datetime] = None
    created_quantity: Optional[int] = None
    last_in_delta: Optional[int] = None
    last_out_delta: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "quantity": self.quantity,
            "unit": self.unit,
            "last_in": _serialize_timestamp(self.last_in),
            "last_out": _serialize_timestamp(self.last_out),
            "created_at": _serialize_timestamp(self.created_at),
            "created_quantity": self.created_quantity,
            "last_in_delta": self.last_in_delta,
            "last_out_delta": self.last_out_delta,
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
            is_new = name not in data
            record = self._coerce_record(data.get(name))
            previous_quantity = record["quantity"]
            record["quantity"] = quantity
            if unit is not None:
                record["unit"] = str(unit).strip()
            now = _now()
            if is_new:
                record["created_at"] = _serialize_timestamp(now)
                record["created_quantity"] = quantity
            if quantity > previous_quantity:
                record["last_in"] = _serialize_timestamp(now)
                record["last_in_delta"] = quantity - previous_quantity
            elif quantity < previous_quantity:
                record["last_out"] = _serialize_timestamp(now)
                record["last_out_delta"] = previous_quantity - quantity
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
                record["last_in_delta"] = delta
            elif delta < 0:
                record["last_out"] = _serialize_timestamp(now)
                record["last_out_delta"] = abs(delta)
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
            created_at = raw.get("created_at")
            created_quantity = raw.get("created_quantity")
            last_in_delta = raw.get("last_in_delta")
            last_out_delta = raw.get("last_out_delta")
        else:
            quantity = int(raw or 0)
            unit = ""
            last_in = None
            last_out = None
            created_at = None
            created_quantity = quantity
            last_in_delta = None
            last_out_delta = None
        try:
            created_quantity_int: Optional[int]
            if created_quantity is None:
                created_quantity_int = None
            else:
                created_quantity_int = int(created_quantity)
        except (TypeError, ValueError):
            created_quantity_int = None
        try:
            last_in_delta_int: Optional[int]
            if last_in_delta is None:
                last_in_delta_int = None
            else:
                last_in_delta_int = abs(int(last_in_delta))
        except (TypeError, ValueError):
            last_in_delta_int = None
        try:
            last_out_delta_int: Optional[int]
            if last_out_delta is None:
                last_out_delta_int = None
            else:
                last_out_delta_int = abs(int(last_out_delta))
        except (TypeError, ValueError):
            last_out_delta_int = None
        return {
            "quantity": quantity,
            "unit": unit,
            "last_in": last_in,
            "last_out": last_out,
            "created_at": created_at,
            "created_quantity": created_quantity_int,
            "last_in_delta": last_in_delta_int,
            "last_out_delta": last_out_delta_int,
        }

    @staticmethod
    def _record_to_item(name: str, record: Dict[str, Any]) -> InventoryItem:
        return InventoryItem(
            name=name,
            quantity=int(record.get("quantity", 0)),
            unit=str(record.get("unit", "") or "").strip(),
            last_in=_parse_timestamp(record.get("last_in")),
            last_out=_parse_timestamp(record.get("last_out")),
            created_at=_parse_timestamp(record.get("created_at")),
            created_quantity=(
                None
                if record.get("created_quantity") is None
                else int(record.get("created_quantity"))
            ),
            last_in_delta=(
                None if record.get("last_in_delta") is None else int(record.get("last_in_delta"))
            ),
            last_out_delta=(
                None if record.get("last_out_delta") is None else int(record.get("last_out_delta"))
            ),
        )
