from io import BytesIO
import csv
from io import StringIO
from pathlib import Path

import pytest

from inventory_app.inventory import InventoryHistoryEntry, InventoryManager


def _login(client) -> None:
    response = client.post(
        "/login",
        data={"username": "admin", "password": "admin"},
        follow_redirects=True,
    )
    assert response.status_code == 200


def test_set_and_get(tmp_path: Path) -> None:
    storage = tmp_path / "data.json"
    manager = InventoryManager(storage)

    item = manager.set_quantity("螺丝", 10, unit="盒", threshold=3)
    assert item.quantity == 10
    assert item.unit == "盒"
    assert item.last_in is not None
    assert item.last_out is None
    assert item.created_at is not None
    assert item.created_quantity == 10
    assert item.last_in_delta == 10
    assert item.threshold == 3
    assert item.category == "uncategorized"
    assert item.store_id == "default"

    fetched = manager.get_item("螺丝")
    assert fetched.quantity == 10
    assert fetched.unit == "盒"
    assert fetched.last_in is not None
    assert fetched.last_out is None
    assert fetched.created_at is not None
    assert fetched.created_quantity == 10
    assert fetched.last_in_delta == 10
    assert fetched.threshold == 3
    assert fetched.category == "uncategorized"
    assert fetched.store_id == "default"


def test_adjust_quantity(tmp_path: Path) -> None:
    storage = tmp_path / "data.json"
    manager = InventoryManager(storage)

    manager.set_quantity("螺丝", 5, unit="件", threshold=2)
    manager.adjust_quantity("螺丝", 5)
    after_in = manager.get_item("螺丝")
    assert after_in.quantity == 10
    assert after_in.unit == "件"
    assert after_in.last_in is not None
    assert after_in.last_in_delta == 5
    assert after_in.threshold == 2

    manager.adjust_quantity("螺丝", -3)
    after_out = manager.get_item("螺丝")
    assert after_out.quantity == 7
    assert after_out.unit == "件"
    assert after_out.last_out is not None
    assert after_out.last_out_delta == 3
    assert after_out.threshold == 2


def test_adjust_quantity_rejects_negative(tmp_path: Path) -> None:
    storage = tmp_path / "data.json"
    manager = InventoryManager(storage)

    manager.set_quantity("螺丝", 2)
    with pytest.raises(ValueError):
        manager.adjust_quantity("螺丝", -3)


def test_set_quantity_threshold_preservation(tmp_path: Path) -> None:
    storage = tmp_path / "data.json"
    manager = InventoryManager(storage)

    manager.set_quantity("面粉", 8, threshold=3)
    manager.set_quantity("面粉", 10, keep_threshold=True)
    item = manager.get_item("面粉")
    assert item.threshold == 3

    manager.set_quantity("面粉", 6, threshold=None)
    updated = manager.get_item("面粉")
    assert updated.threshold is None


def test_serialization_contains_timestamps(tmp_path: Path) -> None:
    storage = tmp_path / "data.json"
    manager = InventoryManager(storage)

    item = manager.set_quantity("垫片", 4, unit="包", threshold=1)
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
    assert payload["threshold"] == 1
    assert payload["category"] == "uncategorized"
    assert payload["store_id"] == "default"


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


def test_clear_history(tmp_path: Path) -> None:
    storage = tmp_path / "data.json"
    manager = InventoryManager(storage)

    manager.set_quantity("咖啡豆", 5)
    manager.adjust_quantity("咖啡豆", 2)

    assert manager.list_history()

    manager.clear_history()

    assert manager.list_history() == []


def test_store_and_category_management(tmp_path: Path) -> None:
    storage = tmp_path / "data.json"
    manager = InventoryManager(storage)

    stores = manager.list_stores()
    assert "default" in stores

    created_store = manager.create_store("北区仓库")
    assert created_store["name"] == "北区仓库"
    assert created_store["id"] in manager.list_stores()

    item = manager.set_quantity(
        "物料A",
        5,
        store_id=created_store["id"],
        category="饮料",
    )
    assert item.store_id == created_store["id"]
    category_id = item.category
    categories = manager.list_categories()
    assert category_id in categories

    manager.delete_category(category_id, cascade=False)
    reassigned = manager.get_item("物料A", store_id=created_store["id"])
    assert reassigned.category == "uncategorized"

    manager.delete_store(created_store["id"], cascade=True)
    assert created_store["id"] not in manager.list_stores()


def test_import_creates_category(tmp_path: Path) -> None:
    storage = tmp_path / "data.json"
    manager = InventoryManager(storage)

    rows = [
        {"name": "水杯", "quantity": 3, "category": "日用品"},
        {"name": "纸巾", "quantity": 6, "category": "日用品"},
    ]
    imported = manager.import_items(rows, user="tester")
    assert len(imported) == 2
    category_id = imported[0].category
    categories = manager.list_categories()
    assert category_id in categories
    assert categories[category_id]["name"] == "日用品"


def test_history_api_endpoint(tmp_path: Path) -> None:
    pytest.importorskip("flask")
    from inventory_app.app import create_app

    storage = tmp_path / "data.json"
    app = create_app(storage)
    app.config.update(TESTING=True)
    client = app.test_client()

    _login(client)

    response = client.post(
        "/api/items",
        json={"name": "咖啡豆", "quantity": 8, "unit": "袋", "threshold": 3},
    )
    assert response.status_code == 201
    created_payload = response.get_json()
    assert created_payload["threshold"] == 3
    assert created_payload["store_id"] == "default"
    assert created_payload["category"] == "uncategorized"

    client.post("/api/items/咖啡豆/in", json={"quantity": 2})

    history_response = client.get("/api/history?limit=5")
    assert history_response.status_code == 200
    payload = history_response.get_json()
    assert isinstance(payload, list)
    assert payload
    assert payload[0]["name"] == "咖啡豆"
    assert "action" in payload[0]


def test_history_export_csv_format(tmp_path: Path) -> None:
    pytest.importorskip("flask")
    from inventory_app.app import create_app

    storage = tmp_path / "data.json"
    app = create_app(storage)
    app.config.update(TESTING=True)
    client = app.test_client()

    _login(client)

    client.post(
        "/api/items",
        json={"name": "咖啡豆", "quantity": 5, "unit": "袋"},
    )
    client.post("/api/items/咖啡豆/in", json={"quantity": 3})
    client.post("/api/items/咖啡豆/out", json={"quantity": 2})

    response = client.get("/api/history/export")
    assert response.status_code == 200

    text = response.data.decode("utf-8-sig")
    reader = csv.DictReader(StringIO(text))
    assert reader.fieldnames == ["时间", "操作类型", "SKU 名称", "操作用户", "门店", "分类"]
    rows = list(reader)
    assert rows
    latest = rows[0]
    assert latest["SKU 名称"] == "咖啡豆"
    assert latest["操作类型"] in {"入库", "出库", "新增", "盘点", "删除"}
    assert latest["操作用户"] == "admin"


def test_history_stats_export_and_dashboard(tmp_path: Path) -> None:
    pytest.importorskip("flask")
    from inventory_app.app import create_app

    storage = tmp_path / "data.json"
    app = create_app(storage)
    app.config.update(TESTING=True)
    client = app.test_client()

    _login(client)

    client.post(
        "/api/items",
        json={"name": "纸箱", "quantity": 20, "unit": "箱"},
    )
    client.post("/api/items/纸箱/in", json={"quantity": 5})
    client.post("/api/items/纸箱/out", json={"quantity": 4})

    dashboard = client.get("/analytics?mode=day")
    assert dashboard.status_code == 200
    html = dashboard.data.decode("utf-8")
    assert "出入库统计" in html
    assert "数据明细" in html

    export_response = client.get("/api/history/stats/export?mode=day")
    assert export_response.status_code == 200
    export_text = export_response.data.decode("utf-8-sig")
    export_reader = csv.DictReader(StringIO(export_text))
    assert export_reader.fieldnames == ["时间", "入库数量", "出库数量", "净变动"]
    export_rows = list(export_reader)
    assert export_rows
    totals_row = export_rows[-1]
    assert totals_row["时间"] == "合计"
    inbound_total = int(totals_row["入库数量"])
    outbound_total = int(totals_row["出库数量"])
    assert inbound_total >= 0
    assert outbound_total >= 0


def test_manager_import_and_export(tmp_path: Path) -> None:
    storage = tmp_path / "data.json"
    manager = InventoryManager(storage)

    rows = [
        {"name": "螺丝", "quantity": "5", "unit": "盒", "threshold": "2"},
        {"name": "", "quantity": 10},
        {"name": "垫片", "quantity": "-1"},
        {"name": "扳手", "quantity": "abc"},
    ]
    imported = manager.import_items(rows)
    assert [item.name for item in imported] == ["螺丝"]
    assert imported[0].threshold == 2

    exported = manager.export_items()
    assert len(exported) == 1
    record = exported[0]
    assert record["name"] == "螺丝"
    assert record["quantity"] == 5
    assert record["unit"] == "盒"
    assert "created_at" in record
    assert "last_in" in record
    assert record["threshold"] == 2


def test_import_export_endpoints(tmp_path: Path) -> None:
    pytest.importorskip("flask")
    from inventory_app.app import create_app

    storage = tmp_path / "data.json"
    app = create_app(storage)
    app.config.update(TESTING=True)
    client = app.test_client()

    _login(client)

    response = client.post(
        "/api/items/import",
        json={"items": [{"name": "咖啡豆", "quantity": 5, "unit": "袋", "threshold": 2}]},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["count"] == 1

    export_resp = client.get("/api/items/export")
    assert export_resp.status_code == 200
    assert "inventory_export" in export_resp.headers["Content-Disposition"]
    export_text = export_resp.data.decode("utf-8-sig")
    header = export_text.splitlines()[0]
    assert "name" in header and "threshold" in header
    assert "咖啡豆" in export_text

    template_resp = client.get("/api/items/template")
    assert template_resp.status_code == 200
    template_text = template_resp.data.decode("utf-8-sig")
    assert "名称,数量,单位,阈值提醒,库存分类" == template_text.splitlines()[0]


def test_history_export_endpoint(tmp_path: Path) -> None:
    pytest.importorskip("flask")
    from inventory_app.app import create_app

    storage = tmp_path / "data.json"
    app = create_app(storage)
    app.config.update(TESTING=True)
    client = app.test_client()

    _login(client)

    client.post("/api/items", json={"name": "咖啡豆", "quantity": 8, "unit": "袋"})
    client.post("/api/items/咖啡豆/in", json={"quantity": 2})

    export_resp = client.get("/api/history/export")
    assert export_resp.status_code == 200
    text = export_resp.data.decode("utf-8-sig")
    lines = [line for line in text.splitlines() if line]
    assert lines
    assert lines[0] == "时间,操作类型,SKU 名称,操作用户,门店,分类"
    assert any("咖啡豆" in line for line in lines[1:])
    assert any("入库" in line or "出库" in line for line in lines[1:])


def test_import_form_endpoint(tmp_path: Path) -> None:
    pytest.importorskip("flask")
    from inventory_app.app import create_app

    storage = tmp_path / "data.json"
    app = create_app(storage)
    app.config.update(TESTING=True)
    client = app.test_client()

    _login(client)

    csv_payload = "名称,数量,单位,阈值提醒\n茶叶,8,罐,3\n"
    response = client.post(
        "/import",
        data={"file": (BytesIO(csv_payload.encode("utf-8")), "bulk.csv")},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "imported=1" in response.headers["Location"]

    items_response = client.get("/api/items")
    items = items_response.get_json()
    assert any(item["name"] == "茶叶" and item.get("threshold") == 3 for item in items)


def test_template_roundtrip_import(tmp_path: Path) -> None:
    pytest.importorskip("flask")
    from inventory_app.app import create_app, _parse_csv_rows

    storage = tmp_path / "data.json"
    app = create_app(storage)
    app.config.update(TESTING=True)
    client = app.test_client()

    _login(client)

    template_resp = client.get("/api/items/template")
    assert template_resp.status_code == 200

    template_text = template_resp.data.decode("utf-8")
    lines = template_text.splitlines()
    assert len(lines) >= 2
    lines[1] = "新品饮料,12,箱,3,饮料"
    edited = "\n".join(lines)

    parsed_rows = _parse_csv_rows(edited)
    assert parsed_rows and parsed_rows[0]["name"] == "新品饮料"

    response = client.post(
        "/import",
        data={"file": (BytesIO(edited.encode("utf-8")), "bulk.csv")},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert response.status_code == 302

    items_response = client.get("/api/items")
    items = items_response.get_json()
    assert any(item["name"] == "新品饮料" for item in items)
