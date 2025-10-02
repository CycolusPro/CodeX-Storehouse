from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_inventory_flow(client: AsyncClient) -> None:
    product_payload = {
        "sku": "SBX-COFFEE-BEAN",
        "name": "Espresso Roast Beans",
        "description": "Dark roast whole bean coffee",
        "unit": "bag",
        "reorder_point": 5,
    }
    location_payload = {
        "name": "Flagship Store",
        "type": "store",
        "address": "1 Market Street",
    }

    product_response = await client.post("/products", json=product_payload)
    assert product_response.status_code == 201
    product = product_response.json()

    location_response = await client.post("/locations", json=location_payload)
    assert location_response.status_code == 201
    location = location_response.json()

    adjustment_payload = {
        "product_id": product["id"],
        "location_id": location["id"],
        "quantity_change": 10,
        "reason": "initial_stock",
    }

    adjustment_response = await client.post("/inventory/adjustments", json=adjustment_payload)
    assert adjustment_response.status_code == 201

    balances_response = await client.get("/inventory/balances")
    assert balances_response.status_code == 200
    balances = balances_response.json()
    assert balances[0]["quantity"] == 10

    # Perform a stock-out adjustment
    stock_out_response = await client.post(
        "/inventory/adjustments",
        json={
            "product_id": product["id"],
            "location_id": location["id"],
            "quantity_change": -6,
            "reason": "daily_sales",
        },
    )
    assert stock_out_response.status_code == 201

    low_stock_response = await client.get("/inventory/low-stock")
    assert low_stock_response.status_code == 200
    low_stock_items = low_stock_response.json()
    assert len(low_stock_items) == 1
    assert low_stock_items[0]["quantity"] == 4


@pytest.mark.asyncio
async def test_cannot_reduce_below_zero(client: AsyncClient) -> None:
    product_response = await client.post(
        "/products",
        json={"sku": "MILK-001", "name": "Whole Milk", "unit": "bottle"},
    )
    product = product_response.json()

    location_response = await client.post(
        "/locations",
        json={"name": "Cold Room", "type": "warehouse"},
    )
    location = location_response.json()

    response = await client.post(
        "/inventory/adjustments",
        json={
            "product_id": product["id"],
            "location_id": location["id"],
            "quantity_change": -1,
            "reason": "spoilage",
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert "Cannot reduce" in body["detail"]
