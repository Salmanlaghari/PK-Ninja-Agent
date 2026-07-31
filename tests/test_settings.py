"""Tests for the v0.7.0 user settings store and /api/settings routes."""
import pytest
from fastapi.testclient import TestClient


def _build_client():
    from config import get_settings
    get_settings.cache_clear()
    import auth as _auth
    _auth.reset_auth_service()
    import importlib
    import main as _main
    importlib.reload(_main)
    return TestClient(_main.app)


class TestSettingsRoutes:
    def test_get_settings_returns_defaults(self, monkeypatch):
        client = _build_client()
        r = client.get("/api/settings")
        assert r.status_code == 200
        body = r.json()
        # Defaults come from config.
        assert body["theme"] == "shinobi"
        assert body["ai_provider"] == "local"
        assert body["auto_save"] is True
        assert body["auto_commit"] is False
        assert body["notifications"] is True
        assert isinstance(body["terminal_preferences"], dict)
        assert isinstance(body["git_preferences"], dict)

    def test_put_settings_partial_update(self, monkeypatch):
        client = _build_client()
        r = client.put("/api/settings", json={"theme": "light", "auto_commit": True})
        assert r.status_code == 200
        body = r.json()
        assert body["theme"] == "light"
        assert body["auto_commit"] is True
        # Untouched fields retain defaults.
        assert body["auto_save"] is True

    def test_settings_persist_across_requests(self, monkeypatch):
        client = _build_client()
        client.put("/api/settings", json={"default_workspace": "my-ws"})
        r = client.get("/api/settings")
        assert r.json()["default_workspace"] == "my-ws"

    def test_put_settings_ignores_unknown_keys(self, monkeypatch):
        client = _build_client()
        r = client.put("/api/settings", json={"theme": "dark", "bogus_field": "x"})
        assert r.status_code == 200
        assert r.json()["theme"] == "dark"
        # The response schema never includes bogus_field.
        assert "bogus_field" not in r.json()

    def test_put_settings_nested_dict(self, monkeypatch):
        client = _build_client()
        r = client.put("/api/settings", json={
            "terminal_preferences": {"shell": "zsh", "font_size": 14, "scrollback": 1000}})
        assert r.status_code == 200
        tp = r.json()["terminal_preferences"]
        assert tp["shell"] == "zsh"
        assert tp["font_size"] == 14

    def test_settings_response_has_no_secrets(self, monkeypatch):
        """The settings response must not leak any secret-guard substrings."""
        client = _build_client()
        r = client.get("/api/settings")
        text = r.text.lower()
        for bad in ["api_key", "token", "password", "secret"]:
            assert bad not in text, f"settings leaked '{bad}'"

    def test_settings_routes_open_when_auth_disabled(self, monkeypatch):
        """Backward compat: settings routes work without a session by default."""
        client = _build_client()
        assert client.get("/api/settings").status_code == 200
        assert client.put("/api/settings", json={"theme": "x"}).status_code == 200


class TestSettingsStoreDirect:
    """Unit-test the store module in isolation (no HTTP layer)."""

    def test_get_defaults_when_no_row(self, tmp_path):
        import asyncio
        from config import get_settings
        from settings_store import get_settings_for_user
        get_settings.cache_clear()
        import os
        os.environ["DATABASE_PATH"] = str(tmp_path / "s.db")
        os.environ["AI_PROVIDER"] = "local"
        get_settings.cache_clear()
        s = get_settings()
        out = asyncio.run(get_settings_for_user(s, None))
        assert out["theme"] == "shinobi"
        assert out["auto_save"] is True

    def test_update_then_get_roundtrip(self, tmp_path):
        import asyncio, os
        from config import get_settings
        from settings_store import get_settings_for_user, update_settings_for_user
        os.environ["DATABASE_PATH"] = str(tmp_path / "s2.db")
        os.environ["AI_PROVIDER"] = "local"
        get_settings.cache_clear()
        s = get_settings()
        out = asyncio.run(update_settings_for_user(s, None, {"theme": "cyber", "notifications": False}))
        assert out["theme"] == "cyber"
        assert out["notifications"] is False
        out2 = asyncio.run(get_settings_for_user(s, None))
        assert out2["theme"] == "cyber"
        assert out2["notifications"] is False
