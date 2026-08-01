"""Tests for the Recovery System — v0.8.0 Phase 5.

Covers:
* :mod:`backend.recovery` unit behaviour (is_interrupted, detect_interrupted,
  mark_task_failed, resume_task, recovery_summary).
* API routes ``/api/recovery``, ``/api/recovery/mark-failed``,
  ``/api/recovery/resume``.
* Log preservation: events are not deleted by recovery.
* Secret-leak guard.
"""
import asyncio

import pytest

import recovery as R


# ── Unit tests ─────────────────────────────────────────────────────────────

class TestRecoveryUnit:
    def test_is_interrupted_true(self):
        row = {"task_id": "t1", "status": "running"}
        assert R.is_interrupted(row, has_runtime=False) is True

    def test_is_interrupted_false_when_runtime_exists(self):
        row = {"task_id": "t1", "status": "running"}
        assert R.is_interrupted(row, has_runtime=True) is False

    def test_is_interrupted_false_when_terminal(self):
        for s in ("completed", "failed", "cancelled", "idle", "done"):
            assert R.is_interrupted({"status": s}, has_runtime=False) is False

    def test_is_interrupted_false_when_idle(self):
        assert R.is_interrupted({"status": "idle"}, has_runtime=False) is False

    def test_detect_interrupted(self):
        async def list_tasks():
            return [
                {"task_id": "t1", "status": "running"},   # interrupted
                {"task_id": "t2", "status": "completed"}, # terminal
                {"task_id": "t3", "status": "running"},   # has runtime -> not interrupted
            ]
        def has_runtime(tid):
            return tid == "t3"
        result = asyncio.run(R.detect_interrupted(list_tasks, has_runtime))
        assert len(result) == 1
        assert result[0]["task_id"] == "t1"
        assert result[0]["interrupted"] is True

    def test_detect_interrupted_empty(self):
        async def list_tasks():
            return []
        def has_runtime(tid):
            return False
        assert asyncio.run(R.detect_interrupted(list_tasks, has_runtime)) == []

    def test_mark_task_failed(self):
        calls = []
        async def update_status(tid, status):
            calls.append((tid, status))
        asyncio.run(R.mark_task_failed(update_status, "t1", reason="crash"))
        assert calls == [("t1", "failed")]

    def test_resume_task_calls_start_fn(self):
        calls = []
        def start_fn(tid, desc, repo):
            calls.append((tid, desc, repo))
        R.resume_task("t1", "desc", "o/r", start_fn)
        assert calls == [("t1", "desc", "o/r")]

    def test_recovery_summary(self):
        s = R.recovery_summary([{"task_id": "a"}, {"task_id": "b"}])
        assert s["interrupted_count"] == 2
        assert s["interrupted_task_ids"] == ["a", "b"]
        assert s["auto_resume"] is False


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


class TestRecoveryAPI:
    def test_detect_empty(self, monkeypatch):
        c = _build_client(monkeypatch)
        r = c.get("/api/recovery")
        assert r.status_code == 200
        body = r.json()
        assert "interrupted_count" in body
        assert "interrupted" in body
        assert isinstance(body["interrupted"], list)
        assert body["auto_resume"] is False

    def test_detect_finds_interrupted_task(self, monkeypatch):
        """Insert a task row with status=running and no runtime -> interrupted."""
        import aiosqlite
        from config import get_settings
        c = _build_client(monkeypatch)
        settings = get_settings()
        db_path = settings.db_path

        async def seed():
            conn = await aiosqlite.connect(str(db_path))
            await conn.execute(
                "INSERT INTO tasks (task_id, description, status, repo, created_at, updated_at) "
                "VALUES ('zzz-interrupted', 'd', 'running', 'o/r', '2024-01-01', '2024-01-01')"
            )
            await conn.commit()
            await conn.close()
        asyncio.run(seed())
        r = c.get("/api/recovery")
        body = r.json()
        ids = [t["task_id"] for t in body["interrupted"]]
        assert "zzz-interrupted" in ids

    def test_mark_failed_unknown_404(self, monkeypatch):
        c = _build_client(monkeypatch)
        r = c.post("/api/recovery/mark-failed", json={"task_id": "nope"})
        assert r.status_code == 404

    def test_mark_failed_on_interrupted(self, monkeypatch):
        import aiosqlite
        from config import get_settings
        c = _build_client(monkeypatch)
        db_path = get_settings().db_path

        async def seed():
            conn = await aiosqlite.connect(str(db_path))
            await conn.execute(
                "INSERT INTO tasks (task_id, description, status, repo, created_at, updated_at) "
                "VALUES ('zzz-mf', 'd', 'running', 'o/r', '2024-01-01', '2024-01-01')"
            )
            # also insert an event so we can verify log preservation
            await conn.execute(
                "INSERT INTO events (task_id, type, message, data, timestamp) "
                "VALUES ('zzz-mf', 'status', 'started', '{}', '2024-01-01')"
            )
            await conn.commit()
            await conn.close()
        asyncio.run(seed())
        r = c.post("/api/recovery/mark-failed", json={"task_id": "zzz-mf"})
        assert r.status_code == 200
        assert r.json()["status"] == "failed"
        # verify the task row is now failed
        g = c.get("/api/tasks/zzz-mf")
        assert g.status_code == 200
        assert g.json()["status"] == "failed"
        # verify the event (log) is still there
        ev = c.get("/api/tasks/zzz-mf/events")
        assert ev.status_code == 200
        assert len(ev.json()) >= 1  # log preserved

    def test_resume_unknown_404(self, monkeypatch):
        c = _build_client(monkeypatch)
        r = c.post("/api/recovery/resume", json={"task_id": "nope"})
        assert r.status_code == 404

    def test_resume_interrupted(self, monkeypatch):
        import aiosqlite
        from config import get_settings
        c = _build_client(monkeypatch)
        db_path = get_settings().db_path

        async def seed():
            conn = await aiosqlite.connect(str(db_path))
            await conn.execute(
                "INSERT INTO tasks (task_id, description, status, repo, created_at, updated_at) "
                "VALUES ('zzz-resume', 'resume me', 'running', 'o/r', '2024-01-01', '2024-01-01')"
            )
            await conn.commit()
            await conn.close()
        asyncio.run(seed())
        r = c.post("/api/recovery/resume", json={"task_id": "zzz-resume"})
        assert r.status_code == 200
        assert r.json()["resumed"] is True

    def test_no_secret_leak(self, monkeypatch):
        c = _build_client(monkeypatch)
        r = c.get("/api/recovery")
        low = r.text.lower()
        for secret in ("api_key", "token", "password", "secret"):
            assert secret not in low
