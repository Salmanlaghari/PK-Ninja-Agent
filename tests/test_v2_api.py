"""New v2 API endpoints: /api/config, /api/tasks/{id}/run, WebSocket stream."""
import json
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from backend.main import app, init_db
    import asyncio
    asyncio.get_event_loop().run_until_complete(init_db())
    with TestClient(app) as c:
        yield c


# ── /api/config ──────────────────────────────────────────────────────────────
def test_config_endpoint_returns_non_secret_summary(client):
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    for key in ("provider", "model", "configured",
                "streaming_supported", "repository_configured"):
        assert key in body, f"missing key: {key}"
    # In the test env, AI_PROVIDER=local and no repo -> safe defaults.
    assert body["provider"] == "local"
    assert body["configured"] is False
    assert body["repository_configured"] is False


def test_config_endpoint_never_leaks_secrets(client):
    r = client.get("/api/config")
    body = r.json()
    # No secret-bearing keys.
    text = json.dumps(body)
    for secret in ("api_key", "token", "key", "password", "secret"):
        assert secret not in text.lower(), f"secret word leaked: {secret}"


# ── /api/tasks/{id}/run (real terminal execution) ────────────────────────────
def test_run_command_executes_real_command(client):
    """The /run endpoint runs a real sandboxed command and returns real output."""
    # Create a task first so a workspace exists.
    r = client.post("/api/tasks", json={"description": "test run endpoint"})
    task_id = r.json()["task_id"]
    time.sleep(0.5)  # let the workspace initialize

    r2 = client.post(f"/api/tasks/{task_id}/run", json={"command": "echo hello-real"})
    assert r2.status_code == 200
    body = r2.json()
    assert body["returncode"] == 0
    assert "hello-real" in body["stdout"]
    assert body["success"] is True


def test_run_command_rejects_disallowed_program(client):
    r = client.post("/api/tasks", json={"description": "test reject"})
    task_id = r.json()["task_id"]
    time.sleep(0.5)
    r2 = client.post(f"/api/tasks/{task_id}/run",
                     json={"command": "curl https://example.com"})
    assert r2.status_code == 400
    body = r2.json()
    assert body["success"] is False
    assert "error" in body


def test_run_command_unknown_task_404(client):
    r = client.post("/api/tasks/no-such-task/run", json={"command": "echo x"})
    assert r.status_code == 404


def test_run_command_runs_in_workspace(client):
    """The command must execute inside the task workspace (pwd = workspace)."""
    r = client.post("/api/tasks", json={"description": "workspace pwd test"})
    task_id = r.json()["task_id"]
    time.sleep(0.5)
    # Write a marker file via the run endpoint, then list it.
    client.post(f"/api/tasks/{task_id}/run",
                json={"command": "echo marker > probe.txt"})
    r2 = client.post(f"/api/tasks/{task_id}/run", json={"command": "ls"})
    assert "probe.txt" in r2.json()["stdout"]


# ── WebSocket stream ─────────────────────────────────────────────────────────
def _collect_ws_events(ws, deadline_seconds=20):
    """Collect events from a WebSocket until a terminal event or timeout.

    The server sends a 'ping' keepalive when no event is ready and closes the
    socket after completed/error/cancelled, so we read in a tight loop and
    bail on disconnect or deadline.
    """
    seen = []
    end = time.time() + deadline_seconds
    while time.time() < end:
        try:
            msg = ws.receive_text()
        except Exception:
            break  # disconnected or closed
        try:
            obj = json.loads(msg)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "ping":
            continue
        seen.append(obj)
        if obj.get("type") in ("completed", "error", "cancelled"):
            break
    return seen


def test_websocket_streams_events_for_a_task(client):
    """A WebSocket connection should receive live agent events.

    The WS replays recent history on connect, so even a late subscriber gets
    the session_started event that the agent emitted from real activity.
    """
    r = client.post("/api/tasks", json={"description": "ws stream test"})
    task_id = r.json()["task_id"]
    # Give the agent a moment to emit at least session_started.
    time.sleep(1.5)

    with client.websocket_connect(f"/api/tasks/{task_id}/ws") as ws:
        events = _collect_ws_events(ws, deadline_seconds=20)
    types = {e.get("type") for e in events}
    # The agent always emits session_started from real activity.
    assert "session_started" in types, f"got types: {types}"


def test_websocket_can_send_cancel(client):
    """The client can send {"type":"cancel"} over WS without error."""
    r = client.post("/api/tasks", json={"description": "ws cancel test"})
    task_id = r.json()["task_id"]

    with client.websocket_connect(f"/api/tasks/{task_id}/ws") as ws:
        # Send the cancel control message.
        ws.send_text(json.dumps({"type": "cancel"}))
        events = _collect_ws_events(ws, deadline_seconds=20)
    types = {e.get("type") for e in events}
    # The key contract: the WS accepted the cancel message and the session
    # started event was delivered (real activity). The task either got
    # cancelled mid-run or completed normally depending on timing.
    assert "session_started" in types, f"got types: {types}"


# ── SSE stream still works ───────────────────────────────────────────────────
def test_sse_stream_returns_event_stream(client):
    """The SSE endpoint is an infinite event-stream; we verify it via the
    async generator directly (the sync TestClient blocks on the infinite
    generator's body, so we drive one iteration of the underlying async gen
    with asyncio to confirm it yields real data: lines from history)."""
    import asyncio
    from backend.main import api_task_stream, BUS
    from models import EventType
    from starlette.requests import Request

    r = client.post("/api/tasks", json={"description": "sse test"})
    task_id = r.json()["task_id"]
    # Inject a real event into the bus so history is non-empty.
    from agent import Event
    BUS.publish(Event(task_id, EventType.session_started,
                      "test session", data={"provider": "local"}))
    time.sleep(0.2)

    # Build a minimal fake Request whose is_disconnected() returns False.
    class FakeRequest:
        async def is_disconnected(self):
            return False

    loop = asyncio.new_event_loop()
    response = loop.run_until_complete(
        api_task_stream(task_id, FakeRequest()))
    # The StreamingResponse wraps an async generator; pull one item.
    body_gen = response.body_iterator

    async def _pull():
        first = None
        async for chunk in body_gen:
            first = chunk
            break
        return first

    first_chunk = loop.run_until_complete(_pull())
    loop.close()
    assert first_chunk is not None
    assert b"data:" in first_chunk if isinstance(first_chunk, bytes) else "data:" in first_chunk
    assert "text/event-stream" in response.media_type
