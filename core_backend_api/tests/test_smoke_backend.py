import os
import json

import pytest
from fastapi.testclient import TestClient

# Import app and Mongo manager
import sys
sys.path.append("src")
from api.main import app, MongoClientManager  # type: ignore


def _env_has_mongo() -> bool:
    """Helper: check if env declares Mongo connection details."""
    return bool(os.getenv("MONGODB_URL") and os.getenv("MONGODB_DB"))


@pytest.fixture(scope="session")
def test_client_with_real_startup():
    """
    Provide a TestClient that runs the startup event (Mongo init and index creation).
    Skip if Mongo env is not provided to avoid failing startup in CI with no DB.
    """
    if not _env_has_mongo():
        pytest.skip("Skipping tests requiring real MongoDB: MONGODB_URL/MONGODB_DB not set")
    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="session")
def test_client_without_db(monkeypatch: pytest.MonkeyPatch):
    """
    Provide a TestClient that bypasses MongoDB initialization by no-op'ing
    MongoClientManager.init and returning simple stubs for db calls when accessed.
    This allows testing endpoints that don't require DB (e.g., /health, WebSocket).
    """
    # No-op Mongo init
    monkeypatch.setattr(MongoClientManager, "init", lambda: None, raising=True)

    class _DummyDB:
        async def create_index(self, *args, **kwargs):
            return None

    class _DummyCollection:
        async def create_index(self, *args, **kwargs):
            return None

        async def find_one(self, *args, **kwargs):
            return {}

        def find(self, *args, **kwargs):
            # Async iterator with no results
            async def _aiter():
                if False:
                    yield None
            return _aiter()

        async def count_documents(self, *args, **kwargs):
            return 0

        async def update_one(self, *args, **kwargs):
            class Res:
                modified_count = 1
            return Res()

        async def insert_one(self, *args, **kwargs):
            class Res:
                inserted_id = "dummy"
            return Res()

        async def find_one_and_update(self, *args, **kwargs):
            return {
                "_id": "dummy",
                "user_id": "u",
                "customer_number": "c",
                "direction": "outbound",
                "status": "initiated",
            }

        def sort(self, *args, **kwargs):
            return self

        def limit(self, *args, **kwargs):
            return self

    class _DummyDBRoot(dict):
        def __getitem__(self, item):
            return _DummyCollection()

    monkeypatch.setattr(MongoClientManager, "db", classmethod(lambda cls: _DummyDBRoot()))
    monkeypatch.setattr(MongoClientManager, "client", classmethod(lambda cls: object()))

    with TestClient(app) as client:
        yield client


def test_health_ok(test_client_without_db: TestClient):
    # Health endpoint does not require DB
    res = test_client_without_db.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data.get("status") == "ok"
    assert "timestamp" in data


@pytest.mark.skipif(not _env_has_mongo(), reason="MongoDB env not configured")
def test_db_connectivity_and_list_endpoints(test_client_with_real_startup: TestClient):
    # users list should return array
    res_users = test_client_with_real_startup.get("/users")
    assert res_users.status_code == 200
    assert isinstance(res_users.json(), list)

    # calls list should return array
    res_calls = test_client_with_real_startup.get("/calls")
    assert res_calls.status_code == 200
    assert isinstance(res_calls.json(), list)


@pytest.mark.skipif(not _env_has_mongo(), reason="MongoDB env not configured")
def test_calls_crud_minimal_flow(test_client_with_real_startup: TestClient):
    # create a call
    payload = {
        "user_id": "user-smoke",
        "customer_number": "+15551234567",
        "direction": "outbound",
        "metadata": {"smoke": True},
    }
    res_create = test_client_with_real_startup.post("/calls", json=payload)
    assert res_create.status_code == 200, res_create.text
    created = res_create.json()
    assert created["status"] == "initiated"
    assert created["user_id"] == payload["user_id"]
    call_id = created["id"]

    # list calls and verify the created one appears
    res_list = test_client_with_real_startup.get("/calls", params={"user_id": payload["user_id"], "limit": 10})
    assert res_list.status_code == 200
    calls = res_list.json()
    assert isinstance(calls, list)
    ids = {c["id"] for c in calls}
    assert call_id in ids


def test_websocket_connect_and_receive_ack(test_client_without_db: TestClient):
    # Using TestClient's WebSocket session
    with test_client_without_db.websocket_connect("/ws/events") as ws:
        # First message should be a 'system.connected' event
        msg = ws.receive_text()
        data = json.loads(msg)
        assert data.get("type") == "system"
        assert data.get("event") == "connected"

        # Send a ping and expect an ack
        ws.send_text("ping")
        ack = json.loads(ws.receive_text())
        assert ack.get("type") == "system"
        assert ack.get("event") == "ack"
        assert ack.get("message") == "ping"
