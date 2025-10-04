from pathlib import Path

import pytest

from inventory_app.inventory import InventoryHistoryEntry, InventoryManager


def test_set_and_get(tmp_path: Path) -> None:
    storage = tmp_path / "data.json"
    manager = InventoryManager(storage)

    item = manager.set_quantity("螺丝", 10, unit="盒")
    assert item.quantity == 10
    assert item.unit == "盒"
    assert item.last_in is not None
    assert item.last_out is None
    assert item.created_at is not None
    assert item.created_quantity == 10
    assert item.last_in_delta == 10

    fetched = manager.get_item("螺丝")
    assert fetched.quantity == 10
    assert fetched.unit == "盒"
    assert fetched.last_in is not None
    assert fetched.last_out is None
    assert fetched.created_at is not None
    assert fetched.created_quantity == 10
    assert fetched.last_in_delta == 10


def test_adjust_quantity(tmp_path: Path) -> None:
    storage = tmp_path / "data.json"
    manager = InventoryManager(storage)

    manager.set_quantity("螺丝", 5, unit="件")
    manager.adjust_quantity("螺丝", 5)
    after_in = manager.get_item("螺丝")
    assert after_in.quantity == 10
    assert after_in.unit == "件"
    assert after_in.last_in is not None
    assert after_in.last_in_delta == 5

    manager.adjust_quantity("螺丝", -3)
    after_out = manager.get_item("螺丝")
    assert after_out.quantity == 7
    assert after_out.unit == "件"
    assert after_out.last_out is not None
    assert after_out.last_out_delta == 3


def test_adjust_quantity_rejects_negative(tmp_path: Path) -> None:
    storage = tmp_path / "data.json"
    manager = InventoryManager(storage)

    manager.set_quantity("螺丝", 2)
    with pytest.raises(ValueError):
        manager.adjust_quantity("螺丝", -3)


def test_serialization_contains_timestamps(tmp_path: Path) -> None:
    storage = tmp_path / "data.json"
    manager = InventoryManager(storage)

    item = manager.set_quantity("垫片", 4, unit="包")
    payload = item.to_dict()

    assert payload["quantity"] == 4
    assert payload["unit"] == "包"
    assert payload["last_in"] is not None
    assert isinstance(payload["last_in"], str)
    assert payload["last_out"] is None
    assert payload["created_at"] is not None
    assert payload["created_quantity"] == 4
    assert payload["last_in_delta"] == 4
    assert payload["last_out_delta"] is None


def test_delete_item(tmp_path: Path) -> None:
    storage = tmp_path / "data.json"
    manager = InventoryManager(storage)

    manager.set_quantity("咖啡豆", 12, unit="袋")
    assert "咖啡豆" in manager.list_items()

    manager.delete_item("咖啡豆")
    assert "咖啡豆" not in manager.list_items()
    with pytest.raises(KeyError):
        manager.get_item("咖啡豆")


def test_history_logging(tmp_path: Path) -> None:
    storage = tmp_path / "data.json"
    history_path = tmp_path / "history.jsonl"
    manager = InventoryManager(storage, history_path=history_path)

    manager.set_quantity("咖啡豆", 10, unit="袋")
    manager.adjust_quantity("咖啡豆", 5)
    manager.adjust_quantity("咖啡豆", -3)
    manager.set_quantity("咖啡豆", 12, unit="箱")
    manager.delete_item("咖啡豆")

    entries = manager.list_history()
    actions = [entry.action for entry in entries]
    assert actions.count("create") == 1
    assert "in" in actions
    assert "out" in actions
    assert "set" in actions
    assert "delete" in actions

    latest_entry = entries[0]
    assert isinstance(latest_entry, InventoryHistoryEntry)
    assert latest_entry.action == "delete"
    assert latest_entry.meta["previous_quantity"] == 12


def test_history_limit(tmp_path: Path) -> None:
    storage = tmp_path / "data.json"
    manager = InventoryManager(storage)

    for idx in range(6):
        manager.set_quantity(f"SKU-{idx}", idx + 1)

    entries = manager.list_history(limit=3)
    assert len(entries) == 3
    assert entries[0].timestamp >= entries[1].timestamp >= entries[2].timestamp


def test_history_api_endpoint(tmp_path: Path) -> None:
    pytest.importorskip("flask")
    from inventory_app.app import create_app

    storage = tmp_path / "data.json"
    app = create_app(storage)
    client = app.test_client()

    response = client.post(
        "/api/items",
        json={"name": "咖啡豆", "quantity": 8, "unit": "袋"},
    )
    assert response.status_code == 201

    client.post("/api/items/咖啡豆/in", json={"quantity": 2})

    history_response = client.get("/api/history?limit=5")
    assert history_response.status_code == 200
    payload = history_response.get_json()
    assert isinstance(payload, list)
    assert payload
    assert payload[0]["name"] == "咖啡豆"
    assert "action" in payload[0]
