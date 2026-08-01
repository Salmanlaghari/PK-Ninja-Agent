"""Tests for the Background Worker — v0.8.0 Phase 2.

Covers:
* Unit behaviour of :class:`backend.worker.BackgroundWorker` (drains the
  scheduler queue, concurrency limit, independent daemon execution, failure
  marking, graceful stop).
* API endpoint ``/api/worker`` (status reporting, opt-in).
"""
import threading
import time

import pytest

from scheduler import QueueStatus, TaskScheduler, reset_scheduler
from worker import (BackgroundWorker, get_worker, init_worker, reset_worker,
                    stop_worker)


# ── Helpers ────────────────────────────────────────────────────────────────

class _FakeStart:
    """Records calls to start_fn; optionally delays or raises."""

    def __init__(self, delay=0.0, fail_ids=None):
        self.calls = []
        self._delay = delay
        self._fail_ids = set(fail_ids or [])
        self._lock = threading.Lock()

    def __call__(self, task_id, description, repo_full):
        with self._lock:
            self.calls.append((task_id, description, repo_full))
        if self._delay:
            time.sleep(self._delay)
        if task_id in self._fail_ids:
            raise RuntimeError(f"boom-{task_id}")


# ── Unit tests ─────────────────────────────────────────────────────────────

class TestBackgroundWorkerUnit:
    def test_drains_queue_in_priority_order(self):
        sched = TaskScheduler(default_priority=5, default_retries=0)
        start = _FakeStart()
        w = BackgroundWorker(sched, start, max_concurrency=4, poll_interval=0.05)
        now = time.time()
        sched.enqueue("t1", "low", "o/r", priority=9, enqueued_at=now)
        sched.enqueue("t2", "high", "o/r", priority=1, enqueued_at=now)
        sched.enqueue("t3", "mid", "o/r", priority=5, enqueued_at=now)
        w.start()
        # wait for all three to be dispatched
        for _ in range(200):
            if len(start.calls) >= 3:
                break
            time.sleep(0.02)
        w.stop(timeout=2.0)
        ids = [c[0] for c in start.calls]
        # priority order: high, mid, low
        assert ids == ["t2", "t3", "t1"]
        # all marked done
        assert all(sched.get(i).status == QueueStatus.DONE for i in ("t1", "t2", "t3"))
        assert w.completed_count == 3

    def test_concurrency_limit(self):
        """At most max_concurrency tasks run simultaneously."""
        sched = TaskScheduler(default_priority=5, default_retries=0)
        barrier = threading.Event()
        start = _FakeStart(delay=0.3)
        w = BackgroundWorker(sched, start, max_concurrency=2, poll_interval=0.02)
        now = time.time()
        for i in range(5):
            sched.enqueue(f"t{i}", "d", "o/r", priority=5, enqueued_at=now)
        w.start()
        # give it time to dispatch up to 2
        time.sleep(0.15)
        assert w.active_count <= 2
        # wait for completion
        for _ in range(300):
            if w.completed_count >= 5:
                break
            time.sleep(0.02)
        w.stop(timeout=3.0)
        assert w.completed_count == 5
        assert len(start.calls) == 5

    def test_failure_marks_scheduler_failed(self):
        sched = TaskScheduler(default_priority=5, default_retries=0)
        start = _FakeStart(fail_ids=["t1"])
        w = BackgroundWorker(sched, start, max_concurrency=1, poll_interval=0.02)
        sched.enqueue("t1", "d", "o/r", priority=5, enqueued_at=time.time())
        w.start()
        for _ in range(200):
            if w.failed_count >= 1:
                break
            time.sleep(0.02)
        w.stop(timeout=2.0)
        item = sched.get("t1")
        assert item.status == QueueStatus.FAILED
        assert "boom" in (item.error or "")
        assert w.failed_count == 1

    def test_failure_auto_retries_with_budget(self):
        """When retries budget > 0, mark_failed re-queues and worker re-runs."""
        sched = TaskScheduler(default_priority=5, default_retries=2)
        start = _FakeStart(fail_ids=["t1"])
        w = BackgroundWorker(sched, start, max_concurrency=1, poll_interval=0.02)
        sched.enqueue("t1", "d", "o/r", priority=5, enqueued_at=time.time())
        w.start()
        # wait for at least 2 attempts (1 initial + 1 auto-retry)
        for _ in range(300):
            if len(start.calls) >= 2:
                break
            time.sleep(0.02)
        w.stop(timeout=2.0)
        assert len(start.calls) >= 2
        assert all(c[0] == "t1" for c in start.calls)

    def test_empty_queue_is_idle(self):
        sched = TaskScheduler(default_priority=5, default_retries=0)
        start = _FakeStart()
        w = BackgroundWorker(sched, start, max_concurrency=2, poll_interval=0.02)
        w.start()
        time.sleep(0.1)
        assert start.calls == []
        assert w.active_count == 0
        w.stop(timeout=1.0)

    def test_stop_terminates_loop(self):
        sched = TaskScheduler(default_priority=5, default_retries=0)
        w = BackgroundWorker(sched, _FakeStart(), max_concurrency=2, poll_interval=0.02)
        w.start()
        assert w.is_running is True
        w.stop(timeout=1.0)
        assert w.is_running is False

    def test_start_idempotent(self):
        sched = TaskScheduler(default_priority=5, default_retries=0)
        w = BackgroundWorker(sched, _FakeStart(), max_concurrency=2, poll_interval=0.05)
        w.start()
        w.start()  # should not spawn a second thread
        # only one worker thread should exist
        worker_threads = [t for t in threading.enumerate() if t.name == "pk-ninja-worker"]
        assert len(worker_threads) == 1
        w.stop(timeout=1.0)


# ── Singleton lifecycle ────────────────────────────────────────────────────

class TestWorkerSingleton:
    def test_get_worker_none_by_default(self):
        reset_worker()
        assert get_worker() is None

    def test_init_worker_starts_thread(self):
        reset_scheduler()
        reset_worker()
        sched = TaskScheduler(default_priority=5, default_retries=0)
        w = init_worker(sched, _FakeStart(), max_concurrency=2, poll_interval=0.05)
        assert w is get_worker()
        assert w.is_running is True
        stop_worker(timeout=1.0)
        assert get_worker() is None


# ── API tests ──────────────────────────────────────────────────────────────

def _clear_settings_cache():
    from config import get_settings
    get_settings.cache_clear()


def _build_client(monkeypatch, scheduler_enabled=False, **extra):
    import importlib
    reset_worker()
    reset_scheduler()
    _clear_settings_cache()
    monkeypatch.setenv("SCHEDULER_ENABLED", "true" if scheduler_enabled else "false")
    for k, v in extra.items():
        monkeypatch.setenv(k.upper(), str(v))
    _clear_settings_cache()
    import main as _main
    importlib.reload(_main)
    from fastapi.testclient import TestClient
    return TestClient(_main.app)


class TestWorkerAPI:
    def test_worker_status_disabled(self, monkeypatch):
        c = _build_client(monkeypatch, scheduler_enabled=False)
        r = c.get("/api/worker")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False
        assert body["running"] is False
        assert body["active"] == 0

    def test_worker_status_enabled(self, monkeypatch):
        c = _build_client(monkeypatch, scheduler_enabled=True)
        r = c.get("/api/worker")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        # the worker thread should be running
        assert body["running"] is True
        assert body["max_concurrency"] == 2

    def test_worker_drains_queued_task(self, monkeypatch):
        """End-to-end: enqueue a task, worker drains it, scheduler marks done."""
        c = _build_client(monkeypatch, scheduler_enabled=True)
        tid = c.post("/api/tasks", json={"description": "j", "repository": "o/r"}).json()["task_id"]
        # The real start_task will run (and likely fail fast without a real
        # AI provider/repo), but the worker should have dispatched it. Wait
        # for the scheduler item to leave QUEUED.
        for _ in range(200):
            item = c.get(f"/api/queue/{tid}").json()
            if item["status"] != "queued":
                break
            time.sleep(0.03)
        final = c.get(f"/api/queue/{tid}").json()
        # It should have moved out of queued (running/done/failed) — the key
        # invariant is that the worker picked it up.
        assert final["status"] != "queued"

    def test_no_secret_leak_in_worker_status(self, monkeypatch):
        c = _build_client(monkeypatch, scheduler_enabled=True)
        r = c.get("/api/worker")
        low = r.text.lower()
        for secret in ("api_key", "token", "password", "secret"):
            assert secret not in low
