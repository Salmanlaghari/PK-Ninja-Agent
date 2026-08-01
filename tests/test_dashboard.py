"""Tests for the Dashboard & System Health endpoints (v0.7.0 Phase 5)."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))


def _clear_settings_cache() -> None:
    try:
        import config as _c
        _c.get_settings.cache_clear()
    except Exception:  # noqa: BLE001
        pass


def _build_client(monkeypatch=None):
    import main as _main
    importlib.reload(_main)
    from fastapi.testclient import TestClient
    return TestClient(_main.app)


class TestDashboard:
    def test_dashboard_shape(self, monkeypatch):
        c = _build_client(monkeypatch)
        r = c.get("/api/dashboard")
        assert r.status_code == 200
        d = r.json()
        for k in ["recent_tasks", "active_tasks", "agent_status",
                  "workspace_status", "git_status", "provider_status",
                  "system_health", "multi_agent_enabled"]:
            assert k in d, f"missing key {k}"
        assert d["agent_status"] in ("idle", "busy")
        assert isinstance(d["recent_tasks"], list)
        assert isinstance(d["active_tasks"], list)
        assert isinstance(d["system_health"], list)
        assert isinstance(d["multi_agent_enabled"], bool)

    def test_dashboard_task_items_shape(self, monkeypatch):
        c = _build_client(monkeypatch)
        # Create a task so there's something in recent_tasks
        r = c.post("/api/tasks", json={"prompt": "dashboard test", "repo": None})
        if r.status_code == 200:
            tid = r.json().get("task_id")
            assert tid
        r = c.get("/api/dashboard")
        d = r.json()
        if d["recent_tasks"]:
            item = d["recent_tasks"][0]
            for k in ["task_id", "description", "status", "created_at"]:
                assert k in item

    def test_dashboard_workspace_status(self, monkeypatch):
        c = _build_client(monkeypatch)
        c.post("/api/workspaces", json={"name": "dashws", "repo": None})
        r = c.get("/api/dashboard")
        ws = r.json()["workspace_status"]
        assert ws["count"] >= 1
        assert "dashws" in ws["names"]
        # cleanup
        c.delete("/api/workspaces/dashws")

    def test_dashboard_agent_status_idle_default(self, monkeypatch):
        c = _build_client(monkeypatch)
        r = c.get("/api/dashboard")
        # No running tasks by default → idle
        assert r.json()["agent_status"] == "idle"

    def test_dashboard_multi_agent_flag(self, monkeypatch):
        c = _build_client(monkeypatch)
        r = c.get("/api/dashboard")
        # Default config has multi_agent disabled
        assert r.json()["multi_agent_enabled"] is False


class TestSystemHealth:
    def test_system_health_shape(self, monkeypatch):
        c = _build_client(monkeypatch)
        r = c.get("/api/system/health")
        assert r.status_code == 200
        d = r.json()
        for k in ["status", "version", "environment", "components", "startup_checks"]:
            assert k in d
        assert d["status"] in ("ok", "degraded", "down", "unknown")
        assert d["version"] == "0.8.0"
        assert isinstance(d["components"], list)

    def test_system_health_components_detail(self, monkeypatch):
        c = _build_client(monkeypatch)
        d = c.get("/api/system/health").json()
        for comp in d["components"]:
            assert "name" in comp
            assert "status" in comp

    def test_system_health_public_no_auth(self, monkeypatch):
        """System health should be accessible even with auth enabled."""
        import os
        os.environ["AUTH_ENABLED"] = "true"
        os.environ["AUTH_SECRET"] = "test-secret-for-health-123456"
        _clear_settings_cache()
        c = _build_client(monkeypatch)
        r = c.get("/api/system/health")
        assert r.status_code == 200
        # cleanup
        os.environ.pop("AUTH_ENABLED", None)
        os.environ.pop("AUTH_SECRET", None)
        _clear_settings_cache()


class TestDashboardNoSecretLeak:
    @pytest.mark.parametrize("path", ["/api/dashboard", "/api/system/health"])
    def test_no_secret_substrings(self, monkeypatch, path):
        c = _build_client(monkeypatch)
        body = c.get(path).text.lower()
        for bad in ["api_key", "token", "password", "secret"]:
            assert bad not in body, f"'{bad}' leaked in {path}"


class TestDashboardAuthCompat:
    def test_dashboard_requires_auth_when_enabled(self, monkeypatch):
        import os
        os.environ["AUTH_ENABLED"] = "true"
        os.environ["AUTH_SECRET"] = "test-secret-dash-1234567890"
        _clear_settings_cache()
        c = _build_client(monkeypatch)
        r = c.get("/api/dashboard")
        assert r.status_code == 401
        os.environ.pop("AUTH_ENABLED", None)
        os.environ.pop("AUTH_SECRET", None)
        _clear_settings_cache()

    def test_dashboard_guest_works(self, monkeypatch):
        import os
        os.environ["AUTH_ENABLED"] = "true"
        os.environ["AUTH_GUEST_ALLOWED"] = "true"
        os.environ["AUTH_SECRET"] = "test-secret-dash2-1234567890"
        _clear_settings_cache()
        c = _build_client(monkeypatch)
        r = c.post("/api/auth/guest", json={"display_name": "DashUser"})
        assert r.status_code == 200
        token = r.json()["session"]
        h = {"Authorization": f"Bearer {token}"}
        r = c.get("/api/dashboard", headers=h)
        assert r.status_code == 200
        os.environ.pop("AUTH_ENABLED", None)
        os.environ.pop("AUTH_GUEST_ALLOWED", None)
        os.environ.pop("AUTH_SECRET", None)
        _clear_settings_cache()
