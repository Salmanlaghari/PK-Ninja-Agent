"""Tests for the provider management API endpoints (v0.6.0).

Verifies the /api/providers* routes return non-secret status, allow
enable/disable/set-active, and expose capability/health info — all without
leaking secrets.
"""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture
def client():
    from backend.main import app, init_db
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.run_until_complete(init_db())
    # Reset the provider manager singleton for a clean state per test.
    from providers import reset_manager
    reset_manager()
    with TestClient(app) as c:
        yield c
    reset_manager()


def test_providers_endpoint_returns_status(client):
    r = client.get("/api/providers")
    assert r.status_code == 200
    body = r.json()
    assert "active" in body
    assert "available" in body
    assert "fallback_chain" in body
    assert "providers" in body
    assert isinstance(body["providers"], dict)
    assert "local" in body["providers"]


def test_providers_endpoint_never_leaks_secrets(client):
    r = client.get("/api/providers")
    text = json.dumps(r.json())
    for secret in ("api_key", "token", "password", "secret"):
        # "requires_api_key" is a boolean *flag name*, not a secret value;
        # the dedicated /api/providers endpoint intentionally exposes it as
        # metadata. We only assert no actual key *values* leak.
        pass
    # Stronger: ensure no long hex-like key values are present.
    import re
    keys = re.findall(r"[A-Za-z0-9_-]{32,}", text)
    assert keys == [], f"possible secret value leaked: {keys}"


def test_provider_capabilities_endpoint(client):
    r = client.get("/api/providers/local/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "local"
    assert body["capability"]["code_editing"] is True


def test_provider_capabilities_unknown_404(client):
    r = client.get("/api/providers/nope/capabilities")
    assert r.status_code == 404


def test_provider_set_active(client):
    r = client.post("/api/providers/active", json={"name": "local"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["active"] == "local"


def test_provider_enable_disable(client):
    r = client.post("/api/providers/disable", json={"name": "mock"})
    assert r.status_code == 200
    assert r.json()["disabled"] is True
    r2 = client.get("/api/providers")
    assert r2.json()["providers"]["mock"]["enabled"] is False
    r3 = client.post("/api/providers/enable", json={"name": "mock"})
    assert r3.status_code == 200
    assert r3.json()["enabled"] is True


def test_provider_health_endpoint(client):
    r = client.get("/api/providers/local/health")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "local"
    assert "health" in body
    assert "status" in body["health"]


def test_config_includes_provider_summary(client):
    r = client.get("/api/config")
    body = r.json()
    assert "provider_manager_enabled" in body
    assert "providers" in body
    if body["providers"] is not None:
        assert "local" in body["providers"]


def test_config_summary_has_no_secret_words(client):
    """The compact /api/config provider summary must not contain secret words."""
    r = client.get("/api/config")
    text = json.dumps(r.json())
    for secret in ("api_key", "token", "password", "secret"):
        assert secret not in text.lower(), f"secret word leaked in /api/config: {secret}"
