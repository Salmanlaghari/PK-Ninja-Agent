"""Serverless deadline guard: POST /api/tasks must return a visible failure
instead of being silently killed by the platform time limit.

Regression test for the "chat box shows no response" bug on Vercel: when the
AI provider call exceeds the function deadline, the endpoint now enforces its
own shorter deadline, marks the task failed, emits an error event, and returns
a normal JSON response.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


class _FakeRT:
    def __init__(self):
        self.status = None

        class _Cancel:
            def set(self):
                pass

        self.cancel = _Cancel()


class _HangingAgent:
    """Stands in for Agent: run() hangs far past the deadline."""

    calls = []

    def __init__(self, task_id, description, repo_full=None, settings=None):
        self.task_id = task_id
        self.description = description
        self.rt = _FakeRT()
        self.emitted = []
        _HangingAgent.calls.append(self)

    def run(self):
        time.sleep(30)  # longer than any deadline used in tests

    def emit(self, ev_type, message, **kwargs):
        self.emitted.append((ev_type, message, kwargs))


@pytest.fixture
def serverless_env(monkeypatch):
    monkeypatch.setenv("PK_NINJA_SERVERLESS", "1")
    monkeypatch.setenv("VERCEL_FUNCTION_MAX_DURATION", "10")  # deadline = 5s
    from config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_serverless_deadline_returns_failed_response(serverless_env, monkeypatch):
    import agent as agent_mod
    import main as main_mod
    from fastapi.testclient import TestClient
    from models import EventType

    monkeypatch.setattr(agent_mod, "Agent", _HangingAgent)
    _HangingAgent.calls = []

    with TestClient(main_mod.app) as client:
        t0 = time.time()
        r = client.post("/api/tasks", json={"description": "hello"})
        elapsed = time.time() - t0

    assert r.status_code == 200
    body = r.json()
    assert body["sync"] is True
    # Deadline hit -> the endpoint reports failure instead of hanging/being killed.
    assert body["status"] == "failed"
    # Must return near the deadline (5s), not hang for the full agent run (30s).
    assert elapsed < 15, f"endpoint took {elapsed:.1f}s — deadline not enforced"

    # A visible error event must have been emitted for the user.
    assert _HangingAgent.calls, "agent was never constructed"
    emitted_types = [e[0] for e in _HangingAgent.calls[0].emitted]
    assert EventType.error in emitted_types
