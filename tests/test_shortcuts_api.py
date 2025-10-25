from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from inventory_app import create_app
import inventory_app.app as app_module


def _create_test_app(tmp_path: Path):
    storage = tmp_path / "inventory.json"
    user_storage = tmp_path / "users.json"
    app = create_app(storage_path=storage, user_storage_path=user_storage)
    app.config.update(TESTING=True)
    return app


def _issue_token(client) -> Dict[str, Any]:
    response = client.post(
        "/api/auth/token",
        json={"username": "admin", "password": "admin"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "success"
    assert payload["token"]
    return payload


def test_issue_api_token_and_use(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    client = app.test_client()

    token_payload = _issue_token(client)
    headers = {"Authorization": f"Bearer {token_payload['token']}"}

    items_response = client.get("/api/items", headers=headers)
    assert items_response.status_code == 200
    assert items_response.get_json() == []


def test_expired_token_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _create_test_app(tmp_path)
    client = app.test_client()

    response = client.post(
        "/api/auth/token",
        json={"username": "admin", "password": "admin", "expires_in": 1},
    )
    assert response.status_code == 200
    token = response.get_json()["token"]

    base_time = app_module.time.time()
    monkeypatch.setattr(app_module.time, "time", lambda: base_time + 7200)

    headers = {"Authorization": f"Bearer {token}"}
    expired_response = client.get("/api/items", headers=headers)
    assert expired_response.status_code == 401


def test_shortcuts_adjust_and_summary(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    client = app.test_client()

    token_payload = _issue_token(client)
    headers = {"Authorization": f"Bearer {token_payload['token']}"}

    create_response = client.post(
        "/api/shortcuts/items/adjust",
        json={"name": "测试零件", "action": "set", "quantity": 10, "unit": "件"},
        headers=headers,
    )
    assert create_response.status_code == 200
    create_payload = create_response.get_json()
    assert create_payload["status"] == "success"
    assert create_payload["action"] == "set"
    assert create_payload["item"]["quantity"] == 10
    assert create_payload["item"]["unit"] == "件"

    out_response = client.post(
        "/api/shortcuts/items/adjust",
        json={"name": "测试零件", "action": "out", "quantity": 3},
        headers=headers,
    )
    assert out_response.status_code == 200
    out_payload = out_response.get_json()
    assert out_payload["action"] == "out"
    assert out_payload["item"]["quantity"] == 7

    summary_response = client.get(
        "/api/shortcuts/items/summary",
        query_string={"name": "测试零件"},
        headers=headers,
    )
    assert summary_response.status_code == 200
    summary_payload = summary_response.get_json()
    assert summary_payload["status"] == "success"
    assert summary_payload["item"]["quantity"] == 7
    assert summary_payload["item"]["store_id"] == "default"

    profile_response = client.get("/api/shortcuts/profile", headers=headers)
    assert profile_response.status_code == 200
    profile_payload = profile_response.get_json()
    assert profile_payload["status"] == "success"
    assert profile_payload["user"]["username"] == "admin"
    assert any(store["id"] == "default" for store in profile_payload["stores"])


def test_shortcuts_adjust_with_explicit_store_selection(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    client = app.test_client()

    token_payload = _issue_token(client)
    headers = {"Authorization": f"Bearer {token_payload['token']}"}

    store_response = client.post(
        "/stores",
        json={"name": "广州门店"},
        headers=headers,
    )
    assert store_response.status_code == 201
    store_payload = store_response.get_json()
    assert store_payload["name"] == "广州门店"
    store_id = store_payload["id"]

    default_store_response = client.post(
        "/api/shortcuts/items/adjust",
        json={"name": "测试零件", "action": "set", "quantity": 5},
        headers=headers,
    )
    assert default_store_response.status_code == 200

    second_store_response = client.post(
        "/api/shortcuts/items/adjust",
        json={
            "name": "测试零件",
            "action": "set",
            "quantity": 12,
            "store_name": "广州门店",
        },
        headers=headers,
    )
    assert second_store_response.status_code == 200
    second_payload = second_store_response.get_json()
    assert second_payload["item"]["store_id"] == store_id
    assert second_payload["item"]["quantity"] == 12

    summary_default = client.get(
        "/api/shortcuts/items/summary",
        query_string={"name": "测试零件"},
        headers=headers,
    )
    assert summary_default.status_code == 200
    assert summary_default.get_json()["item"]["quantity"] == 5

    summary_named = client.get(
        "/api/shortcuts/items/summary",
        query_string={"name": "测试零件", "store_name": "广州门店"},
        headers=headers,
    )
    assert summary_named.status_code == 200
    assert summary_named.get_json()["item"]["quantity"] == 12

    out_response = client.post(
        "/api/shortcuts/items/adjust",
        json={
            "name": "测试零件",
            "action": "out",
            "quantity": 2,
            "store_id": store_id,
        },
        headers=headers,
    )
    assert out_response.status_code == 200
    assert out_response.get_json()["item"]["quantity"] == 10

    summary_id = client.get(
        "/api/shortcuts/items/summary",
        query_string={"name": "测试零件", "store_id": store_id},
        headers=headers,
    )
    assert summary_id.status_code == 200
    assert summary_id.get_json()["item"]["quantity"] == 10


def test_shortcuts_adjust_rejects_unknown_store(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    client = app.test_client()

    token_payload = _issue_token(client)
    headers = {"Authorization": f"Bearer {token_payload['token']}"}

    response = client.post(
        "/api/shortcuts/items/adjust",
        json={
            "name": "未知零件",
            "action": "set",
            "quantity": 1,
            "store_name": "不存在的门店",
        },
        headers=headers,
    )
    assert response.status_code == 404
    payload = response.get_json()
    assert payload["code"] == "store_not_found"
    assert payload["status"] == "error"
