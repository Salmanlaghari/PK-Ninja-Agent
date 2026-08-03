"""Tests for v1.2.0 features: persistence, dependencies, rate limiting, CSRF, Mistral."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))


# ── Scheduler Persistence Tests ──────────────────────────────────────────────

class TestSchedulerPersistence:
    """Test that scheduler state persists to SQLite."""

    def test_persistence_schema_creates_table(self, tmp_path):
        """init_persistence_schema creates the scheduler_queue table."""
        os.environ["DB_PATH"] = str(tmp_path / "test.db")
        from scheduler_persistence import init_persistence_schema
        init_persistence_schema()
        from db import connect_sync
        conn = connect_sync()
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        conn.close()
        assert "scheduler_queue" in tables
        del os.environ["DB_PATH"]

    def test_persist_and_load(self, tmp_path):
        """Items persisted to DB can be loaded back."""
        os.environ["DB_PATH"] = str(tmp_path / "test.db")
        from scheduler_persistence import init_persistence_schema, persist_enqueue, load_queue
        from scheduler import TaskScheduler
        init_persistence_schema()

        sched = TaskScheduler()
        now = time.time()
        item = sched.enqueue("t1", "test task", "owner/repo", enqueued_at=now)
        persist_enqueue(item)

        # Create fresh scheduler and load
        sched2 = TaskScheduler()
        count = load_queue(sched2)
        assert count == 1
        loaded = sched2.get("t1")
        assert loaded is not None
        assert loaded.task_id == "t1"
        assert loaded.description == "test task"
        del os.environ["DB_PATH"]

    def test_persist_status_update(self, tmp_path):
        """Status updates are persisted."""
        os.environ["DB_PATH"] = str(tmp_path / "test.db")
        from scheduler_persistence import init_persistence_schema, persist_enqueue, persist_status
        init_persistence_schema()

        from scheduler import TaskScheduler
        sched = TaskScheduler()
        now = time.time()
        item = sched.enqueue("t2", "task 2", "o/r", enqueued_at=now)
        persist_enqueue(item)
        persist_status("t2", "done")

        from db import connect_sync
        conn = connect_sync()
        row = conn.execute("SELECT status FROM scheduler_queue WHERE task_id='t2'").fetchone()
        conn.close()
        assert row["status"] == "done"
        del os.environ["DB_PATH"]

    def test_graceful_degradation_no_table(self):
        """Persistence functions don't crash when table is missing."""
        from scheduler_persistence import persist_enqueue, persist_status, persist_remove
        # These should not raise
        mock_item = MagicMock()
        mock_item.task_id = "x"
        mock_item.description = ""
        mock_item.repo_full = ""
        mock_item.priority = 5
        mock_item.status.value = "queued"
        mock_item.retries = 0
        mock_item.max_retries = 1
        mock_item.enqueued_at = time.time()
        mock_item.started_at = None
        mock_item.error = None
        mock_item.depends_on = []
        persist_enqueue(mock_item)  # should not raise
        persist_status("x", "done")  # should not raise
        persist_remove("x")  # should not raise


# ── Task Dependencies Tests ──────────────────────────────────────────────────

class TestTaskDependencies:
    """Test task dependency support in the scheduler."""

    def test_task_with_no_deps_is_queued(self):
        from scheduler import TaskScheduler
        s = TaskScheduler()
        item = s.enqueue("t1", "no deps", "o/r", enqueued_at=time.time())
        assert item.status.value == "queued"

    def test_task_with_unmet_deps_is_waiting(self):
        from scheduler import TaskScheduler
        s = TaskScheduler()
        s.enqueue("t1", "first", "o/r", enqueued_at=time.time())
        item2 = s.enqueue("t2", "depends on t1", "o/r", enqueued_at=time.time(), depends_on=["t1"])
        assert item2.status.value == "waiting"

    def test_task_with_met_deps_is_queued(self):
        from scheduler import TaskScheduler
        s = TaskScheduler()
        t1 = s.enqueue("t1", "first", "o/r", enqueued_at=time.time())
        s.mark_done("t1")
        t2 = s.enqueue("t2", "depends on t1", "o/r", enqueued_at=time.time(), depends_on=["t1"])
        assert t2.status.value == "queued"

    def test_pop_promotes_waiting_when_deps_met(self):
        from scheduler import TaskScheduler
        s = TaskScheduler()
        s.enqueue("t1", "first", "o/r", enqueued_at=time.time())
        s.enqueue("t2", "depends on t1", "o/r", enqueued_at=time.time(), depends_on=["t1"])

        # t1 should pop first
        item = s.pop_next()
        assert item.task_id == "t1"
        s.mark_done("t1")

        # Now t2 should be promoted and pop
        item2 = s.pop_next()
        assert item2 is not None
        assert item2.task_id == "t2"

    def test_depend_on_nonexistent_task(self):
        from scheduler import TaskScheduler
        s = TaskScheduler()
        item = s.enqueue("t1", "depends on missing", "o/r", enqueued_at=time.time(), depends_on=["nonexistent"])
        # nonexistent is not DONE, so should be waiting
        assert item.status.value == "waiting"

    def test_to_dict_includes_depends_on(self):
        from scheduler import TaskScheduler
        s = TaskScheduler()
        s.enqueue("t1", "first", "o/r", enqueued_at=time.time())
        item = s.enqueue("t2", "dep", "o/r", enqueued_at=time.time(), depends_on=["t1"])
        d = item.to_dict()
        assert "depends_on" in d
        assert d["depends_on"] == ["t1"]


# ── Rate Limiter Tests ───────────────────────────────────────────────────────

class TestRateLimiter:
    """Test the in-memory token bucket rate limiter."""

    def test_allows_within_limit(self):
        from rate_limiter import RateLimiter
        rl = RateLimiter(max_requests=10, window_seconds=60)
        allowed, headers = rl.check("test-ip")
        assert allowed is True
        assert "X-RateLimit-Limit" in headers
        assert headers["X-RateLimit-Limit"] == "10"

    def test_blocks_over_limit(self):
        from rate_limiter import RateLimiter
        rl = RateLimiter(max_requests=2, window_seconds=60)
        rl.check("test-ip")
        rl.check("test-ip")
        allowed, headers = rl.check("test-ip")
        assert allowed is False
        assert "Retry-After" in headers

    def test_different_keys_independent(self):
        from rate_limiter import RateLimiter
        rl = RateLimiter(max_requests=1, window_seconds=60)
        allowed1, _ = rl.check("ip-1")
        allowed2, _ = rl.check("ip-2")
        assert allowed1 is True
        assert allowed2 is True

    def test_cleanup_removes_stale(self):
        from rate_limiter import RateLimiter
        rl = RateLimiter(max_requests=10, window_seconds=60)
        rl.check("old-ip")
        removed = rl.cleanup(max_age=0)  # immediate cleanup
        assert removed >= 1

    def test_csrf_token_generation(self):
        from rate_limiter import generate_csrf_token, validate_csrf_token
        token = generate_csrf_token("session-123")
        assert "." in token
        assert validate_csrf_token(token, "session-123") is True

    def test_csrf_token_invalid(self):
        from rate_limiter import validate_csrf_token
        assert validate_csrf_token("invalid", "session") is False
        assert validate_csrf_token("", "session") is False

    def test_csrf_token_wrong_session(self):
        from rate_limiter import generate_csrf_token, validate_csrf_token
        token = generate_csrf_token("session-1")
        assert validate_csrf_token(token, "session-2") is False


# ── Mistral Provider Tests ───────────────────────────────────────────────────

class TestMistralProvider:
    """Test Mistral adapter registration and basic functionality."""

    def test_mistral_registered_in_manager(self):
        from providers.manager import ProviderManager
        from config import get_settings
        get_settings.cache_clear()
        mgr = ProviderManager()
        info = mgr.get_info("mistral")
        assert info is not None
        assert info.display_name == "Mistral AI"
        assert info.requires_api_key is True

    def test_mistral_capability(self):
        from providers.manager import ProviderManager
        mgr = ProviderManager()
        cap = mgr.capability("mistral")
        assert cap is not None
        assert cap.streaming is True

    def test_mistral_in_available_providers(self):
        from providers.manager import ProviderManager
        mgr = ProviderManager()
        assert "mistral" in mgr.available_providers()
