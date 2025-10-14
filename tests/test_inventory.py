from io import BytesIO
import csv
from io import StringIO
from pathlib import Path

import pytest

from inventory_app.app import _history_statistics
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


def test_transfer_between_stores(tmp_path: Path) -> None:
    storage = tmp_path / "data.json"
    history_path = tmp_path / "history.jsonl"
    manager = InventoryManager(storage, history_path=history_path)

    target_store = manager.create_store("分店A")
    manager.set_quantity("咖啡豆", 10, unit="袋", threshold=2, store_id="default")

    source_item, target_item = manager.transfer_item(
        "咖啡豆",
        4,
        source_store_id="default",
        target_store_id=target_store["id"],
        user="tester",
    )

    assert source_item.quantity == 6
    assert source_item.store_id == "default"
    assert target_item.quantity == 4
    assert target_item.store_id == target_store["id"]
    assert target_item.unit == "袋"
    assert target_item.threshold == 2

    source_history = manager.list_history(store_id="default")
    target_history = manager.list_history(store_id=target_store["id"])
    assert any(entry.action == "out" and entry.meta.get("transfer") for entry in source_history)
    assert any(entry.action == "in" and entry.meta.get("transfer") for entry in target_history)

    with pytest.raises(ValueError):
        manager.transfer_item(
            "咖啡豆",
            20,
            source_store_id="default",
            target_store_id=target_store["id"],
        )

    with pytest.raises(ValueError):
        manager.transfer_item(
            "咖啡豆",
            1,
            source_store_id="default",
            target_store_id="default",
        )


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


def test_history_statistics_counts_create_and_set(tmp_path: Path) -> None:
    storage = tmp_path / "data.json"
    history_path = tmp_path / "history.jsonl"
    manager = InventoryManager(storage, history_path=history_path)

    manager.set_quantity("样品", 10)
    manager.set_quantity("样品", 18)
    manager.set_quantity("样品", 12)

    entries = manager.list_history()
    rows = _history_statistics(entries)

    assert rows
    stats = rows[0]
    assert stats["inbound"] == 18
    assert stats["outbound"] == 6
    assert stats["net"] == 12


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
    assert reader.fieldnames == [
        "时间",
        "操作类型",
        "SKU 名称",
        "操作用户",
        "门店",
        "分类",
        "初始量",
        "增减量",
        "当前量",
    ]
    rows = list(reader)
    assert rows
    latest = rows[0]
    assert latest["SKU 名称"] == "咖啡豆"
    assert latest["操作类型"] in {"入库", "出库", "新增", "盘点", "删除"}
    assert latest["操作用户"] == "admin"
    assert latest["当前量"]
    assert latest["增减量"]


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

    dashboard = client.get("/analytics?mode=sku")
    assert dashboard.status_code == 200
    html = dashboard.data.decode("utf-8")
    assert "出入库统计" in html
    assert "数据明细" in html

    export_response = client.get("/api/history/stats/export?mode=sku")
    assert export_response.status_code == 200
    export_text = export_response.data.decode("utf-8-sig")
    export_stream = StringIO(export_text)
    raw_reader = csv.reader(export_stream)
    metadata_rows = []
    for row in raw_reader:
        if not row:
            break
        metadata_rows.append(row)
    assert metadata_rows
    metadata = {row[0]: row[1] if len(row) > 1 else "" for row in metadata_rows}
    assert "门店" in metadata
    assert metadata.get("统计时间范围")
    assert metadata.get("导出时间")
    header = next(raw_reader)
    assert header == [
        "SKU 名称",
        "分类",
        "单位",
        "入库数量",
        "出库数量",
        "净变动",
    ]
    export_rows = [dict(zip(header, row)) for row in raw_reader if any(row)]
    assert export_rows
    totals_row = export_rows[-1]
    assert totals_row["SKU 名称"] == "合计"
    inbound_total = int(totals_row["入库数量"])
    outbound_total = int(totals_row["出库数量"])
    assert inbound_total >= 0
    assert outbound_total >= 0


def test_transfer_api_endpoint(tmp_path: Path) -> None:
    pytest.importorskip("flask")
    from inventory_app.app import create_app

    storage = tmp_path / "data.json"
    app = create_app(storage)
    app.config.update(TESTING=True)
    client = app.test_client()

    _login(client)

    store_response = client.post("/stores", json={"name": "分店A"})
    assert store_response.status_code == 201
    store_payload = store_response.get_json()
    target_store_id = store_payload["id"]

    create_response = client.post(
        "/api/items",
        json={
            "name": "咖啡豆",
            "quantity": 8,
            "unit": "袋",
            "threshold": 2,
            "store_id": "default",
        },
    )
    assert create_response.status_code == 201

    transfer_response = client.post(
        "/api/items/咖啡豆/transfer",
        json={
            "quantity": 3,
            "source_store_id": "default",
            "target_store_id": target_store_id,
        },
    )
    assert transfer_response.status_code == 200
    transfer_payload = transfer_response.get_json()
    assert transfer_payload["source"]["quantity"] == 5
    assert transfer_payload["target"]["quantity"] == 3
    assert transfer_payload["target"]["store_id"] == target_store_id

    invalid_transfer = client.post(
        "/api/items/咖啡豆/transfer",
        json={
            "quantity": 100,
            "source_store_id": "default",
            "target_store_id": target_store_id,
        },
    )
    assert invalid_transfer.status_code == 400


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
    assert (
        lines[0]
        == "时间,操作类型,SKU 名称,操作用户,门店,分类,初始量,增减量,当前量"
    )
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
