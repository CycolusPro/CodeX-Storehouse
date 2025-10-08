"""Inventory management logic for simple Flask app."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Iterable, List, Optional
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


def _normalize_threshold(value: Any) -> Optional[int]:
    """Convert threshold inputs to non-negative integers or ``None``."""

    if value is None:
        return None
    if isinstance(value, str):
        if value.strip() == "":
            return None
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        threshold_int = parsed
    else:
        try:
            threshold_int = int(value)
        except (TypeError, ValueError):
            return None
    if threshold_int < 0:
        return None
    return threshold_int


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
    threshold: Optional[int] = None

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
            "threshold": self.threshold,
        }


@dataclass
class InventoryHistoryEntry:
    """Represents a single inventory mutation event."""

    timestamp: datetime
    action: str
    name: str
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> Dict[str, Any]:
        return {
            "timestamp": _serialize_timestamp(self.timestamp),
            "action": self.action,
            "name": self.name,
            "meta": self.meta,
        }

    def to_dict(self) -> Dict[str, Any]:
        record = self.to_record()
        record["timestamp"] = record["timestamp"]
        return record

    @classmethod
    def from_record(cls, record: Dict[str, Any]) -> "InventoryHistoryEntry":
        timestamp = _parse_timestamp(record.get("timestamp"))
        if timestamp is None:
            raise ValueError("Invalid timestamp in history record")
        action = str(record.get("action") or "").strip()
        name = str(record.get("name") or "").strip()
        meta = record.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        return cls(timestamp=timestamp, action=action, name=name, meta=meta)


@dataclass
class InventoryManager:
    """Manages inventory data persisted to a JSON file."""

    storage_path: Path
    history_path: Optional[Path] = None
    _lock: RLock = field(default_factory=RLock, init=False)

    def __post_init__(self) -> None:
        self.storage_path = Path(self.storage_path)
        if self.history_path is None:
            suffix = ".history.jsonl"
            self.history_path = self.storage_path.with_suffix(suffix)
        else:
            self.history_path = Path(self.history_path)
        if not self.storage_path.exists():
            self._write_data({})
        if self.history_path is not None:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)

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

    def set_quantity(
        self,
        name: str,
        quantity: int,
        unit: Optional[str] = None,
        threshold: Optional[int] = None,
        *,
        keep_threshold: bool = False,
        user: Optional[str] = None,
    ) -> InventoryItem:
        if quantity < 0:
            raise ValueError("Quantity cannot be negative")
        with self._lock:
            data = self._read_data()
            is_new = name not in data
            record = self._coerce_record(data.get(name))
            previous_quantity = record["quantity"]
            previous_unit = record.get("unit", "")
            record["quantity"] = quantity
            if unit is not None:
                record["unit"] = str(unit).strip()
            if not keep_threshold:
                record["threshold"] = _normalize_threshold(threshold)
            new_unit = record.get("unit", "")
            now = _now()
            if is_new:
                record["created_at"] = _serialize_timestamp(now)
                record["created_quantity"] = quantity
                if keep_threshold:
                    record.setdefault("threshold", _normalize_threshold(threshold))
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
            if is_new:
                meta = {
                    "quantity": quantity,
                    "unit": new_unit,
                }
                if user:
                    meta["user"] = user
                self._append_history_entry(
                    InventoryHistoryEntry(
                        timestamp=now,
                        action="create",
                        name=name,
                        meta=meta,
                    )
                )
            else:
                delta = quantity - previous_quantity
                unit_changed = (new_unit or "") != (previous_unit or "")
                if delta != 0 or unit_changed:
                    meta: Dict[str, Any] = {
                        "new_quantity": quantity,
                        "previous_quantity": previous_quantity,
                        "unit": new_unit,
                    }
                    if unit_changed:
                        meta["previous_unit"] = previous_unit
                    if delta != 0:
                        meta["delta"] = delta
                    if user:
                        meta["user"] = user
                    self._append_history_entry(
                        InventoryHistoryEntry(
                            timestamp=now,
                            action="set",
                            name=name,
                            meta=meta,
                        )
                    )
            item = self._record_to_item(name, record)
        return item

    def adjust_quantity(
        self, name: str, delta: int, *, user: Optional[str] = None
    ) -> InventoryItem:
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
                meta = {
                    "delta": delta,
                    "new_quantity": new_quantity,
                    "unit": record.get("unit", ""),
                }
                if user:
                    meta["user"] = user
                self._append_history_entry(
                    InventoryHistoryEntry(
                        timestamp=now,
                        action="in",
                        name=name,
                        meta=meta,
                    )
                )
            elif delta < 0:
                record["last_out"] = _serialize_timestamp(now)
                record["last_out_delta"] = abs(delta)
                meta = {
                    "delta": abs(delta),
                    "new_quantity": new_quantity,
                    "unit": record.get("unit", ""),
                }
                if user:
                    meta["user"] = user
                self._append_history_entry(
                    InventoryHistoryEntry(
                        timestamp=now,
                        action="out",
                        name=name,
                        meta=meta,
                    )
                )
            data[name] = record
            self._write_data(data)
            item = self._record_to_item(name, record)
        return item

    def delete_item(self, name: str, *, user: Optional[str] = None) -> None:
        with self._lock:
            data = self._read_data()
            if name not in data:
                raise KeyError(f"Item '{name}' not found")
            record = self._coerce_record(data.get(name))
            del data[name]
            self._write_data(data)
            now = _now()
            meta = {
                "previous_quantity": record.get("quantity", 0),
                "unit": record.get("unit", ""),
            }
            if user:
                meta["user"] = user
            self._append_history_entry(
                InventoryHistoryEntry(
                    timestamp=now,
                    action="delete",
                    name=name,
                    meta=meta,
                )
            )

    def list_history(self, limit: Optional[int] = None) -> List[InventoryHistoryEntry]:
        if self.history_path is None:
            return []
        with self._lock:
            if not self.history_path.exists():
                return []
            raw_lines = self.history_path.read_text(encoding="utf-8").splitlines()
        entries: List[InventoryHistoryEntry] = []
        for line in raw_lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            try:
                entry = InventoryHistoryEntry.from_record(payload)
            except ValueError:
                continue
            entries.append(entry)
        entries.sort(key=lambda entry: entry.timestamp, reverse=True)
        if limit is not None and limit >= 0:
            return entries[:limit]
        return entries

    def clear_history(self) -> None:
        if self.history_path is None:
            return
        with self._lock:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            self.history_path.write_text("", encoding="utf-8")

    def import_items(
        self, rows: Iterable[Dict[str, Any]], *, user: Optional[str] = None
    ) -> List[InventoryItem]:
        """Bulk import items from iterable rows.

        Each row should provide ``name`` and ``quantity`` fields and may include
        ``unit``. Invalid rows are ignored. The method returns the list of
        imported/updated items in the order they were successfully applied.
        """

        imported: List[InventoryItem] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            quantity_raw = row.get("quantity")
            try:
                quantity = int(quantity_raw)
            except (TypeError, ValueError):
                continue
            if quantity < 0:
                continue
            unit_value = row.get("unit")
            unit = None if unit_value is None else str(unit_value).strip()
            threshold_raw = None
            threshold_field_present = False
            if isinstance(row, dict):
                for key in ("threshold", "阈值提醒", "阈值"):
                    if key in row:
                        threshold_raw = row.get(key)
                        threshold_field_present = True
                        break
            threshold = _normalize_threshold(threshold_raw)
            item = self.set_quantity(
                name,
                quantity,
                unit=unit,
                threshold=threshold,
                keep_threshold=not threshold_field_present,
                user=user,
            )
            imported.append(item)
        return imported

    def export_items(self) -> List[Dict[str, Any]]:
        """Return inventory data formatted for tabular export."""

        records: List[Dict[str, Any]] = []
        items = sorted(self.list_items().values(), key=lambda item: item.name)
        for item in items:
            records.append(
                {
                    "name": item.name,
                    "quantity": item.quantity,
                    "unit": item.unit,
                    "created_at": _serialize_timestamp(item.created_at),
                    "last_in": _serialize_timestamp(item.last_in),
                    "last_in_delta": item.last_in_delta,
                    "last_out": _serialize_timestamp(item.last_out),
                    "last_out_delta": item.last_out_delta,
                    "threshold": item.threshold,
                }
            )
        return records

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

    def _append_history_entry(self, entry: InventoryHistoryEntry) -> None:
        if self.history_path is None:
            return
        record = entry.to_record()
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

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
            threshold_value = _normalize_threshold(raw.get("threshold"))
        else:
            quantity = int(raw or 0)
            unit = ""
            last_in = None
            last_out = None
            created_at = None
            created_quantity = quantity
            last_in_delta = None
            last_out_delta = None
            threshold_value = None
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
            "threshold": threshold_value,
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
            threshold=_normalize_threshold(record.get("threshold")),
        )
