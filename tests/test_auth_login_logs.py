import json
from pathlib import Path

from inventory_app import create_app
from inventory_app.auth import UserManager


def _perform_login(client) -> None:
    response = client.post(
        "/login",
        data={"username": "admin", "password": "admin"},
        headers={
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
            ),
            "X-Forwarded-For": "203.0.113.10",
        },
    )
    assert response.status_code in {302, 303}


def test_login_success_creates_log(tmp_path: Path) -> None:
    storage = tmp_path / "inventory.json"
    user_storage = tmp_path / "users.json"
    app = create_app(storage_path=storage, user_storage_path=user_storage)
    app.config.update(TESTING=True, SERVER_NAME="localhost")

    client = app.test_client()
    _perform_login(client)

    log_path = user_storage.with_name("login_logs.json")
    assert log_path.exists()
    payload = json.loads(log_path.read_text(encoding="utf-8"))
    assert len(payload) == 1
    entry = payload[0]
    assert entry["username"] == "admin"
    assert entry["ip_address"] == "203.0.113.10"
    assert entry["timestamp"]
    assert entry["client_type"] in {"移动端", "平板端", "桌面端", "未知"}
    assert entry["event_type"] == "login"
    assert entry["path"] == "/login"
    assert entry["method"] == "POST"


def test_authenticated_request_logs_access_event(tmp_path: Path) -> None:
    storage = tmp_path / "inventory.json"
    user_storage = tmp_path / "users.json"
    app = create_app(storage_path=storage, user_storage_path=user_storage)
    app.config.update(TESTING=True, SERVER_NAME="localhost")

    client = app.test_client()
    _perform_login(client)

    response = client.get("/users")
    assert response.status_code == 200

    log_path = user_storage.with_name("login_logs.json")
    payload = json.loads(log_path.read_text(encoding="utf-8"))
    assert len(payload) >= 2
    last_entry = payload[-1]
    assert last_entry["event_type"] == "access"
    assert last_entry["path"] == "/users"
    assert last_entry["method"] == "GET"


def test_clear_login_logs_route(tmp_path: Path) -> None:
    storage = tmp_path / "inventory.json"
    user_storage = tmp_path / "users.json"
    app = create_app(storage_path=storage, user_storage_path=user_storage)
    app.config.update(TESTING=True, SERVER_NAME="localhost")

    client = app.test_client()
    _perform_login(client)

    clear_response = client.post("/admin/login-logs/clear", follow_redirects=False)
    assert clear_response.status_code in {302, 303}

    log_path = user_storage.with_name("login_logs.json")
    payload = json.loads(log_path.read_text(encoding="utf-8"))
    assert payload == []


def test_login_records_are_sorted(tmp_path: Path) -> None:
    user_storage = tmp_path / "users.json"
    manager = UserManager(user_storage)

    manager.record_login("admin", ip_address="1.1.1.1", user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120")
    manager.record_login("admin", ip_address="2.2.2.2", user_agent="Mozilla/5.0 (iPad; CPU OS 16_0) Safari/605.1.15")

    records = manager.list_login_records()
    assert len(records) == 2
    assert records[0].ip_address == "2.2.2.2"
    assert records[0].client_type in {"平板端", "移动端", "桌面端"}
    assert records[0].event_type == "login"
