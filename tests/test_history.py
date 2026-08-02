"""Tests for the Job History module — v0.8.0 Phase 6.

Covers:
* :mod:`backend.history` unit behaviour (query_history filters, search,
  pagination, get_job_detail, history_stats).
* API routes ``/api/history``, ``/api/history/{task_id}``,
  ``/api/history-stats``.
* Search across description *and* event messages.
* Date-range filtering.
* Status normalization (legacy pending/completed -> idle/success).
* Secret-leak guard.
"""
import asyncio
import datetime as _dt

import aiosqlite
import pytest

import history as H


# ── helpers ────────────────────────────────────────────────────────────────

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


def _db_path():
    from config import get_settings
    return get_settings().db_path


def _seed(rows):
    """rows: list of dicts with keys task_id, description, status, repo,
    created_at, updated_at, events (optional list of (type, message, ts)).

    Ensures the tasks/events schema exists before inserting (the schema is
    normally created by main's _db() on first connection, but unit tests may
    call _seed without building a client first).
    """
    db_path = _db_path()

    async def go():
        conn = await aiosqlite.connect(str(db_path))
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS tasks ("
            "task_id TEXT PRIMARY KEY, description TEXT NOT NULL, "
            "status TEXT NOT NULL, repo TEXT, branch TEXT, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, "
            "type TEXT NOT NULL, message TEXT NOT NULL, data TEXT, "
            "timestamp TEXT NOT NULL)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id)"
        )
        for r in rows:
            await conn.execute(
                "INSERT INTO tasks (task_id, description, status, repo, branch, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (
                    r["task_id"],
                    r["description"],
                    r["status"],
                    r.get("repo"),
                    r.get("branch"),
                    r["created_at"],
                    r["updated_at"],
                ),
            )
            for etype, msg, ts in r.get("events", []):
                await conn.execute(
                    "INSERT INTO events (task_id, type, message, data, timestamp) "
                    "VALUES (?,?,?,?,?)",
                    (r["task_id"], etype, msg, "{}", ts),
                )
        await conn.commit()
        await conn.close()

    asyncio.run(go())


# Unique prefix so we don't collide with other tests sharing the test DB.
_PREFIX = "hist6-"


def _mk(tid, desc, status, repo, created, updated, events=None):
    return {
        "task_id": _PREFIX + tid,
        "description": desc,
        "status": status,
        "repo": repo,
        "created_at": created,
        "updated_at": updated,
        "events": events or [],
    }


# ── Unit tests ─────────────────────────────────────────────────────────────

class TestHistoryUnit:
    def test_query_empty_db(self):
        """Fresh-ish query with no matching rows returns empty items."""
        result = asyncio.run(
            H.query_history(repo="nonexistent-repo-xyz", limit=10)
        )
        assert result["items"] == []
        assert result["count"] == 0
        assert result["limit"] == 10

    def test_query_basic(self):
        _seed([
            _mk("a", "fix bug", "success", "o/r1", "2024-03-01", "2024-03-01"),
            _mk("b", "add tests", "failed", "o/r2", "2024-04-01", "2024-04-01"),
        ])
        result = asyncio.run(H.query_history(repo="o/r1"))
        ids = [it["task_id"] for it in result["items"]]
        assert _PREFIX + "a" in ids
        # o/r2 not included because we filtered to o/r1
        assert _PREFIX + "b" not in ids

    def test_query_search_description(self):
        _seed([
            _mk("search-desc", "Refactor auth module", "success",
                "o/search", "2024-05-01", "2024-05-01"),
        ])
        result = asyncio.run(H.query_history(search="refactor auth"))
        ids = [it["task_id"] for it in result["items"]]
        assert _PREFIX + "search-desc" in ids

    def test_query_search_event_message(self):
        """Search should match event messages, not just description."""
        _seed([
            _mk("search-ev", "generic task", "success", "o/se",
                "2024-05-02", "2024-05-02",
                events=[("status", "Discovered a memory leak in parser", "2024-05-02")]),
        ])
        # Search for text only in the event, not the description
        result = asyncio.run(H.query_history(search="memory leak"))
        ids = [it["task_id"] for it in result["items"]]
        assert _PREFIX + "search-ev" in ids

    def test_query_status_filter(self):
        _seed([
            _mk("st-succ", "ok", "success", "o/sf", "2024-06-01", "2024-06-01"),
            _mk("st-fail", "bad", "failed", "o/sf", "2024-06-02", "2024-06-02"),
        ])
        result = asyncio.run(H.query_history(status="failed"))
        ids = [it["task_id"] for it in result["items"]]
        assert _PREFIX + "st-fail" in ids
        assert _PREFIX + "st-succ" not in ids

    def test_query_status_normalizes_legacy(self):
        """Asking for 'idle' should also return legacy 'pending' rows."""
        _seed([
            _mk("leg-pend", "old pending", "pending", "o/leg",
                "2024-01-01", "2024-01-01"),
        ])
        result = asyncio.run(H.query_history(status="idle", repo="o/leg"))
        ids = [it["task_id"] for it in result["items"]]
        assert _PREFIX + "leg-pend" in ids
        # and the returned status is normalized
        for it in result["items"]:
            if it["task_id"] == _PREFIX + "leg-pend":
                assert it["status"] == "idle"

    def test_query_status_normalizes_completed_to_success(self):
        _seed([
            _mk("leg-comp", "old completed", "completed", "o/leg2",
                "2024-01-01", "2024-01-01"),
        ])
        result = asyncio.run(H.query_history(status="success", repo="o/leg2"))
        ids = [it["task_id"] for it in result["items"]]
        assert _PREFIX + "leg-comp" in ids

    def test_query_date_range(self):
        _seed([
            _mk("d1", "before", "success", "o/dr", "2024-01-15", "2024-01-15"),
            _mk("d2", "during", "success", "o/dr", "2024-02-15", "2024-02-15"),
            _mk("d3", "after", "success", "o/dr", "2024-03-15", "2024-03-15"),
        ])
        result = asyncio.run(
            H.query_history(repo="o/dr", date_from="2024-02-01", date_to="2024-02-28")
        )
        ids = [it["task_id"] for it in result["items"]]
        assert _PREFIX + "d2" in ids
        assert _PREFIX + "d1" not in ids
        assert _PREFIX + "d3" not in ids

    def test_query_pagination(self):
        rows = [
            _mk(f"p{i}", f"task {i}", "success", "o/page",
                f"2024-01-{i+1:02d}", f"2024-01-{i+1:02d}")
            for i in range(10)
        ]
        _seed(rows)
        page1 = asyncio.run(H.query_history(repo="o/page", limit=4, offset=0))
        page2 = asyncio.run(H.query_history(repo="o/page", limit=4, offset=4))
        assert len(page1["items"]) == 4
        assert len(page2["items"]) == 4
        # no overlap
        ids1 = {it["task_id"] for it in page1["items"]}
        ids2 = {it["task_id"] for it in page2["items"]}
        assert ids1.isdisjoint(ids2)
        # ordered by created_at DESC, so page1 is newer
        assert page1["items"][0]["created_at"] >= page2["items"][0]["created_at"]

    def test_query_include_events(self):
        _seed([
            _mk("iev", "with events", "success", "o/iev", "2024-07-01", "2024-07-01",
                events=[
                    ("status", "started", "2024-07-01T00:00:00"),
                    ("status", "working", "2024-07-01T00:01:00"),
                    ("status", "done", "2024-07-01T00:02:00"),
                ]),
        ])
        result = asyncio.run(
            H.query_history(repo="o/iev", include_events=2)
        )
        item = result["items"][0]
        assert item["event_count"] == 3
        assert len(item["events"]) == 2  # latest 2
        # latest N => last two messages
        msgs = [e["message"] for e in item["events"]]
        assert "done" in msgs
        assert "started" not in msgs

    def test_query_filters_echoed(self):
        result = asyncio.run(
            H.query_history(repo="o/echo", status="success", search="x")
        )
        f = result["filters"]
        assert f["repo"] == "o/echo"
        assert f["status"] == "success"
        assert f["search"] == "x"

    def test_get_job_detail(self):
        _seed([
            _mk("detail", "detail task", "success", "o/det", "2024-08-01", "2024-08-01",
                events=[("status", "hi", "2024-08-01T00:00:00")]),
        ])
        detail = asyncio.run(H.get_job_detail(_PREFIX + "detail"))
        assert detail is not None
        assert detail["task_id"] == _PREFIX + "detail"
        assert detail["event_count"] == 1
        assert detail["events"][0]["message"] == "hi"

    def test_get_job_detail_not_found(self):
        assert asyncio.run(H.get_job_detail("does-not-exist-xyz")) is None

    def test_history_stats(self):
        _seed([
            _mk("stat-a", "a", "success", "o/stat1", "2024-09-01", "2024-09-01"),
            _mk("stat-b", "b", "failed", "o/stat1", "2024-09-02", "2024-09-02"),
            _mk("stat-c", "c", "success", "o/stat2", "2024-09-03", "2024-09-03",
                events=[("status", "e1", "2024-09-03")]),
        ])
        stats = asyncio.run(H.history_stats())
        assert stats["total_tasks"] >= 3
        assert "success" in stats["by_status"]
        assert "failed" in stats["by_status"]
        assert stats["by_status"]["success"] >= 2
        assert stats["by_status"]["failed"] >= 1
        assert "o/stat1" in stats["by_repo"]
        assert stats["total_events"] >= 1

    def test_limit_clamped(self):
        result = asyncio.run(H.query_history(limit=99999))
        assert result["limit"] == 500  # clamped to max

    def test_offset_clamped(self):
        result = asyncio.run(H.query_history(offset=-5))
        assert result["offset"] == 0


# ── API tests ──────────────────────────────────────────────────────────────

class TestHistoryAPI:
    def test_history_list_endpoint(self, monkeypatch):
        c = _build_client(monkeypatch)
        _seed([
            _mk("api-1", "api task one", "success", "o/api", "2024-10-01", "2024-10-01"),
        ])
        r = c.get("/api/history", params={"repo": "o/api"})
        assert r.status_code == 200
        body = r.json()
        assert "items" in body
        assert "count" in body
        assert "filters" in body
        ids = [it["task_id"] for it in body["items"]]
        assert _PREFIX + "api-1" in ids

    def test_history_search_endpoint(self, monkeypatch):
        c = _build_client(monkeypatch)
        _seed([
            _mk("api-search", "build the login page", "success", "o/apis",
                "2024-10-02", "2024-10-02"),
        ])
        r = c.get("/api/history", params={"search": "login page"})
        assert r.status_code == 200
        ids = [it["task_id"] for it in r.json()["items"]]
        assert _PREFIX + "api-search" in ids

    def test_history_detail_endpoint(self, monkeypatch):
        c = _build_client(monkeypatch)
        _seed([
            _mk("api-det", "detail via api", "success", "o/apid", "2024-10-03", "2024-10-03",
                events=[("status", "hello world", "2024-10-03")]),
        ])
        r = c.get(f"/api/history/{_PREFIX}api-det")
        assert r.status_code == 200
        body = r.json()
        assert body["task_id"] == _PREFIX + "api-det"
        assert body["events"][0]["message"] == "hello world"

    def test_history_detail_404(self, monkeypatch):
        c = _build_client(monkeypatch)
        r = c.get("/api/history/no-such-task-xyz")
        assert r.status_code == 404

    def test_history_stats_endpoint(self, monkeypatch):
        c = _build_client(monkeypatch)
        r = c.get("/api/history-stats")
        assert r.status_code == 200
        body = r.json()
        assert "total_tasks" in body
        assert "by_status" in body
        assert "by_repo" in body

    def test_history_status_filter_endpoint(self, monkeypatch):
        c = _build_client(monkeypatch)
        _seed([
            _mk("api-st-ok", "ok", "success", "o/apist", "2024-10-04", "2024-10-04"),
            _mk("api-st-bad", "bad", "failed", "o/apist", "2024-10-05", "2024-10-05"),
        ])
        r = c.get("/api/history", params={"repo": "o/apist", "status": "failed"})
        ids = [it["task_id"] for it in r.json()["items"]]
        assert _PREFIX + "api-st-bad" in ids
        assert _PREFIX + "api-st-ok" not in ids

    def test_history_date_filter_endpoint(self, monkeypatch):
        c = _build_client(monkeypatch)
        _seed([
            _mk("api-date-in", "in range", "success", "o/apidate", "2024-11-15", "2024-11-15"),
            _mk("api-date-out", "out of range", "success", "o/apidate", "2024-12-25", "2024-12-25"),
        ])
        r = c.get("/api/history", params={
            "repo": "o/apidate", "date_from": "2024-11-01", "date_to": "2024-11-30"
        })
        ids = [it["task_id"] for it in r.json()["items"]]
        assert _PREFIX + "api-date-in" in ids
        assert _PREFIX + "api-date-out" not in ids

    def test_history_pagination_endpoint(self, monkeypatch):
        c = _build_client(monkeypatch)
        rows = [
            _mk(f"api-page-{i}", f"page task {i}", "success", "o/apipage",
                f"2024-01-{i+1:02d}", f"2024-01-{i+1:02d}")
            for i in range(6)
        ]
        _seed(rows)
        r = c.get("/api/history", params={
            "repo": "o/apipage", "limit": 3, "offset": 0
        })
        body = r.json()
        assert len(body["items"]) == 3
        assert body["count"] >= 6

    def test_history_include_events_endpoint(self, monkeypatch):
        c = _build_client(monkeypatch)
        _seed([
            _mk("api-iev", "with ev", "success", "o/apiiev", "2024-12-01", "2024-12-01",
                events=[
                    ("status", "e1", "2024-12-01T00:00:00"),
                    ("status", "e2", "2024-12-01T00:01:00"),
                ]),
        ])
        r = c.get("/api/history", params={
            "repo": "o/apiiev", "include_events": 5
        })
        item = r.json()["items"][0]
        assert item["event_count"] == 2
        assert len(item["events"]) == 2

    def test_history_no_secret_leak(self, monkeypatch):
        c = _build_client(monkeypatch)
        r = c.get("/api/history")
        low = r.text.lower()
        for secret in ("api_key", "token", "password", "secret"):
            assert secret not in low

    def test_history_detail_no_secret_leak(self, monkeypatch):
        c = _build_client(monkeypatch)
        r = c.get(f"/api/history/{_PREFIX}api-det")
        low = r.text.lower()
        for secret in ("api_key", "token", "password", "secret"):
            assert secret not in low

    def test_history_stats_no_secret_leak(self, monkeypatch):
        c = _build_client(monkeypatch)
        r = c.get("/api/history-stats")
        low = r.text.lower()
        for secret in ("api_key", "token", "password", "secret"):
            assert secret not in low
