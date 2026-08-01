"""Tests for Workspace Sessions — v0.8.0 Phase 3.

Covers:
* CRUD behaviour of :mod:`backend.sessions` (create, list, get, touch, close,
  delete, find_active_for_repo, reuse-on-create).
* API routes under ``/api/sessions`` (list, create, get, restore, close,
  delete, repo filter).
* Schema is created idempotently on every DB connection.
"""
from pathlib import Path

import pytest

import sessions as S


# ── Unit tests ─────────────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test_sessions.db"


class TestSessionsUnit:
    def test_create_and_get(self, db_path):
        s = S.create_session.__wrapped__ if hasattr(S.create_session, "__wrapped__") else None
        import asyncio
        sess = asyncio.run(S.create_session(
            db_path, repo_full="o/r", workspace="/tmp/ws",
            branch="main", task_id="t1", description="d",
        ))
        assert sess["session_id"]
        assert sess["repo_full"] == "o/r"
        assert sess["state"] == "active"
        got = asyncio.run(S.get_session(db_path, sess["session_id"]))
        assert got["session_id"] == sess["session_id"]

    def test_list_and_filter(self, db_path):
        import asyncio
        asyncio.run(S.create_session(db_path, repo_full="o/r", workspace="/a"))
        asyncio.run(S.create_session(db_path, repo_full="o/r2", workspace="/b"))
        all_s = asyncio.run(S.list_sessions(db_path))
        assert len(all_s) == 2
        only_r = asyncio.run(S.list_sessions(db_path, repo_full="o/r"))
        assert len(only_r) == 1 and only_r[0]["repo_full"] == "o/r"

    def test_find_active_for_repo(self, db_path):
        import asyncio
        asyncio.run(S.create_session(db_path, repo_full="o/r", workspace="/a"))
        found = asyncio.run(S.find_active_for_repo(db_path, "o/r"))
        assert found is not None and found["state"] == "active"
        assert asyncio.run(S.find_active_for_repo(db_path, "o/none")) is None

    def test_touch_updates_fields(self, db_path):
        import asyncio
        sess = asyncio.run(S.create_session(db_path, repo_full="o/r", workspace="/a"))
        updated = asyncio.run(S.touch_session(
            db_path, sess["session_id"], branch="dev", task_id="t2",
            description="new", state="interrupted",
        ))
        assert updated["branch"] == "dev"
        assert updated["task_id"] == "t2"
        assert updated["description"] == "new"
        assert updated["state"] == "interrupted"

    def test_close_session(self, db_path):
        import asyncio
        sess = asyncio.run(S.create_session(db_path, repo_full="o/r", workspace="/a"))
        closed = asyncio.run(S.close_session(db_path, sess["session_id"]))
        assert closed["state"] == "closed"
        # closed session should not be found as active
        assert asyncio.run(S.find_active_for_repo(db_path, "o/r")) is None

    def test_delete_session(self, db_path):
        import asyncio
        sess = asyncio.run(S.create_session(db_path, repo_full="o/r", workspace="/a"))
        assert asyncio.run(S.delete_session(db_path, sess["session_id"])) is True
        assert asyncio.run(S.get_session(db_path, sess["session_id"])) is None
        assert asyncio.run(S.delete_session(db_path, sess["session_id"])) is False

    def test_schema_idempotent(self, db_path):
        """ensure_sessions_schema can be called multiple times safely."""
        import asyncio
        import aiosqlite

        async def go():
            conn = await aiosqlite.connect(str(db_path))
            await S.ensure_sessions_schema(conn)
            await S.ensure_sessions_schema(conn)  # no error
            await conn.close()
        asyncio.run(go())
        # can still create a session afterwards
        asyncio.run(S.create_session(db_path, repo_full="o/r", workspace="/a"))


# ── API tests ──────────────────────────────────────────────────────────────

def _clear_settings_cache():
    from config import get_settings
    get_settings.cache_clear()


def _build_client(monkeypatch):
    import importlib
    _clear_settings_cache()
    import main as _main
    importlib.reload(_main)
    from fastapi.testclient import TestClient
    return TestClient(_main.app)


class TestSessionsAPI:
    def test_create_and_list(self, monkeypatch):
        c = _build_client(monkeypatch)
        r = c.post("/api/sessions", json={
            "repo_full": "o/r", "workspace": "/tmp/ws",
            "branch": "main", "task_id": "t1", "description": "d",
        })
        assert r.status_code == 200
        sess = r.json()
        assert sess["repo_full"] == "o/r"
        assert sess["state"] == "active"
        sid = sess["session_id"]
        # list
        lst = c.get("/api/sessions").json()
        assert lst["count"] >= 1
        assert any(s["session_id"] == sid for s in lst["sessions"])
        # get
        g = c.get(f"/api/sessions/{sid}").json()
        assert g["session_id"] == sid

    def test_create_reuses_active_for_same_repo(self, monkeypatch):
        c = _build_client(monkeypatch)
        r1 = c.post("/api/sessions", json={"repo_full": "o/r", "workspace": "/a"}).json()
        r2 = c.post("/api/sessions", json={"repo_full": "o/r", "workspace": "/b"}).json()
        # should reuse the same session (same id)
        assert r1["session_id"] == r2["session_id"]
        # only one session for o/r
        lst = c.get("/api/sessions?repo_full=o/r").json()
        assert lst["count"] == 1

    def test_restore_and_close(self, monkeypatch):
        c = _build_client(monkeypatch)
        sid = c.post("/api/sessions", json={"repo_full": "o/r", "workspace": "/a"}).json()["session_id"]
        # close
        closed = c.post(f"/api/sessions/{sid}/close").json()
        assert closed["state"] == "closed"
        # restore
        restored = c.post(f"/api/sessions/{sid}/restore").json()
        assert restored["state"] == "active"

    def test_delete(self, monkeypatch):
        c = _build_client(monkeypatch)
        sid = c.post("/api/sessions", json={"repo_full": "o/r", "workspace": "/a"}).json()["session_id"]
        d = c.delete(f"/api/sessions/{sid}").json()
        assert d["deleted"] is True
        assert c.get(f"/api/sessions/{sid}").status_code == 404
        # second delete -> 404
        assert c.delete(f"/api/sessions/{sid}").status_code == 404

    def test_get_unknown_404(self, monkeypatch):
        c = _build_client(monkeypatch)
        assert c.get("/api/sessions/nope").status_code == 404

    def test_filter_by_state(self, monkeypatch):
        c = _build_client(monkeypatch)
        s1 = c.post("/api/sessions", json={"repo_full": "o/r1", "workspace": "/a"}).json()
        s2 = c.post("/api/sessions", json={"repo_full": "o/r2", "workspace": "/b"}).json()
        c.post(f"/api/sessions/{s2['session_id']}/close")
        active = c.get("/api/sessions?state=active").json()
        closed = c.get("/api/sessions?state=closed").json()
        assert all(s["state"] == "active" for s in active["sessions"])
        assert all(s["state"] == "closed" for s in closed["sessions"])
        assert any(s["session_id"] == s1["session_id"] for s in active["sessions"])
        assert any(s["session_id"] == s2["session_id"] for s in closed["sessions"])

    def test_no_secret_leak(self, monkeypatch):
        c = _build_client(monkeypatch)
        c.post("/api/sessions", json={"repo_full": "o/r", "workspace": "/a"})
        for ep in ["/api/sessions", "/api/sessions/list"]:
            r = c.get(ep)
            if r.status_code == 200:
                low = r.text.lower()
                for secret in ("api_key", "token", "password", "secret"):
                    assert secret not in low, f"{secret} leaked in {ep}"
