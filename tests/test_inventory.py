from io import BytesIO, StringIO
import csv
import xlrd
import xlwt
from pathlib import Path
from typing import List

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


def test_delete_category_cascade_scoped_to_store(tmp_path: Path) -> None:
    storage = tmp_path / "data.json"
    manager = InventoryManager(storage)

    other_store = manager.create_store("南区仓库")
    # Assign the same category to items in two different stores
    item_default = manager.set_quantity("苹果", 5, category="生鲜", store_id="default")
    item_other = manager.set_quantity(
        "香蕉", 7, category="生鲜", store_id=other_store["id"]
    )
    assert item_default.category == item_other.category

    manager.delete_category(
        item_default.category,
        cascade=True,
        store_id="default",
    )

    with pytest.raises(KeyError):
        manager.get_item("苹果", store_id="default")

    remaining = manager.get_item("香蕉", store_id=other_store["id"])
    assert remaining.quantity == 7
    assert remaining.category == "uncategorized"


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


def test_history_export_xls_format(tmp_path: Path) -> None:
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

    workbook = xlrd.open_workbook(file_contents=response.data)
    sheet = workbook.sheet_by_index(0)
    header = [str(value).strip() for value in sheet.row_values(0)]
    assert header == [
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
    rows = []
    for row_index in range(1, sheet.nrows):
        row_values = sheet.row_values(row_index)
        if not any(str(value).strip() for value in row_values):
            continue
        rows.append(dict(zip(header, row_values)))
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
    workbook = xlrd.open_workbook(file_contents=export_response.data)
    sheet = workbook.sheet_by_index(0)
    metadata_rows = []
    row_index = 0
    while row_index < sheet.nrows:
        row_values = sheet.row_values(row_index)
        if not any(str(value).strip() for value in row_values):
            row_index += 1
            break
        metadata_rows.append(row_values)
        row_index += 1
    assert metadata_rows
    metadata = {
        str(row[0]).strip(): row[1] if len(row) > 1 else ""
        for row in metadata_rows
    }
    assert "门店" in metadata
    assert metadata.get("统计时间范围")
    assert metadata.get("导出时间")
    header = [str(value).strip() for value in sheet.row_values(row_index)]
    assert header == [
        "SKU 名称",
        "分类",
        "单位",
        "入库数量",
        "出库数量",
        "净变动",
        "截止库存",
    ]
    row_index += 1
    export_rows = []
    for idx in range(row_index, sheet.nrows):
        row_values = sheet.row_values(idx)
        if not any(str(value).strip() for value in row_values):
            continue
        export_rows.append(dict(zip(header, row_values)))
    assert export_rows
    totals_row = export_rows[-1]
    assert totals_row["SKU 名称"] == "合计"
    inbound_total = int(totals_row["入库数量"])
    outbound_total = int(totals_row["出库数量"])
    assert inbound_total >= 0
    assert outbound_total >= 0
    cutoff_total = int(totals_row["截止库存"])
    assert cutoff_total >= 0


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
        json={
            "items": [
                {"name": "薯片", "quantity": 12, "unit": "箱", "category": "零食"},
                {"name": "咖啡豆", "quantity": 5, "unit": "袋", "category": "饮料", "threshold": 2},
                {"name": "绿茶", "quantity": 9, "unit": "瓶", "category": "饮料"},
            ]
        },
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["count"] == 3

    export_resp = client.get("/api/items/export")
    assert export_resp.status_code == 200
    assert "inventory_export" in export_resp.headers["Content-Disposition"]
    export_book = xlrd.open_workbook(file_contents=export_resp.data, formatting_info=True)
    export_sheet = export_book.sheet_by_index(0)
    title_row = [str(value).strip() for value in export_sheet.row_values(0)]
    assert title_row[0] == "星选送库存盘点表"

    header_row_index = None
    export_header: List[str] = []
    for row_idx in range(export_sheet.nrows):
        row_values = [str(value).strip() for value in export_sheet.row_values(row_idx)]
        if "商品名称" in row_values and "库存数量" in row_values:
            export_header = row_values
            header_row_index = row_idx
            break
    assert header_row_index is not None
    assert export_header == ["门店", "分类", "商品名称", "库存数量", "单位"]

    header_index = {value: idx for idx, value in enumerate(export_header)}
    data_rows = []
    last_store = ""
    last_category = ""
    for row_idx in range(header_row_index + 1, export_sheet.nrows):
        row_values = export_sheet.row_values(row_idx)
        if not any(str(value).strip() for value in row_values):
            continue
        store_cell = export_sheet.cell_value(row_idx, header_index["门店"])
        category_cell = export_sheet.cell_value(row_idx, header_index["分类"])
        if str(store_cell).strip():
            last_store = str(store_cell).strip()
        if str(category_cell).strip():
            last_category = str(category_cell).strip()
        data_rows.append(
            {
                "excel_row": row_idx,
                "门店": last_store,
                "分类": last_category or "未分类",
                "商品名称": str(export_sheet.cell_value(row_idx, header_index["商品名称"])).strip(),
                "库存数量": int(export_sheet.cell_value(row_idx, header_index["库存数量"])),
                "单位": str(export_sheet.cell_value(row_idx, header_index["单位"])).strip(),
            }
        )

    assert len(data_rows) == payload["count"]
    categories = [row["分类"] for row in data_rows]
    assert categories == sorted(categories)

    from itertools import groupby

    for category, group in groupby(data_rows, key=lambda row: row["分类"]):
        quantities = [row["库存数量"] for row in group]
        assert quantities == sorted(quantities, reverse=True)

    merged_ranges = {
        (rlow, rhigh, clow, chigh)
        for rlow, rhigh, clow, chigh in getattr(export_sheet, "merged_cells", [])
    }
    data_start_row = header_row_index + 1
    data_end_row = data_start_row + len(data_rows)
    store_col = header_index["门店"]
    category_col = header_index["分类"]
    assert (data_start_row, data_end_row, store_col, store_col + 1) in merged_ranges

    category_offsets = {}
    for index, row in enumerate(data_rows):
        category_offsets.setdefault(row["分类"], []).append(index)
    for offsets in category_offsets.values():
        start_index = offsets[0]
        end_index = offsets[-1]
        start_row = data_start_row + start_index
        end_row = data_start_row + end_index + 1
        assert (start_row, end_row, category_col, category_col + 1) in merged_ranges

    template_resp = client.get("/api/items/template")
    assert template_resp.status_code == 200
    template_book = xlrd.open_workbook(file_contents=template_resp.data)
    template_sheet = template_book.sheet_by_index(0)
    template_header = [str(value).strip() for value in template_sheet.row_values(0)]
    assert template_header == ["名称", "数量", "单位", "阈值提醒", "库存分类"]


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
    export_book = xlrd.open_workbook(file_contents=export_resp.data)
    export_sheet = export_book.sheet_by_index(0)
    header = [str(value).strip() for value in export_sheet.row_values(0)]
    assert header == [
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
    records = []
    for row_idx in range(1, export_sheet.nrows):
        row_values = export_sheet.row_values(row_idx)
        if not any(str(value).strip() for value in row_values):
            continue
        records.append(dict(zip(header, row_values)))
    assert any(row.get("SKU 名称") == "咖啡豆" for row in records)
    assert any(
        row.get("操作类型") in {"入库", "出库"}
        for row in records
    )


def test_import_form_endpoint(tmp_path: Path) -> None:
    pytest.importorskip("flask")
    from inventory_app.app import create_app

    storage = tmp_path / "data.json"
    app = create_app(storage)
    app.config.update(TESTING=True)
    client = app.test_client()

    _login(client)

    workbook = xlwt.Workbook()
    sheet = workbook.add_sheet("Sheet1")
    header = ["名称", "数量", "单位", "阈值提醒"]
    for index, value in enumerate(header):
        sheet.write(0, index, value)
    sheet.write(1, 0, "茶叶")
    sheet.write(1, 1, 8)
    sheet.write(1, 2, "罐")
    sheet.write(1, 3, 3)
    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    response = client.post(
        "/import",
        data={"file": (buffer, "bulk.xls")},
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
    from inventory_app.app import create_app, _parse_xls_rows

    storage = tmp_path / "data.json"
    app = create_app(storage)
    app.config.update(TESTING=True)
    client = app.test_client()

    _login(client)

    template_resp = client.get("/api/items/template")
    assert template_resp.status_code == 200

    template_book = xlrd.open_workbook(file_contents=template_resp.data)
    template_sheet = template_book.sheet_by_index(0)
    header = [str(value).strip() for value in template_sheet.row_values(0)]
    assert header == ["名称", "数量", "单位", "阈值提醒", "库存分类"]
    edited_book = xlwt.Workbook()
    edited_sheet = edited_book.add_sheet("Sheet1")
    for index, value in enumerate(header):
        edited_sheet.write(0, index, value)
    edited_sheet.write(1, 0, "新品饮料")
    edited_sheet.write(1, 1, 12)
    edited_sheet.write(1, 2, "箱")
    edited_sheet.write(1, 3, 3)
    edited_sheet.write(1, 4, "饮料")
    edited_buffer = BytesIO()
    edited_book.save(edited_buffer)
    edited_buffer.seek(0)

    parsed_rows = _parse_xls_rows(edited_buffer.getvalue())
    assert parsed_rows and parsed_rows[0]["name"] == "新品饮料"

    edited_buffer.seek(0)
    response = client.post(
        "/import",
        data={"file": (edited_buffer, "bulk.xls")},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert response.status_code == 302

    items_response = client.get("/api/items")
    items = items_response.get_json()
    assert any(item["name"] == "新品饮料" for item in items)
