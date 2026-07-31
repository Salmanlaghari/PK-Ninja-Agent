"""Tests for the v0.7.0 modular authentication system.

Covers:
* Auth disabled (default) → anonymous user, all routes open (backward compat).
* Auth enabled → guest login, session token round-trip, protected routes.
* GitHub login path (mocked httpx call).
* Token tampering / expiry / malformed → 401.
* Secret-leak guard: no secrets appear in auth responses or /api/config.
"""
import base64
import datetime as _dt
import json
import os
import time

import pytest
from fastapi.testclient import TestClient


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _set_auth_env(monkeypatch, **kwargs):
    """Flip auth-related env vars and reset the cached settings + auth service."""
    defaults = {
        "AUTH_ENABLED": "true",
        "AUTH_GUEST_ALLOWED": "true",
        "AUTH_GITHUB_ENABLED": "true",
        "AUTH_SECRET": "test-secret-do-not-use-in-prod",
        "AUTH_GUEST_TTL_SECONDS": "3600",
        "AUTH_USER_TTL_SECONDS": "86400",
        "GITHUB_TOKEN": "",  # not required for the beta token-verify flow
        "GITHUB_OWNER": "",
        "GITHUB_REPO": "",
    }
    defaults.update(kwargs)
    for k, v in defaults.items():
        monkeypatch.setenv(k, v)
    # Clear caches so new env is picked up.
    from config import get_settings
    get_settings.cache_clear()
    import auth as _auth
    _auth.reset_auth_service()


def _clear_auth_env(monkeypatch):
    """Return to the default (auth-disabled) state."""
    for k in ["AUTH_ENABLED", "AUTH_GITHUB_ENABLED", "AUTH_SECRET"]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AUTH_GUEST_ALLOWED", "true")
    from config import get_settings
    get_settings.cache_clear()
    import auth as _auth
    _auth.reset_auth_service()


def _build_client():
    """Build a fresh TestClient from a freshly-reloaded app (picks up env)."""
    from config import get_settings
    get_settings.cache_clear()
    import auth as _auth
    _auth.reset_auth_service()
    import importlib
    import main as _main
    importlib.reload(_main)
    return TestClient(_main.app)


@pytest.fixture
def client():
    """A fresh TestClient (rebuilt per test). Tests set env *before* calling
    ``_build_client()`` directly when they need a specific auth state; this
    fixture provides the default (auth-disabled) client for convenience."""
    return _build_client()


# --------------------------------------------------------------------------- #
# Auth disabled (default / backward compat)
# --------------------------------------------------------------------------- #

class TestAuthDisabled:
    def test_status_reports_disabled(self, monkeypatch):
        _clear_auth_env(monkeypatch)
        client = _build_client()
        r = client.get("/api/auth/status")
        assert r.status_code == 200
        body = r.json()
        assert body["auth_enabled"] is False
        assert body["authenticated"] is False
        # anonymous user is not surfaced when auth is off
        assert body.get("user") is None

    def test_me_returns_anonymous(self, monkeypatch):
        _clear_auth_env(monkeypatch)
        client = _build_client()
        r = client.get("/api/me")
        assert r.status_code == 200
        body = r.json()
        assert body["user_id"] == "anonymous"
        assert body["is_guest"] is True

    def test_guest_login_noop_when_disabled(self, monkeypatch):
        _clear_auth_env(monkeypatch)
        client = _build_client()
        r = client.post("/api/auth/guest", json={"display_name": "Someone"})
        assert r.status_code == 200
        body = r.json()
        assert body["session"] == ""
        assert body["user"]["user_id"] == "anonymous"
        assert body["expires_in"] == 0

    def test_github_login_rejected_when_disabled(self, monkeypatch):
        _clear_auth_env(monkeypatch)
        client = _build_client()
        r = client.post("/api/auth/github", json={"github_token": "ghp_xxx"})
        assert r.status_code == 400

    def test_existing_routes_still_open(self, monkeypatch):
        """No auth header needed for pre-existing endpoints (backward compat)."""
        _clear_auth_env(monkeypatch)
        client = _build_client()
        r = client.get("/health")
        assert r.status_code == 200
        r = client.get("/api/config")
        assert r.status_code == 200

    def test_logout_ok_when_disabled(self, monkeypatch):
        _clear_auth_env(monkeypatch)
        client = _build_client()
        r = client.post("/api/auth/logout")
        assert r.status_code == 200
        assert r.json()["success"] is True

    def test_status_is_public_when_enabled(self, monkeypatch):
        """auth/status must be reachable WITHOUT a token so the frontend can
        decide whether to show the login screen (chicken-and-egg)."""
        _set_auth_env(monkeypatch)
        client = _build_client()
        r = client.get("/api/auth/status")
        assert r.status_code == 200
        body = r.json()
        assert body["auth_enabled"] is True
        assert body["user"] is None  # no session → no identity


# --------------------------------------------------------------------------- #
# Auth enabled — guest flow
# --------------------------------------------------------------------------- #

class TestGuestFlow:
    def test_guest_login_returns_token(self, monkeypatch):
        _set_auth_env(monkeypatch)
        client = _build_client()
        r = client.post("/api/auth/guest", json={"display_name": "Ninja"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["session"], "expected a session token"
        assert body["user"]["is_guest"] is True
        assert body["user"]["display_name"] == "Ninja"
        assert body["expires_in"] > 0

    def test_guest_token_authenticates_me(self, monkeypatch):
        _set_auth_env(monkeypatch)
        client = _build_client()
        tok = client.post("/api/auth/guest", json={}).json()["session"]
        r = client.get("/api/me", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        assert r.json()["is_guest"] is True
        assert r.json()["user_id"].startswith("guest_")

    def test_me_rejects_missing_token_when_enabled(self, monkeypatch):
        _set_auth_env(monkeypatch)
        client = _build_client()
        r = client.get("/api/me")
        assert r.status_code == 401

    def test_me_rejects_tampered_token(self, monkeypatch):
        _set_auth_env(monkeypatch)
        client = _build_client()
        tok = client.post("/api/auth/guest", json={}).json()["session"]
        # Flip the last char of the signature.
        bad = tok[:-1] + ("a" if tok[-1] != "a" else "b")
        r = client.get("/api/me", headers={"Authorization": f"Bearer {bad}"})
        assert r.status_code == 401

    def test_me_rejects_malformed_token(self, monkeypatch):
        _set_auth_env(monkeypatch)
        client = _build_client()
        r = client.get("/api/me", headers={"Authorization": "Bearer not-a-token"})
        assert r.status_code == 401

    def test_logout_ok_when_enabled(self, monkeypatch):
        _set_auth_env(monkeypatch)
        client = _build_client()
        tok = client.post("/api/auth/guest", json={}).json()["session"]
        r = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        assert r.json()["success"] is True

    def test_session_query_param_works(self, monkeypatch):
        _set_auth_env(monkeypatch)
        client = _build_client()
        tok = client.post("/api/auth/guest", json={}).json()["session"]
        r = client.get(f"/api/me?session={tok}")
        assert r.status_code == 200
        assert r.json()["user_id"].startswith("guest_")


# --------------------------------------------------------------------------- #
# Token internals (HMAC signed, expiry)
# --------------------------------------------------------------------------- #

class TestTokenInternals:
    def test_payload_is_base64_json(self, monkeypatch):
        _set_auth_env(monkeypatch)
        client = _build_client()
        tok = client.post("/api/auth/guest", json={"display_name": "X"}).json()["session"]
        payload_b64, _, sig = tok.rpartition(".")
        payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode())
        assert payload["is_guest"] is True
        assert payload["display_name"] == "X"
        assert "exp" in payload and "iat" in payload
        assert len(sig) == 64  # sha256 hex

    def test_expired_token_rejected(self, monkeypatch):
        _set_auth_env(monkeypatch, AUTH_GUEST_TTL_SECONDS="1")
        client = _build_client()
        tok = client.post("/api/auth/guest", json={}).json()["session"]
        time.sleep(2)
        r = client.get("/api/me", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 401

    def test_secret_rotation_invalidates_old_tokens(self, monkeypatch):
        _set_auth_env(monkeypatch, AUTH_SECRET="secret-one")
        client = _build_client()
        tok = client.post("/api/auth/guest", json={}).json()["session"]
        # Rotate the secret + rebuild client.
        _set_auth_env(monkeypatch, AUTH_SECRET="secret-two")
        client = _build_client()
        r = client.get("/api/me", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 401


# --------------------------------------------------------------------------- #
# GitHub login (mocked)
# --------------------------------------------------------------------------- #

class TestGitHubLogin:
    def test_github_login_disabled(self, monkeypatch):
        _set_auth_env(monkeypatch, AUTH_GITHUB_ENABLED="false")
        client = _build_client()
        r = client.post("/api/auth/github", json={"github_token": "ghp_xxx"})
        assert r.status_code == 401

    def test_github_login_empty_token(self, monkeypatch):
        _set_auth_env(monkeypatch)
        client = _build_client()
        # A whitespace-only token passes Pydantic min_length but fails auth.
        r = client.post("/api/auth/github", json={"github_token": "   "})
        assert r.status_code == 401
        # A truly empty string is rejected by request validation (422).
        r2 = client.post("/api/auth/github", json={"github_token": ""})
        assert r2.status_code == 422

    def test_github_login_success_mocked(self, monkeypatch):
        _set_auth_env(monkeypatch)
        client = _build_client()

        # Monkeypatch the httpx.Client.get inside auth to return a fake user.
        import auth as _auth

        class _FakeResp:
            status_code = 200
            def json(self):
                return {
                    "id": 12345,
                    "login": "octocat",
                    "name": "The Octocat",
                    "avatar_url": "https://avatars.githubusercontent.com/u/583231?v=4",
                }

        class _FakeClient:
            def __init__(self, *a, **k):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def get(self, url, headers=None):
                return _FakeResp()

        monkeypatch.setattr(_auth.httpx, "Client", _FakeClient)

        r = client.post("/api/auth/github", json={"github_token": "ghp_valid"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["session"]
        assert body["user"]["github_login"] == "octocat"
        assert body["user"]["is_guest"] is False
        assert body["user"]["display_name"] == "The Octocat"

        # The token should authenticate /api/me.
        r2 = client.get("/api/me", headers={"Authorization": f"Bearer {body['session']}"})
        assert r2.status_code == 200
        assert r2.json()["github_login"] == "octocat"

    def test_github_login_invalid_token_mocked(self, monkeypatch):
        _set_auth_env(monkeypatch)
        client = _build_client()
        import auth as _auth

        class _FakeResp:
            status_code = 401
            def json(self):
                return {}

        class _FakeClient:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url, headers=None): return _FakeResp()

        monkeypatch.setattr(_auth.httpx, "Client", _FakeClient)
        r = client.post("/api/auth/github", json={"github_token": "ghp_bad"})
        assert r.status_code == 401


# --------------------------------------------------------------------------- #
# Secret-leak guard (auth must not leak secrets into responses)
# --------------------------------------------------------------------------- #

class TestNoSecretLeak:
    def test_config_has_no_auth_secret(self, monkeypatch):
        _set_auth_env(monkeypatch, AUTH_SECRET="super-secret-value-xyz")
        client = _build_client()
        r = client.get("/api/config")
        text = r.text.lower()
        assert "super-secret-value-xyz" not in text
        # The guard substrings from the existing suite.
        for bad in ["api_key", "token", "password", "secret"]:
            assert bad not in text, f"config leaked '{bad}'"

    def test_auth_responses_have_no_token_field(self, monkeypatch):
        _set_auth_env(monkeypatch)
        client = _build_client()
        # 'github_token' is only in the request body, never echoed in responses.
        body = client.post("/api/auth/guest", json={"display_name": "Z"}).json()
        dumped = json.dumps(body).lower()
        # The session field is the only credential-like value; it must not
        # contain the guard substrings that the existing leak test checks.
        # (The session token is base64url + hex, which never contains "token".)
        assert "github_token" not in dumped
        assert "password" not in dumped
        assert "secret" not in dumped
