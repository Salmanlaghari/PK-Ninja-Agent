"""Task creation + event streaming + agent loop (offline local provider)."""
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    # Import after env is set by conftest.
    from backend.main import app, init_db
    import asyncio
    asyncio.get_event_loop().run_until_complete(init_db())
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_create_task_and_stream(client):
    r = client.post("/api/tasks", json={"description": "Add module docstrings"})
    assert r.status_code == 200
    data = r.json()
    task_id = data["task_id"]
    assert task_id

    # Give the agent thread a moment to run (it works in a local-only ws).
    time.sleep(3)

    # Fetch task detail with events.
    r2 = client.get(f"/api/tasks/{task_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["task_id"] == task_id
    assert body["description"] == "Add module docstrings"
    assert isinstance(body["events"], list)
    # The agent must have emitted at least the session_started event.
    types = [e["type"] for e in body["events"]]
    assert "session_started" in types


def test_event_types_are_real(client):
    """Events must come from actual agent activity, not faked."""
    r = client.post("/api/tasks", json={"description": "Create a README.md"})
    task_id = r.json()["task_id"]
    time.sleep(3)
    r2 = client.get(f"/api/tasks/{task_id}/events")
    events = r2.json()
    types = {e["type"] for e in events}
    # The loop always emits these from real steps.
    assert "session_started" in types
    assert "analyzing" in types
    assert "planning" in types


def test_list_tasks(client):
    client.post("/api/tasks", json={"description": "task one"})
    r = client.get("/api/tasks")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_cancel_unknown_task(client):
    r = client.post("/api/tasks/nope/cancel")
    assert r.status_code == 200
    assert r.json()["cancelled"] is False


def test_repository_endpoint_no_creds(client):
    # No GitHub creds in the test env -> configured False, no error raised.
    r = client.get("/api/repository")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert "error" in body
