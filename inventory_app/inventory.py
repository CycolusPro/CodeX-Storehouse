"""Inventory management logic for simple Flask app."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Iterable, List, Optional, Tuple, cast
import json
import re


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


_DEFAULT_STORE_ID = "default"
_DEFAULT_STORE_NAME = "默认门店"
_UNCATEGORIZED_ID = "uncategorized"
_UNCATEGORIZED_NAME = "未分类"


def _slugify_identifier(value: str, *, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    normalized = normalized.strip("-")
    if not normalized:
        normalized = fallback
    return normalized


def _next_identifier(base: str, existing: Iterable[str]) -> str:
    if base not in existing:
        return base
    index = 2
    while f"{base}-{index}" in existing:
        index += 1
    return f"{base}-{index}"


@dataclass
class InventoryItem:
    """Represents a single inventory item."""

    name: str
    quantity: int = 0
    unit: str = ""
    category: str = ""
    store_id: str = ""
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
            "category": self.category,
            "store_id": self.store_id,
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
    """Manages inventory data persisted to a JSON file with stores and categories."""

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
        with self._lock:
            state = self._load_state_locked()
            self._write_state_unlocked(state)
        if self.history_path is not None:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Stores and categories
    # ------------------------------------------------------------------
    def list_stores(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            state = self._load_state_locked()
            stores: Dict[str, Dict[str, Any]] = {}
            for store_id, store_data in state["stores"].items():
                stores[store_id] = {
                    "id": store_id,
                    "name": store_data.get("name", store_id),
                    "created_at": store_data.get("created_at"),
                    "items_count": len(store_data.get("items", {})),
                }
            return stores

    def create_store(self, name: str) -> Dict[str, Any]:
        candidate = name.strip()
        if not candidate:
            raise ValueError("门店名称不能为空")
        with self._lock:
            state = self._load_state_locked()
            stores = state["stores"]
            existing_names = {data.get("name", key).strip(): key for key, data in stores.items()}
            if candidate in existing_names:
                raise ValueError("门店名称已存在")
            base = _slugify_identifier(candidate, fallback="store")
            store_id = _next_identifier(base, stores.keys())
            now_serialized = _serialize_timestamp(_now())
            stores[store_id] = {
                "id": store_id,
                "name": candidate,
                "created_at": now_serialized,
                "items": {},
            }
            meta = state.setdefault("meta", {})
            if meta.get("default_store") not in stores:
                meta["default_store"] = store_id
            self._write_state_unlocked(state)
            return {
                "id": store_id,
                "name": candidate,
                "created_at": now_serialized,
                "items_count": 0,
            }

    def delete_store(
        self,
        store_id: str,
        *,
        cascade: bool = False,
        user: Optional[str] = None,
    ) -> None:
        with self._lock:
            state = self._load_state_locked()
            stores = state["stores"]
            if store_id not in stores:
                raise KeyError(f"Store '{store_id}' not found")
            if len(stores) <= 1:
                raise ValueError("至少需要保留一个门店")
            store = stores[store_id]
            items = store.get("items", {})
            if items and not cascade:
                raise ValueError("门店仍有库存，无法删除")
            category_map = state["categories"]
            store_name = store.get("name", store_id)
            if items and cascade:
                for item_name, record in list(items.items()):
                    normalized = self._coerce_record(record, default_category=_UNCATEGORIZED_ID)
                    category_id = normalized.get("category", _UNCATEGORIZED_ID)
                    category_entry = category_map.get(category_id, {})
                    meta = {
                        "previous_quantity": normalized.get("quantity", 0),
                        "unit": normalized.get("unit", ""),
                        "store_id": store_id,
                        "store_name": store_name,
                        "category_id": category_id,
                        "category_name": category_entry.get("name", category_id),
                    }
                    if user:
                        meta["user"] = user
                    self._append_history_entry(
                        InventoryHistoryEntry(
                            timestamp=_now(),
                            action="delete",
                            name=item_name,
                            meta=meta,
                        )
                    )
                items.clear()
            del stores[store_id]
            meta = state.setdefault("meta", {})
            if meta.get("default_store") == store_id:
                meta["default_store"] = next(iter(stores))
            self._write_state_unlocked(state)

    def list_categories(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            state = self._load_state_locked()
            categories: Dict[str, Dict[str, Any]] = {}
            for category_id, category_data in state["categories"].items():
                categories[category_id] = {
                    "id": category_id,
                    "name": category_data.get("name", category_id),
                    "created_at": category_data.get("created_at"),
                }
            return categories

    def create_category(self, name: str) -> Dict[str, Any]:
        candidate = name.strip()
        if not candidate:
            raise ValueError("分类名称不能为空")
        with self._lock:
            state = self._load_state_locked()
            categories = state["categories"]
            for entry in categories.values():
                if entry.get("name") == candidate:
                    raise ValueError("分类名称已存在")
            base = _slugify_identifier(candidate, fallback="category")
            category_id = _next_identifier(base, categories.keys())
            now_serialized = _serialize_timestamp(_now())
            categories[category_id] = {
                "id": category_id,
                "name": candidate,
                "created_at": now_serialized,
            }
            self._write_state_unlocked(state)
            return {
                "id": category_id,
                "name": candidate,
                "created_at": now_serialized,
            }

    def delete_category(
        self,
        category_id: str,
        *,
        cascade: bool = False,
        user: Optional[str] = None,
        store_id: Optional[str] = None,
    ) -> None:
        if category_id == _UNCATEGORIZED_ID:
            raise ValueError("默认分类无法删除")
        with self._lock:
            state = self._load_state_locked()
            categories = state["categories"]
            if category_id not in categories:
                raise KeyError(f"Category '{category_id}' not found")
            resolved_store: Optional[str] = None
            if store_id is not None:
                resolved_store = self._normalize_store_id(state, store_id)
            default_category = _UNCATEGORIZED_ID
            for store_id, store in state["stores"].items():
                items = store.get("items", {})
                to_delete: List[str] = []
                for item_name, record in items.items():
                    normalized = self._coerce_record(record, default_category=default_category)
                    if normalized.get("category", default_category) != category_id:
                        continue
                    should_cascade = cascade and (
                        resolved_store is None or store_id == resolved_store
                    )
                    if should_cascade:
                        meta = {
                            "previous_quantity": normalized.get("quantity", 0),
                            "unit": normalized.get("unit", ""),
                            "store_id": store_id,
                            "store_name": store.get("name", store_id),
                            "category_id": category_id,
                            "category_name": categories[category_id].get("name", category_id),
                        }
                        if user:
                            meta["user"] = user
                        self._append_history_entry(
                            InventoryHistoryEntry(
                                timestamp=_now(),
                                action="delete",
                                name=item_name,
                                meta=meta,
                            )
                        )
                        to_delete.append(item_name)
                    else:
                        record["category"] = default_category
                for item_name in to_delete:
                    items.pop(item_name, None)
            del categories[category_id]
            self._write_state_unlocked(state)

    # ------------------------------------------------------------------
    # Inventory operations
    # ------------------------------------------------------------------
    def list_items(
        self,
        store_id: Optional[str] = None,
        *,
        category_id: Optional[str] = None,
    ) -> Dict[str, InventoryItem]:
        with self._lock:
            state = self._load_state_locked()
            resolved_store = self._normalize_store_id(state, store_id)
            items_map: Dict[str, InventoryItem] = {}
            store = state["stores"][resolved_store]
            for name, record in store.get("items", {}).items():
                normalized = self._coerce_record(record, default_category=_UNCATEGORIZED_ID)
                if category_id and normalized.get("category") != category_id:
                    continue
                items_map[name] = self._record_to_item(
                    name, normalized, store_id=resolved_store
                )
            return items_map

    def get_item(self, name: str, *, store_id: Optional[str] = None) -> InventoryItem:
        items = self.list_items(store_id=store_id)
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
        category: Optional[str] = None,
        store_id: Optional[str] = None,
        user: Optional[str] = None,
    ) -> InventoryItem:
        if quantity < 0:
            raise ValueError("Quantity cannot be negative")
        with self._lock:
            state = self._load_state_locked()
            resolved_store = self._normalize_store_id(state, store_id)
            category_id = self._ensure_category(state, category)
            item = self._set_quantity_locked(
                state,
                resolved_store,
                name,
                quantity,
                unit=unit,
                threshold=threshold,
                keep_threshold=keep_threshold,
                category_id=category_id,
                user=user,
            )
            self._write_state_unlocked(state)
            return item

    def adjust_quantity(
        self,
        name: str,
        delta: int,
        *,
        store_id: Optional[str] = None,
        user: Optional[str] = None,
    ) -> InventoryItem:
        with self._lock:
            state = self._load_state_locked()
            resolved_store = self._normalize_store_id(state, store_id)
            store = state["stores"][resolved_store]
            items = store.get("items", {})
            if name not in items:
                raise KeyError(f"Item '{name}' not found")
            record = self._coerce_record(items[name], default_category=_UNCATEGORIZED_ID)
            current_quantity = record["quantity"]
            new_quantity = current_quantity + delta
            if new_quantity < 0:
                raise ValueError("Insufficient stock for this operation")
            record["quantity"] = new_quantity
            now = _now()
            category_id = record.get("category", _UNCATEGORIZED_ID)
            category_entry = state["categories"].get(category_id, {})
            meta_base = {
                "new_quantity": new_quantity,
                "previous_quantity": current_quantity,
                "unit": record.get("unit", ""),
                "store_id": resolved_store,
                "store_name": store.get("name", resolved_store),
                "category_id": category_id,
                "category_name": category_entry.get("name", category_id),
            }
            if delta > 0:
                record["last_in"] = _serialize_timestamp(now)
                record["last_in_delta"] = delta
                meta = dict(meta_base)
                meta["delta"] = delta
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
                meta = dict(meta_base)
                meta["delta"] = abs(delta)
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
            items[name] = record
            self._write_state_unlocked(state)
            return self._record_to_item(name, record, store_id=resolved_store)

    def transfer_item(
        self,
        name: str,
        quantity: int,
        *,
        source_store_id: Optional[str] = None,
        target_store_id: Optional[str] = None,
        user: Optional[str] = None,
    ) -> Tuple[InventoryItem, InventoryItem]:
        if quantity <= 0:
            raise ValueError("Transfer quantity must be greater than zero")
        with self._lock:
            state = self._load_state_locked()
            source_id = self._normalize_store_id(state, source_store_id)
            target_id = self._normalize_store_id(state, target_store_id)
            if source_id == target_id:
                raise ValueError("Source and target stores must be different")
            stores_state = state["stores"]
            source_store = stores_state[source_id]
            target_store = stores_state[target_id]
            source_items = source_store.setdefault("items", {})
            if name not in source_items:
                raise KeyError(f"Item '{name}' not found in source store")
            source_record = self._coerce_record(
                source_items[name], default_category=_UNCATEGORIZED_ID
            )
            current_source_quantity = source_record["quantity"]
            if quantity > current_source_quantity:
                raise ValueError("Insufficient stock for transfer")

            now = _now()
            source_new_quantity = current_source_quantity - quantity
            source_record["quantity"] = source_new_quantity
            source_record["last_out"] = _serialize_timestamp(now)
            source_record["last_out_delta"] = quantity

            source_unit = source_record.get("unit", "")
            source_category = source_record.get("category", _UNCATEGORIZED_ID)
            source_threshold = source_record.get("threshold")

            target_items = target_store.setdefault("items", {})
            target_exists = name in target_items
            target_record = self._coerce_record(
                target_items.get(name), default_category=_UNCATEGORIZED_ID
            )
            previous_target_quantity = target_record["quantity"]
            target_new_quantity = previous_target_quantity + quantity
            target_record["quantity"] = target_new_quantity
            target_record["last_in"] = _serialize_timestamp(now)
            target_record["last_in_delta"] = quantity
            if not target_exists:
                target_record["created_at"] = _serialize_timestamp(now)
                target_record["created_quantity"] = target_new_quantity
            if not target_record.get("unit") and source_unit:
                target_record["unit"] = source_unit
            target_category = target_record.get("category") or source_category
            target_record["category"] = target_category or _UNCATEGORIZED_ID
            if source_threshold is not None:
                if not target_exists or target_record.get("threshold") is None:
                    target_record["threshold"] = source_threshold

            source_items[name] = source_record
            target_items[name] = target_record

            categories = state["categories"]
            source_category_entry = categories.get(
                source_record.get("category", _UNCATEGORIZED_ID), {}
            )
            target_category_entry = categories.get(
                target_record.get("category", _UNCATEGORIZED_ID), {}
            )

            source_meta: Dict[str, Any] = {
                "previous_quantity": current_source_quantity,
                "new_quantity": source_new_quantity,
                "unit": source_record.get("unit", ""),
                "store_id": source_id,
                "store_name": source_store.get("name", source_id),
                "category_id": source_record.get("category", _UNCATEGORIZED_ID),
                "category_name": source_category_entry.get(
                    "name", source_record.get("category", _UNCATEGORIZED_ID)
                ),
                "delta": quantity,
                "transfer": True,
                "transfer_target_id": target_id,
                "transfer_target_name": target_store.get("name", target_id),
            }
            target_meta: Dict[str, Any] = {
                "previous_quantity": previous_target_quantity,
                "new_quantity": target_new_quantity,
                "unit": target_record.get("unit", ""),
                "store_id": target_id,
                "store_name": target_store.get("name", target_id),
                "category_id": target_record.get("category", _UNCATEGORIZED_ID),
                "category_name": target_category_entry.get(
                    "name", target_record.get("category", _UNCATEGORIZED_ID)
                ),
                "delta": quantity,
                "transfer": True,
                "transfer_source_id": source_id,
                "transfer_source_name": source_store.get("name", source_id),
            }
            if user:
                source_meta["user"] = user
                target_meta["user"] = user

            self._append_history_entry(
                InventoryHistoryEntry(
                    timestamp=now,
                    action="out",
                    name=name,
                    meta=source_meta,
                )
            )
            self._append_history_entry(
                InventoryHistoryEntry(
                    timestamp=now,
                    action="in",
                    name=name,
                    meta=target_meta,
                )
            )

            self._write_state_unlocked(state)
            source_item = self._record_to_item(name, source_record, store_id=source_id)
            target_item = self._record_to_item(name, target_record, store_id=target_id)
            return source_item, target_item

    def delete_item(
        self,
        name: str,
        *,
        store_id: Optional[str] = None,
        user: Optional[str] = None,
    ) -> None:
        with self._lock:
            state = self._load_state_locked()
            resolved_store = self._normalize_store_id(state, store_id)
            store = state["stores"][resolved_store]
            items = store.get("items", {})
            if name not in items:
                raise KeyError(f"Item '{name}' not found")
            record = self._coerce_record(items[name], default_category=_UNCATEGORIZED_ID)
            category_id = record.get("category", _UNCATEGORIZED_ID)
            category_entry = state["categories"].get(category_id, {})
            del items[name]
            self._write_state_unlocked(state)
            meta = {
                "previous_quantity": record.get("quantity", 0),
                "unit": record.get("unit", ""),
                "store_id": resolved_store,
                "store_name": store.get("name", resolved_store),
                "category_id": category_id,
                "category_name": category_entry.get("name", category_id),
            }
            if user:
                meta["user"] = user
            self._append_history_entry(
                InventoryHistoryEntry(
                    timestamp=_now(),
                    action="delete",
                    name=name,
                    meta=meta,
                )
            )

    def list_history(
        self,
        *,
        store_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[InventoryHistoryEntry]:
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
            if store_id and entry.meta.get("store_id") != store_id:
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

    def preview_import_rows(
        self,
        rows: Iterable[Dict[str, Any]],
        *,
        store_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return normalized preview data for a batch import without mutating state."""

        preview: List[Dict[str, Any]] = []
        with self._lock:
            state = self._load_state_locked()
            state_copy = deepcopy(state)
            resolved_store = self._normalize_store_id(state_copy, store_id)
            categories = state_copy["categories"]
            store_entry = state_copy["stores"].get(resolved_store, {})
            items = store_entry.get("items", {}) if isinstance(store_entry, dict) else {}
            for index, row in enumerate(rows, start=1):
                entry: Dict[str, Any] = {
                    "index": index,
                    "valid": False,
                    "name": "",
                    "quantity": None,
                    "unit": "",
                    "threshold": None,
                    "threshold_field_present": False,
                    "category_id": _UNCATEGORIZED_ID,
                    "category_name": categories.get(_UNCATEGORIZED_ID, {}).get(
                        "name", _UNCATEGORIZED_NAME
                    ),
                    "category_input": "",
                    "messages": [],
                    "existing": False,
                    "existing_quantity": None,
                    "existing_unit": "",
                    "existing_category_id": None,
                    "existing_category_name": "",
                    "quantity_delta": None,
                    "quantity_changed": False,
                    "unit_changed": False,
                    "category_changed": False,
                }
                if not isinstance(row, dict):
                    entry["messages"].append("无法识别的行格式")
                    preview.append(entry)
                    continue
                name = str(row.get("name") or "").strip()
                entry["name"] = name
                if not name:
                    entry["messages"].append("缺少 SKU 名称")
                quantity_raw = row.get("quantity")
                quantity: Optional[int]
                try:
                    quantity = int(quantity_raw)
                    if quantity < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    entry["messages"].append("数量必须为非负整数")
                    quantity = None
                entry["quantity"] = quantity
                unit_value = row.get("unit")
                entry["unit"] = "" if unit_value is None else str(unit_value).strip()
                threshold_raw = None
                threshold_field_present = False
                for key in ("threshold", "阈值提醒", "阈值"):
                    if key in row:
                        threshold_raw = row.get(key)
                        threshold_field_present = True
                        break
                entry["threshold_field_present"] = threshold_field_present
                threshold = _normalize_threshold(threshold_raw)
                entry["threshold"] = threshold
                if threshold_field_present:
                    raw_text = "" if threshold_raw is None else str(threshold_raw).strip()
                    if raw_text and threshold is None:
                        entry["messages"].append("阈值格式无效")
                category_raw = None
                for key in ("category", "分类", "库存分类"):
                    if key in row:
                        category_raw = row.get(key)
                        break
                entry["category_input"] = (
                    "" if category_raw is None else str(category_raw).strip()
                )
                category_id = self._ensure_category(state_copy, category_raw)
                entry["category_id"] = category_id
                category_record = state_copy["categories"].get(category_id, {})
                entry["category_name"] = category_record.get(
                    "name", category_id or _UNCATEGORIZED_NAME
                )
                if name and name in items:
                    existing_raw = items.get(name)
                    normalized_existing = self._coerce_record(
                        existing_raw, default_category=_UNCATEGORIZED_ID
                    )
                    entry["existing"] = True
                    entry["existing_quantity"] = normalized_existing.get("quantity")
                    entry["existing_unit"] = normalized_existing.get("unit", "")
                    existing_category_id = normalized_existing.get(
                        "category", _UNCATEGORIZED_ID
                    )
                    entry["existing_category_id"] = existing_category_id
                    entry["existing_category_name"] = categories.get(
                        existing_category_id, {}
                    ).get("name", existing_category_id or _UNCATEGORIZED_NAME)
                entry["store_id"] = resolved_store
                if name and quantity is not None and not entry["messages"]:
                    entry["valid"] = True
                if entry["valid"]:
                    if entry["existing"]:
                        existing_quantity = cast(
                            Optional[int], entry["existing_quantity"]
                        )
                        previous_quantity = (
                            0 if existing_quantity is None else int(existing_quantity)
                        )
                        entry["quantity_delta"] = entry["quantity"] - previous_quantity
                        entry["quantity_changed"] = entry["quantity_delta"] != 0
                        entry["unit_changed"] = (
                            (entry["unit"] or "")
                            != (entry["existing_unit"] or "")
                        )
                        entry["category_changed"] = (
                            (entry["existing_category_id"] or _UNCATEGORIZED_ID)
                            != (entry["category_id"] or _UNCATEGORIZED_ID)
                        )
                    else:
                        entry["quantity_delta"] = entry["quantity"]
                preview.append(entry)
        return preview

    def import_items(
        self,
        rows: Iterable[Dict[str, Any]],
        *,
        store_id: Optional[str] = None,
        user: Optional[str] = None,
    ) -> List[InventoryItem]:
        imported: List[InventoryItem] = []
        with self._lock:
            state = self._load_state_locked()
            resolved_store = self._normalize_store_id(state, store_id)
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
                for key in ("threshold", "阈值提醒", "阈值"):
                    if key in row:
                        threshold_raw = row.get(key)
                        threshold_field_present = True
                        break
                category_raw = None
                for key in ("category", "分类", "库存分类"):
                    if key in row and str(row.get(key) or "").strip():
                        category_raw = row.get(key)
                        break
                category_id = self._ensure_category(state, category_raw)
                threshold = _normalize_threshold(threshold_raw)
                item = self._set_quantity_locked(
                    state,
                    resolved_store,
                    name,
                    quantity,
                    unit=unit,
                    threshold=threshold,
                    keep_threshold=not threshold_field_present,
                    category_id=category_id,
                    user=user,
                )
                imported.append(item)
            self._write_state_unlocked(state)
        return imported

    def export_items(
        self,
        *,
        store_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        with self._lock:
            state = self._load_state_locked()
            if store_id:
                store_ids = [self._normalize_store_id(state, store_id)]
            else:
                store_ids = list(state["stores"].keys())
            for sid in store_ids:
                store = state["stores"][sid]
                for name, record in store.get("items", {}).items():
                    normalized = self._coerce_record(record, default_category=_UNCATEGORIZED_ID)
                    item = self._record_to_item(name, normalized, store_id=sid)
                    payload = item.to_dict()
                    category_entry = state["categories"].get(item.category, {})
                    payload["store_id"] = sid
                    payload["store_name"] = store.get("name", sid)
                    payload["category_name"] = category_entry.get("name", item.category)
                    records.append(payload)
        def _sort_key(row: Dict[str, Any]) -> tuple[Any, ...]:
            quantity_value = row.get("quantity")
            try:
                quantity = int(quantity_value)
            except (TypeError, ValueError):
                quantity = 0
            store_name = str(row.get("store_name") or "")
            category_name = str(row.get("category_name") or "")
            name = str(row.get("name") or "")
            return (store_name, category_name, -quantity, name)

        records.sort(key=_sort_key)
        return records

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _initial_state(self) -> Dict[str, Any]:
        now_serialized = _serialize_timestamp(_now())
        return {
            "stores": {
                _DEFAULT_STORE_ID: {
                    "id": _DEFAULT_STORE_ID,
                    "name": _DEFAULT_STORE_NAME,
                    "created_at": now_serialized,
                    "items": {},
                }
            },
            "categories": {
                _UNCATEGORIZED_ID: {
                    "id": _UNCATEGORIZED_ID,
                    "name": _UNCATEGORIZED_NAME,
                    "created_at": now_serialized,
                }
            },
            "meta": {"default_store": _DEFAULT_STORE_ID},
        }

    def _load_state_locked(self) -> Dict[str, Any]:
        if not self.storage_path.exists():
            state = self._initial_state()
            self._write_state_unlocked(state)
            return state
        raw = self.storage_path.read_text(encoding="utf-8") or "{}"
        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            state = {}
        changed, upgraded = self._upgrade_state(state)
        if changed:
            self._write_state_unlocked(upgraded)
        return upgraded

    def _write_state_unlocked(self, state: Dict[str, Any]) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.storage_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        temp_path.replace(self.storage_path)

    def _append_history_entry(self, entry: InventoryHistoryEntry) -> None:
        if self.history_path is None:
            return
        record = entry.to_record()
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _upgrade_state(self, state: Any) -> Tuple[bool, Dict[str, Any]]:
        changed = False
        if not isinstance(state, dict):
            state = {}
            changed = True
        now_serialized = _serialize_timestamp(_now())
        stores_raw = state.get("stores")
        categories_raw = state.get("categories")
        meta = state.get("meta")
        legacy_items: Optional[Dict[str, Any]] = None
        if not isinstance(stores_raw, dict):
            legacy_items = {}
            for key, value in state.items():
                if key in {"categories", "meta", "stores"}:
                    continue
                if isinstance(value, dict):
                    legacy_items[key] = value
            stores: Dict[str, Any] = {}
            changed = True
        else:
            stores = dict(stores_raw)
        if legacy_items is not None:
            stores[_DEFAULT_STORE_ID] = {
                "id": _DEFAULT_STORE_ID,
                "name": _DEFAULT_STORE_NAME,
                "created_at": now_serialized,
                "items": legacy_items,
            }
        if not stores:
            stores[_DEFAULT_STORE_ID] = {
                "id": _DEFAULT_STORE_ID,
                "name": _DEFAULT_STORE_NAME,
                "created_at": now_serialized,
                "items": {},
            }
            changed = True
        for store_id, store_data in list(stores.items()):
            if not isinstance(store_data, dict):
                stores[store_id] = {
                    "id": store_id,
                    "name": store_id,
                    "created_at": now_serialized,
                    "items": {},
                }
                changed = True
                continue
            if "items" not in store_data or not isinstance(store_data["items"], dict):
                store_data["items"] = {}
                changed = True
            if store_data.get("id") != store_id:
                store_data["id"] = store_id
                changed = True
            if "name" not in store_data or not isinstance(store_data["name"], str):
                store_data["name"] = store_id
                changed = True
            if "created_at" not in store_data:
                store_data["created_at"] = now_serialized
                changed = True
        if not isinstance(categories_raw, dict):
            categories: Dict[str, Any] = {}
            changed = True
        else:
            categories = dict(categories_raw)
        if _UNCATEGORIZED_ID not in categories:
            categories[_UNCATEGORIZED_ID] = {
                "id": _UNCATEGORIZED_ID,
                "name": _UNCATEGORIZED_NAME,
                "created_at": now_serialized,
            }
            changed = True
        for category_id, category_data in list(categories.items()):
            if not isinstance(category_data, dict):
                categories[category_id] = {
                    "id": category_id,
                    "name": str(category_data),
                    "created_at": now_serialized,
                }
                changed = True
                continue
            if category_data.get("id") != category_id:
                category_data["id"] = category_id
                changed = True
            if "name" not in category_data or not isinstance(category_data["name"], str):
                category_data["name"] = category_id
                changed = True
            if "created_at" not in category_data:
                category_data["created_at"] = now_serialized
                changed = True
        for store_data in stores.values():
            items = store_data.get("items", {})
            for item_name, record in list(items.items()):
                if not isinstance(record, dict):
                    items[item_name] = {"quantity": int(record or 0)}
                    record = items[item_name]
                    changed = True
                category_id = record.get("category")
                if not isinstance(category_id, str) or not category_id.strip():
                    record["category"] = _UNCATEGORIZED_ID
                    changed = True
                elif category_id not in categories:
                    categories[category_id] = {
                        "id": category_id,
                        "name": category_id,
                        "created_at": now_serialized,
                    }
                    changed = True
        if not isinstance(meta, dict):
            meta = {}
            changed = True
        if meta.get("default_store") not in stores:
            meta["default_store"] = next(iter(stores))
            changed = True
        upgraded = {
            "stores": stores,
            "categories": categories,
            "meta": meta,
        }
        return changed, upgraded

    def _normalize_store_id(self, state: Dict[str, Any], store_id: Optional[str]) -> str:
        stores = state["stores"]
        if store_id and store_id in stores:
            return store_id
        default_store = state.get("meta", {}).get("default_store")
        if default_store in stores:
            return default_store
        return next(iter(stores))

    def _ensure_category(self, state: Dict[str, Any], category: Optional[str]) -> str:
        categories = state["categories"]
        if category is None:
            return _UNCATEGORIZED_ID
        candidate = str(category).strip()
        if candidate == "":
            return _UNCATEGORIZED_ID
        if candidate in categories:
            return candidate
        for category_id, data in categories.items():
            if data.get("name") == candidate:
                return category_id
        base = _slugify_identifier(candidate, fallback="category")
        new_id = _next_identifier(base, categories.keys())
        categories[new_id] = {
            "id": new_id,
            "name": candidate,
            "created_at": _serialize_timestamp(_now()),
        }
        return new_id

    def _set_quantity_locked(
        self,
        state: Dict[str, Any],
        store_id: str,
        name: str,
        quantity: int,
        *,
        unit: Optional[str],
        threshold: Optional[int],
        keep_threshold: bool,
        category_id: str,
        user: Optional[str],
    ) -> InventoryItem:
        store = state["stores"][store_id]
        items = store.setdefault("items", {})
        is_new = name not in items
        record = self._coerce_record(items.get(name), default_category=_UNCATEGORIZED_ID)
        previous_quantity = record["quantity"]
        previous_unit = record.get("unit", "")
        record["quantity"] = quantity
        if unit is not None:
            record["unit"] = str(unit).strip()
        record["category"] = category_id or _UNCATEGORIZED_ID
        if not keep_threshold:
            record["threshold"] = _normalize_threshold(threshold)
        else:
            if is_new and record.get("threshold") is None:
                record["threshold"] = _normalize_threshold(threshold)
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
        elif quantity > 0 and record.get("last_in") is None and record.get("last_out") is None:
            record["last_in"] = _serialize_timestamp(now)
        items[name] = record
        category_entry = state["categories"].get(category_id, {})
        store_name = store.get("name", store_id)
        new_unit = record.get("unit", "")
        if is_new:
            meta = {
                "quantity": quantity,
                "previous_quantity": previous_quantity,
                "unit": new_unit,
                "store_id": store_id,
                "store_name": store_name,
                "category_id": category_id,
                "category_name": category_entry.get("name", category_id),
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
                    "store_id": store_id,
                    "store_name": store_name,
                    "category_id": category_id,
                    "category_name": category_entry.get("name", category_id),
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
        return self._record_to_item(name, record, store_id=store_id)

    @staticmethod
    def _coerce_record(raw: Any, *, default_category: str) -> Dict[str, Any]:
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
            category_value = raw.get("category")
            category = default_category if category_value is None else str(category_value).strip() or default_category
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
            category = default_category
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
            "category": category,
        }

    @staticmethod
    def _record_to_item(
        name: str,
        record: Dict[str, Any],
        *,
        store_id: str,
    ) -> InventoryItem:
        return InventoryItem(
            name=name,
            quantity=int(record.get("quantity", 0)),
            unit=str(record.get("unit", "") or "").strip(),
            category=str(record.get("category", _UNCATEGORIZED_ID)),
            store_id=store_id,
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
