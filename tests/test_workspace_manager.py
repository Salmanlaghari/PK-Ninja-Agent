"""Tests for the Workspace Manager (v0.7.0 Phase 3).

Covers:
* list / create / rename / delete / switch / recent API routes
* direct manager functions (sandboxing, validation, recents)
* path-traversal protection
* recent-workspaces persistence
* secret-leak guard (no api_key/token/etc. in responses)
* backward compatibility with auth disabled
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure backend importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))


def _clear_settings_cache() -> None:
    try:
        import config as _config
        _config.get_settings.cache_clear()
    except Exception:  # noqa: BLE001
        pass


def _set_env(monkeypatch, **kwargs) -> None:
    """Set workspace root + db path in a tmp dir; clear caches."""
    tmp = Path(tempfile.mkdtemp(prefix="wsm_test_"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp / "workspaces"))
    monkeypatch.setenv("DB_PATH", str(tmp / "test.db"))
    for k, v in kwargs.items():
        monkeypatch.setenv(k, str(v))
    _clear_settings_cache()


def _build_client():
    """Reimport main so it picks up env changes; return a TestClient."""
    import main as _main
    importlib.reload(_main)
    from fastapi.testclient import TestClient
    return TestClient(_main.app)


# ── API route tests (auth disabled by default) ────────────────────────────────
class TestWorkspaceRoutes:
    def test_list_empty(self, monkeypatch):
        _set_env(monkeypatch)
        c = _build_client()
        r = c.get("/api/workspaces")
        assert r.status_code == 200
        data = r.json()
        assert "workspaces" in data
        assert data["workspaces"] == []

    def test_create_and_list(self, monkeypatch):
        _set_env(monkeypatch)
        c = _build_client()
        r = c.post("/api/workspaces", json={"name": "alpha", "repo": None})
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "alpha"
        assert body["is_git_repo"] is False
        assert body["file_count"] == 0
        # Listed
        r = c.get("/api/workspaces")
        names = [w["name"] for w in r.json()["workspaces"]]
        assert "alpha" in names

    def test_create_duplicate_400(self, monkeypatch):
        _set_env(monkeypatch)
        c = _build_client()
        assert c.post("/api/workspaces", json={"name": "dup", "repo": None}).status_code == 200
        r = c.post("/api/workspaces", json={"name": "dup", "repo": None})
        assert r.status_code == 400
        assert "already exists" in r.json()["detail"]

    def test_rename(self, monkeypatch):
        _set_env(monkeypatch)
        c = _build_client()
        c.post("/api/workspaces", json={"name": "oldname", "repo": None})
        r = c.put("/api/workspaces", json={"old_name": "oldname", "new_name": "newname"})
        assert r.status_code == 200
        assert r.json()["name"] == "newname"
        # Old gone, new present
        names = [w["name"] for w in c.get("/api/workspaces").json()["workspaces"]]
        assert "newname" in names
        assert "oldname" not in names

    def test_rename_missing_400(self, monkeypatch):
        _set_env(monkeypatch)
        c = _build_client()
        r = c.put("/api/workspaces", json={"old_name": "ghost", "new_name": "x"})
        assert r.status_code == 400

    def test_rename_to_existing_400(self, monkeypatch):
        _set_env(monkeypatch)
        c = _build_client()
        c.post("/api/workspaces", json={"name": "a", "repo": None})
        c.post("/api/workspaces", json={"name": "b", "repo": None})
        r = c.put("/api/workspaces", json={"old_name": "a", "new_name": "b"})
        assert r.status_code == 400

    def test_delete(self, monkeypatch):
        _set_env(monkeypatch)
        c = _build_client()
        c.post("/api/workspaces", json={"name": "todelete", "repo": None})
        r = c.delete("/api/workspaces/todelete")
        assert r.status_code == 200
        assert r.json()["name"] == "todelete"
        names = [w["name"] for w in c.get("/api/workspaces").json()["workspaces"]]
        assert "todelete" not in names

    def test_delete_missing_400(self, monkeypatch):
        _set_env(monkeypatch)
        c = _build_client()
        r = c.delete("/api/workspaces/ghost")
        assert r.status_code == 400

    def test_switch(self, monkeypatch):
        _set_env(monkeypatch)
        c = _build_client()
        c.post("/api/workspaces", json={"name": "sw", "repo": None})
        r = c.post("/api/workspaces/switch", json={"name": "sw"})
        assert r.status_code == 200
        assert r.json()["name"] == "sw"

    def test_switch_missing_400(self, monkeypatch):
        _set_env(monkeypatch)
        c = _build_client()
        r = c.post("/api/workspaces/switch", json={"name": "ghost"})
        assert r.status_code == 400

    def test_recent_after_switch(self, monkeypatch):
        _set_env(monkeypatch)
        c = _build_client()
        c.post("/api/workspaces", json={"name": "r1", "repo": None})
        c.post("/api/workspaces", json={"name": "r2", "repo": None})
        c.post("/api/workspaces/switch", json={"name": "r1"})
        c.post("/api/workspaces/switch", json={"name": "r2"})
        r = c.get("/api/workspaces/recent")
        assert r.status_code == 200
        recents = [w["name"] for w in r.json()["workspaces"]]
        # r2 was switched last → should be first
        assert recents[0] == "r2"
        assert "r1" in recents

    def test_recent_excludes_deleted(self, monkeypatch):
        _set_env(monkeypatch)
        c = _build_client()
        c.post("/api/workspaces", json={"name": "rdel", "repo": None})
        c.post("/api/workspaces/switch", json={"name": "rdel"})
        c.delete("/api/workspaces/rdel")
        r = c.get("/api/workspaces/recent")
        recents = [w["name"] for w in r.json()["workspaces"]]
        assert "rdel" not in recents


class TestWorkspaceValidation:
    def test_path_traversal_rejected(self, monkeypatch):
        _set_env(monkeypatch)
        c = _build_client()
        r = c.post("/api/workspaces", json={"name": "../escape", "repo": None})
        assert r.status_code == 400

    def test_dot_name_rejected(self, monkeypatch):
        _set_env(monkeypatch)
        c = _build_client()
        r = c.post("/api/workspaces", json={"name": ".hidden", "repo": None})
        assert r.status_code == 400

    def test_separator_in_name_rejected(self, monkeypatch):
        _set_env(monkeypatch)
        c = _build_client()
        r = c.post("/api/workspaces", json={"name": "a/b", "repo": None})
        assert r.status_code == 400

    def test_empty_name_422(self, monkeypatch):
        _set_env(monkeypatch)
        c = _build_client()
        r = c.post("/api/workspaces", json={"name": "", "repo": None})
        assert r.status_code == 422  # pydantic min_length


class TestWorkspaceDirect:
    """Direct calls to workspace_manager functions."""

    def test_safe_name_strips_whitespace(self):
        import workspace_manager as wm
        assert wm._safe_name("  hello  ") == "hello"

    def test_safe_name_reserved_rejected(self):
        import workspace_manager as wm
        with pytest.raises(wm.WorkspaceManagerError):
            wm._safe_name(".")
        with pytest.raises(wm.WorkspaceManagerError):
            wm._safe_name("..")

    def test_safe_name_too_long(self):
        import workspace_manager as wm
        with pytest.raises(wm.WorkspaceManagerError):
            wm._safe_name("x" * 200)

    def test_create_describes_dir(self, monkeypatch):
        _set_env(monkeypatch)
        import config
        import asyncio
        import workspace_manager as wm
        settings = config.get_settings()
        async def _run():
            return await wm.create_workspace(settings, "direct1")
        item = asyncio.new_event_loop().run_until_complete(_run())
        assert item["name"] == "direct1"
        assert Path(item["path"]).is_dir()

    def test_list_sorted_by_name(self, monkeypatch):
        _set_env(monkeypatch)
        import config
        import asyncio
        import workspace_manager as wm
        settings = config.get_settings()
        async def _run():
            for n in ["zeta", "alpha", "mid"]:
                await wm.create_workspace(settings, n)
            return await wm.list_workspaces(settings)
        items = asyncio.new_event_loop().run_until_complete(_run())
        names = [w["name"] for w in items]
        assert names == sorted(names)


class TestNoSecretLeak:
    """Ensure workspace responses never leak secrets."""

    @pytest.mark.parametrize("path", [
        "/api/workspaces",
        "/api/workspaces/recent",
    ])
    def test_no_secret_substrings(self, monkeypatch, path):
        _set_env(monkeypatch)
        c = _build_client()
        c.post("/api/workspaces", json={"name": "leaktest", "repo": None})
        r = c.get(path)
        body = r.text.lower()
        for bad in ["api_key", "token", "password", "secret"]:
            assert bad not in body, f"'{bad}' leaked in {path}"


class TestWorkspaceAuthCompat:
    """When AUTH_ENABLED=true, workspace routes require auth."""

    def test_auth_required_when_enabled(self, monkeypatch):
        _set_env(monkeypatch, AUTH_ENABLED="true", AUTH_SECRET="test-secret-123456")
        c = _build_client()
        r = c.get("/api/workspaces")
        assert r.status_code == 401

    def test_auth_guest_can_use_workspaces(self, monkeypatch):
        _set_env(monkeypatch, AUTH_ENABLED="true", AUTH_GUEST_ALLOWED="true",
                 AUTH_SECRET="test-secret-123456")
        c = _build_client()
        # login as guest
        r = c.post("/api/auth/guest", json={"display_name": "Tester"})
        assert r.status_code == 200
        token = r.json()["session"]
        h = {"Authorization": f"Bearer {token}"}
        # list works with token
        r = c.get("/api/workspaces", headers=h)
        assert r.status_code == 200
        # create works
        r = c.post("/api/workspaces", json={"name": "authws", "repo": None}, headers=h)
        assert r.status_code == 200
        assert r.json()["name"] == "authws"
