"""Tests for the Export module — v0.8.0 Phase 7.

Covers:
* :mod:`backend.exporter` pure functions (logs JSON/text, report markdown,
  history JSON/CSV).
* API routes ``/api/export/{task_id}`` (json/text/markdown) and
  ``/api/export-history`` (json/csv).
* CSV format correctness (header row, RFC-4180 quoting).
* Markdown report structure (tables, timeline).
* Secret-leak guard.
"""
import asyncio
import csv
import io
import json

import aiosqlite
import pytest

import exporter as E


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


_PREFIX = "exp7-"


def _seed(rows):
    """rows: list of dicts with keys task_id, description, status, repo,
    created_at, updated_at, events (optional list of (type, message, ts))."""
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
                    _PREFIX + r["task_id"],
                    r["description"], r["status"], r.get("repo"),
                    r.get("branch"), r["created_at"], r["updated_at"],
                ),
            )
            for etype, msg, ts in r.get("events", []):
                await conn.execute(
                    "INSERT INTO events (task_id, type, message, data, timestamp) "
                    "VALUES (?,?,?,?,?)",
                    (_PREFIX + r["task_id"], etype, msg, "{}", ts),
                )
        await conn.commit()
        await conn.close()

    asyncio.run(go())


def _sample_task():
    return {
        "task_id": _PREFIX + "unit-task",
        "description": "Fix the login bug",
        "status": "success",
        "repo": "o/r",
        "branch": "feat/x",
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:05:00",
    }


def _sample_events():
    return [
        {"type": "status", "message": "Started", "data": {},
         "timestamp": "2024-01-01T00:00:01"},
        {"type": "analysis", "message": "Found null pointer", "data": {},
         "timestamp": "2024-01-01T00:01:00"},
        {"type": "status", "message": "Done", "data": {},
         "timestamp": "2024-01-01T00:05:00"},
    ]


# ── Unit tests ─────────────────────────────────────────────────────────────

class TestExporterUnit:
    def test_logs_json_valid(self):
        out = E.export_logs_json(_sample_task(), _sample_events())
        data = json.loads(out)
        assert data["task"]["task_id"] == _PREFIX + "unit-task"
        assert data["event_count"] == 3
        assert len(data["events"]) == 3
        assert data["events"][0]["message"] == "Started"

    def test_logs_json_includes_task_fields(self):
        out = E.export_logs_json(_sample_task(), _sample_events())
        data = json.loads(out)
        assert data["task"]["status"] == "success"
        assert data["task"]["repo"] == "o/r"
        assert data["task"]["branch"] == "feat/x"

    def test_logs_text_format(self):
        out = E.export_logs_text(_sample_task(), _sample_events())
        assert _PREFIX + "unit-task" in out
        assert "Fix the login bug" in out
        assert "Started" in out
        assert "Found null pointer" in out
        assert "Done" in out
        # line-oriented
        assert out.count("\n") >= 6

    def test_logs_text_no_events(self):
        out = E.export_logs_text(_sample_task(), [])
        assert "Events (0):" in out

    def test_report_markdown_structure(self):
        out = E.export_report_markdown(_sample_task(), _sample_events())
        assert out.startswith("# Task Report:")
        assert _PREFIX + "unit-task" in out
        assert "Fix the login bug" in out
        assert "| Field | Value |" in out
        assert "## Event Breakdown" in out
        assert "## Event Timeline" in out
        assert "status" in out  # event type appears
        assert "analysis" in out

    def test_report_markdown_no_events(self):
        out = E.export_report_markdown(_sample_task(), [])
        assert "## Event Timeline" in out
        assert "No events recorded" in out

    def test_report_markdown_has_version_footer(self):
        out = E.export_report_markdown(_sample_task(), _sample_events())
        assert "v0.8.0" in out

    def test_history_json(self):
        items = [
            {"task_id": "t1", "description": "a", "status": "success",
             "repo": "o/r", "branch": None, "created_at": "2024-01-01",
             "updated_at": "2024-01-01"},
            {"task_id": "t2", "description": "b", "status": "failed",
             "repo": "o/r", "branch": "dev", "created_at": "2024-02-01",
             "updated_at": "2024-02-01"},
        ]
        out = E.export_history_json(items, count=42)
        data = json.loads(out)
        assert data["count"] == 42
        assert data["exported"] == 2
        assert len(data["tasks"]) == 2
        assert data["tasks"][0]["task_id"] == "t1"

    def test_history_csv(self):
        items = [
            {"task_id": "t1", "description": "hello, world", "status": "success",
             "repo": "o/r", "branch": None, "created_at": "2024-01-01",
             "updated_at": "2024-01-01"},
        ]
        out = E.export_history_csv(items)
        reader = csv.reader(io.StringIO(out))
        rows = list(reader)
        assert rows[0] == ["task_id", "description", "status", "repo",
                           "branch", "created_at", "updated_at"]
        assert rows[1][0] == "t1"
        # comma in description should be quoted by csv module
        assert '"hello, world"' in out

    def test_history_csv_empty(self):
        out = E.export_history_csv([])
        reader = csv.reader(io.StringIO(out))
        rows = list(reader)
        assert len(rows) == 1  # only header
        assert rows[0][0] == "task_id"

    def test_logs_json_empty_events(self):
        out = E.export_logs_json(_sample_task(), [])
        data = json.loads(out)
        assert data["event_count"] == 0
        assert data["events"] == []


# ── API tests ──────────────────────────────────────────────────────────────

class TestExportAPI:
    def test_export_task_json(self, monkeypatch):
        c = _build_client(monkeypatch)
        _seed([{
            "task_id": "json", "description": "json export", "status": "success",
            "repo": "o/ej", "created_at": "2024-03-01", "updated_at": "2024-03-01",
            "events": [("status", "hi", "2024-03-01")],
        }])
        r = c.get(f"/api/export/{_PREFIX}json", params={"format": "json"})
        assert r.status_code == 200
        data = r.json()
        assert data["task"]["task_id"] == _PREFIX + "json"
        assert data["event_count"] == 1

    def test_export_task_text(self, monkeypatch):
        c = _build_client(monkeypatch)
        _seed([{
            "task_id": "text", "description": "text export", "status": "success",
            "repo": "o/et", "created_at": "2024-03-02", "updated_at": "2024-03-02",
            "events": [("status", "hello text", "2024-03-02")],
        }])
        r = c.get(f"/api/export/{_PREFIX}text", params={"format": "text"})
        assert r.status_code == 200
        assert "text/plain" in r.headers.get("content-type", "")
        assert "hello text" in r.text

    def test_export_task_markdown(self, monkeypatch):
        c = _build_client(monkeypatch)
        _seed([{
            "task_id": "md", "description": "md export", "status": "success",
            "repo": "o/em", "created_at": "2024-03-03", "updated_at": "2024-03-03",
            "events": [("analysis", "deep thought", "2024-03-03")],
        }])
        r = c.get(f"/api/export/{_PREFIX}md", params={"format": "markdown"})
        assert r.status_code == 200
        assert "text/markdown" in r.headers.get("content-type", "")
        assert "# Task Report:" in r.text
        assert "deep thought" in r.text

    def test_export_task_404(self, monkeypatch):
        c = _build_client(monkeypatch)
        r = c.get("/api/export/no-such-task-xyz")
        assert r.status_code == 404

    def test_export_task_default_json(self, monkeypatch):
        c = _build_client(monkeypatch)
        _seed([{
            "task_id": "def", "description": "default fmt", "status": "success",
            "repo": "o/ed", "created_at": "2024-03-04", "updated_at": "2024-03-04",
        }])
        r = c.get(f"/api/export/{_PREFIX}def")
        assert r.status_code == 200
        # default format is json
        data = r.json()
        assert data["task"]["task_id"] == _PREFIX + "def"

    def test_export_history_json(self, monkeypatch):
        c = _build_client(monkeypatch)
        _seed([
            {"task_id": "hj1", "description": "hist json 1", "status": "success",
             "repo": "o/ehj", "created_at": "2024-04-01", "updated_at": "2024-04-01"},
            {"task_id": "hj2", "description": "hist json 2", "status": "failed",
             "repo": "o/ehj", "created_at": "2024-04-02", "updated_at": "2024-04-02"},
        ])
        r = c.get("/api/export-history", params={"repo": "o/ehj", "format": "json"})
        assert r.status_code == 200
        data = r.json()
        assert data["exported"] == 2
        ids = [t["task_id"] for t in data["tasks"]]
        assert _PREFIX + "hj1" in ids
        assert _PREFIX + "hj2" in ids

    def test_export_history_csv(self, monkeypatch):
        c = _build_client(monkeypatch)
        _seed([
            {"task_id": "hc1", "description": "hist csv 1", "status": "success",
             "repo": "o/ehc", "created_at": "2024-05-01", "updated_at": "2024-05-01"},
        ])
        r = c.get("/api/export-history", params={"repo": "o/ehc", "format": "csv"})
        assert r.status_code == 200
        assert "text/csv" in r.headers.get("content-type", "")
        reader = csv.reader(io.StringIO(r.text))
        rows = list(reader)
        assert rows[0][0] == "task_id"
        assert rows[1][0] == _PREFIX + "hc1"

    def test_export_history_default_json(self, monkeypatch):
        c = _build_client(monkeypatch)
        r = c.get("/api/export-history", params={"repo": "no-such-repo-xyz"})
        assert r.status_code == 200
        data = r.json()
        assert "tasks" in data

    def test_export_task_json_no_secret_leak(self, monkeypatch):
        c = _build_client(monkeypatch)
        _seed([{
            "task_id": "sl", "description": "refactor module", "status": "success",
            "repo": "o/esl", "created_at": "2024-06-01", "updated_at": "2024-06-01",
        }])
        r = c.get(f"/api/export/{_PREFIX}sl", params={"format": "json"})
        low = r.text.lower()
        for secret in ("api_key", "token", "password", "secret"):
            assert secret not in low

    def test_export_history_csv_no_secret_leak(self, monkeypatch):
        c = _build_client(monkeypatch)
        _seed([
            {"task_id": "slc1", "description": "clean task one", "status": "success",
             "repo": "o/eslc", "created_at": "2024-07-01", "updated_at": "2024-07-01"},
        ])
        # filter to our clean repo so other tests' data can't trigger the guard
        r = c.get("/api/export-history", params={"format": "csv", "repo": "o/eslc"})
        assert r.status_code == 200
        low = r.text.lower()
        for secret in ("api_key", "token", "password", "secret"):
            assert secret not in low
