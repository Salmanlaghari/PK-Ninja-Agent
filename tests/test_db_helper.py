"""Tests for the centralized serverless-safe SQLite helper (backend/db.py)
and the GITHUB_REPOSITORY env-var support in config.py.

These cover the v1.5.0 fixes for the Vercel "unable to open database file"
and "No GitHub repo configured" errors.
"""
import asyncio
import os
import sqlite3
import sys
from pathlib import Path

import aiosqlite
import pytest

# Ensure backend/ is importable.
BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import db as dbmod  # noqa: E402
from config import Settings  # noqa: E402


# ── resolve_db_path ──────────────────────────────────────────────────────

def test_resolve_db_path_explicit_wins(tmp_path):
    explicit = tmp_path / "explicit.db"
    p = dbmod.resolve_db_path(explicit)
    assert p == explicit.resolve()


def test_resolve_db_path_env_override(tmp_path, monkeypatch):
    env_path = tmp_path / "from_env.db"
    monkeypatch.setenv("DB_PATH", str(env_path))
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    p = dbmod.resolve_db_path()
    assert p == env_path.resolve()


def test_resolve_db_path_legacy_alias(tmp_path, monkeypatch):
    env_path = tmp_path / "legacy.db"
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(env_path))
    p = dbmod.resolve_db_path()
    assert p == env_path.resolve()


def test_resolve_db_path_creates_parent(tmp_path, monkeypatch):
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    nested = tmp_path / "nested" / "deep" / "x.db"
    monkeypatch.setenv("DB_PATH", str(nested))
    p = dbmod.resolve_db_path()
    assert p.parent.exists()


# ── connect (async) applies PRAGMAs ──────────────────────────────────────

@pytest.mark.asyncio
async def test_connect_applies_pragmas(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    conn = await dbmod.connect()
    try:
        cur = await conn.execute("PRAGMA journal_mode")
        mode = (await cur.fetchone())[0]
        assert mode in ("wal", "memory")  # memory on tiny ephemeral FS
        cur = await conn.execute("PRAGMA busy_timeout")
        bt = (await cur.fetchone())[0]
        assert bt == 30000
        cur = await conn.execute("PRAGMA temp_store")
        ts = (await cur.fetchone())[0]
        assert ts in (0, 2)  # 2 = MEMORY; 0 = default on some builds
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_connect_row_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    conn = await dbmod.connect()
    try:
        await conn.execute("CREATE TABLE t(a,b)")
        await conn.execute("INSERT INTO t VALUES (1,2)")
        await conn.commit()
        cur = await conn.execute("SELECT a,b FROM t")
        row = await cur.fetchone()
        assert row["b"] == 2  # Row factory gives dict-like access
    finally:
        await conn.close()


# ── connect_sync ─────────────────────────────────────────────────────────

def test_connect_sync_applies_pragmas(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "s.db"))
    conn = dbmod.connect_sync()
    try:
        bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert bt == 30000
    finally:
        conn.close()


# ── with_retry ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_with_retry_succeeds_after_transient():
    state = {"n": 0}

    @dbmod.with_retry(max_retries=3, base_delay=0.001)
    async def flaky():
        state["n"] += 1
        if state["n"] < 2:
            raise aiosqlite.OperationalError("database is locked")
        return "ok"

    assert await flaky() == "ok"
    assert state["n"] == 2


@pytest.mark.asyncio
async def test_with_retry_reraises_non_transient():
    @dbmod.with_retry(max_retries=3, base_delay=0.001)
    async def bad():
        raise ValueError("not a db error")

    with pytest.raises(ValueError):
        await bad()


@pytest.mark.asyncio
async def test_with_retry_exhausted():
    state = {"n": 0}

    @dbmod.with_retry(max_retries=2, base_delay=0.001)
    async def always_locked():
        state["n"] += 1
        raise aiosqlite.OperationalError("unable to open database file")

    with pytest.raises(aiosqlite.OperationalError):
        await always_locked()
    assert state["n"] == 3  # 1 initial + 2 retries


# ── GITHUB_REPOSITORY env parsing ────────────────────────────────────────

def test_github_repo_full_from_github_repository_env(monkeypatch):
    monkeypatch.delenv("GITHUB_OWNER", raising=False)
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "Salmanlaghari/PK-Ninja-Agent")
    s = Settings()
    assert s.github_repo_full() == "Salmanlaghari/PK-Ninja-Agent"


def test_github_repo_full_owner_repo_wins_over_env(monkeypatch):
    monkeypatch.setenv("GITHUB_OWNER", "alice")
    monkeypatch.setenv("GITHUB_REPO", "myrepo")
    monkeypatch.setenv("GITHUB_REPOSITORY", "bob/other")
    s = Settings()
    assert s.github_repo_full() == "alice/myrepo"


def test_github_repo_full_none_when_unset(monkeypatch):
    monkeypatch.delenv("GITHUB_OWNER", raising=False)
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    s = Settings()
    assert s.github_repo_full() is None


def test_github_repo_full_ignores_malformed_env(monkeypatch):
    monkeypatch.delenv("GITHUB_OWNER", raising=False)
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "no-slash-here")
    s = Settings()
    assert s.github_repo_full() is None


# ── /api/health endpoint ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_api_health_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "h.db"))
    monkeypatch.setenv("GITHUB_REPOSITORY", "Salmanlaghari/PK-Ninja-Agent")
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["github_repo"] == "Salmanlaghari/PK-Ninja-Agent"
