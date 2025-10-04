"""Inventory app package."""
from __future__ import annotations

from .inventory import InventoryHistoryEntry, InventoryItem, InventoryManager

__all__ = ["create_app", "InventoryHistoryEntry", "InventoryItem", "InventoryManager"]


def create_app(*args, **kwargs):
    from .app import create_app as _create_app

    return _create_app(*args, **kwargs)
