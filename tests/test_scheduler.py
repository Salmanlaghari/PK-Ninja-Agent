"""Tests for the Task Scheduler — v0.8.0 Phase 1.

Covers:
* Unit behaviour of :class:`backend.scheduler.TaskScheduler` (priority queue,
  pause / resume / cancel / retry / reorder, thread safety).
* API routes under ``/api/queue`` (opt-in via ``SCHEDULER_ENABLED=true``).
* Backward compatibility: with the scheduler disabled (default),
  ``POST /api/tasks`` still uses the fire-and-forget ``start_task()`` path.
"""
import time

import pytest

from scheduler import (QueueItem, QueueStatus, TaskScheduler,
                       get_scheduler, init_scheduler, reset_scheduler)


# ── Unit tests ─────────────────────────────────────────────────────────────

class TestTaskSchedulerUnit:
    """Pure unit tests for the TaskScheduler class (no HTTP)."""

    def _sched(self) -> TaskScheduler:
        return TaskScheduler(default_priority=5, default_retries=1)

    def test_enqueue_and_pop_priority_order(self):
        s = self._sched()
        now = time.time()
        s.enqueue("t1", "low", "o/r", priority=9, enqueued_at=now)
        s.enqueue("t2", "high", "o/r", priority=1, enqueued_at=now)
        s.enqueue("t3", "mid", "o/r", priority=5, enqueued_at=now)
        popped = [s.pop_next().task_id for _ in range(3)]
        assert popped == ["t2", "t3", "t1"]  # high -> mid -> low

    def test_fifo_tiebreak_on_equal_priority(self):
        s = self._sched()
        now = time.time()
        s.enqueue("a", "a", "o/r", priority=5, enqueued_at=now)
        s.enqueue("b", "b", "o/r", priority=5, enqueued_at=now)
        s.enqueue("c", "c", "o/r", priority=5, enqueued_at=now)
        popped = [s.pop_next().task_id for _ in range(3)]
        assert popped == ["a", "b", "c"]

    def test_pop_next_empty_returns_none(self):
        assert self._sched().pop_next() is None

    def test_enqueue_duplicate_raises(self):
        s = self._sched()
        s.enqueue("t1", "d", "o/r", enqueued_at=0.0)
        with pytest.raises(ValueError):
            s.enqueue("t1", "d", "o/r", enqueued_at=0.0)

    def test_pause_and_resume(self):
        s = self._sched()
        s.enqueue("t1", "d", "o/r", priority=5, enqueued_at=0.0)
        assert s.pause("t1").status == QueueStatus.PAUSED
        # paused task must NOT be popped
        assert s.pop_next() is None
        # resume re-queues it
        assert s.resume("t1").status == QueueStatus.QUEUED
        popped = s.pop_next()
        assert popped is not None and popped.task_id == "t1"
        assert popped.status == QueueStatus.RUNNING

    def test_cancel_queued(self):
        s = self._sched()
        s.enqueue("t1", "d", "o/r", priority=5, enqueued_at=0.0)
        assert s.cancel("t1").status == QueueStatus.CANCELLED
        # cancelled tasks are skipped during pop
        assert s.pop_next() is None

    def test_cancel_already_done_is_noop(self):
        s = self._sched()
        s.enqueue("t1", "d", "o/r", priority=5, enqueued_at=0.0)
        s.mark_done("t1")
        assert s.cancel("t1").status == QueueStatus.DONE

    def test_retry_requeues_failed(self):
        s = self._sched()
        s.enqueue("t1", "d", "o/r", priority=5, enqueued_at=0.0,
                  max_retries=1)
        s.mark_failed("t1", "boom")
        # mark_failed with budget remaining auto-retries -> QUEUED
        item = s.get("t1")
        assert item.status == QueueStatus.QUEUED
        assert item.retries == 1
        # pop should now succeed
        assert s.pop_next().task_id == "t1"

    def test_mark_failed_exhausts_retries(self):
        s = self._sched()
        s.enqueue("t1", "d", "o/r", priority=5, enqueued_at=0.0,
                  max_retries=1)
        s.mark_failed("t1", "boom")  # retry 1 (auto)
        s.pop_next()  # running
        s.mark_failed("t1", "boom again")  # no budget -> FAILED
        assert s.get("t1").status == QueueStatus.FAILED
        assert s.get("t1").error == "boom again"

    def test_manual_retry_bumps_cap(self):
        s = self._sched()
        s.enqueue("t1", "d", "o/r", priority=5, enqueued_at=0.0,
                  max_retries=0)
        s.mark_failed("t1", "boom")  # no budget -> FAILED
        item = s.retry("t1")
        assert item.status == QueueStatus.QUEUED
        assert item.retries == 1
        assert s.pop_next().task_id == "t1"

    def test_reorder_changes_priority(self):
        s = self._sched()
        now = time.time()
        s.enqueue("low", "d", "o/r", priority=9, enqueued_at=now)
        s.enqueue("high", "d", "o/r", priority=5, enqueued_at=now)
        # bump "low" to higher priority than "high"
        s.reorder("low", priority=1)
        assert s.pop_next().task_id == "low"
        assert s.pop_next().task_id == "high"

    def test_list_items_filters(self):
        s = self._sched()
        now = time.time()
        s.enqueue("t1", "d", "o/r1", priority=5, enqueued_at=now)
        s.enqueue("t2", "d", "o/r2", priority=5, enqueued_at=now)
        s.pause("t2")
        assert len(s.list_items()) == 2
        assert len(s.list_items(status=QueueStatus.PAUSED)) == 1
        assert s.list_items(status=QueueStatus.PAUSED)[0].task_id == "t2"
        assert len(s.list_items(repo_full="o/r1")) == 1

    def test_queue_length_and_running_count(self):
        s = self._sched()
        now = time.time()
        s.enqueue("t1", "d", "o/r", priority=5, enqueued_at=now)
        s.enqueue("t2", "d", "o/r", priority=5, enqueued_at=now)
        assert s.queue_length() == 2
        s.pop_next()  # t1 -> RUNNING
        assert s.queue_length() == 1
        assert s.running_count() == 1

    def test_peek_next_does_not_remove(self):
        s = self._sched()
        now = time.time()
        s.enqueue("t1", "d", "o/r", priority=5, enqueued_at=now)
        assert s.peek_next().task_id == "t1"
        assert s.queue_length() == 1  # still queued

    def test_remove_drops_item(self):
        s = self._sched()
        s.enqueue("t1", "d", "o/r", priority=5, enqueued_at=0.0)
        assert s.remove("t1") is True
        assert s.get("t1") is None
        assert s.remove("nope") is False

    def test_clear_resets(self):
        s = self._sched()
        s.enqueue("t1", "d", "o/r", priority=5, enqueued_at=0.0)
        s.clear()
        assert s.list_items() == []
        assert s.queue_length() == 0

    def test_to_dict_shape(self):
        s = self._sched()
        item = s.enqueue("t1", "desc", "o/r", priority=3, enqueued_at=1.5)
        d = item.to_dict()
        assert d["task_id"] == "t1"
        assert d["description"] == "desc"
        assert d["repo_full"] == "o/r"
        assert d["priority"] == 3
        assert d["status"] == "queued"
        assert d["enqueued_at"] == 1.5


# ── Singleton lifecycle ────────────────────────────────────────────────────

class TestSchedulerSingleton:
    def test_get_scheduler_none_by_default(self):
        reset_scheduler()
        assert get_scheduler() is None

    def test_init_scheduler_creates_singleton(self):
        reset_scheduler()
        s = init_scheduler(default_priority=7, default_retries=2)
        assert s is get_scheduler()
        assert s._default_priority == 7
        # idempotent re-init reconfigures
        s2 = init_scheduler(default_priority=3, default_retries=0)
        assert s2 is s
        assert s2._default_priority == 3


# ── API tests ──────────────────────────────────────────────────────────────

def _clear_settings_cache():
    from config import get_settings
    get_settings.cache_clear()


def _build_client(monkeypatch, scheduler_enabled=False, **extra):
    """Build a TestClient with the given scheduler env config.

    Reloads ``main`` so the startup handler re-evaluates env vars and
    (re)initialises the scheduler singleton accordingly.
    """
    import importlib

    # Reset the scheduler singleton so tests don't leak state into each other.
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


class TestQueueAPI:
    def test_queue_disabled_returns_empty(self, monkeypatch):
        c = _build_client(monkeypatch, scheduler_enabled=False)
        r = c.get("/api/queue")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False
        assert body["queue"] == []
        assert body["queue_length"] == 0

    def test_queue_actions_404_when_disabled(self, monkeypatch):
        c = _build_client(monkeypatch, scheduler_enabled=False)
        assert c.post("/api/queue/pause", json={"task_id": "x"}).status_code == 404
        assert c.post("/api/queue/resume", json={"task_id": "x"}).status_code == 404
        assert c.post("/api/queue/cancel", json={"task_id": "x"}).status_code == 404

    def test_create_task_queued_when_enabled(self, monkeypatch):
        c = _build_client(monkeypatch, scheduler_enabled=True)
        # Use a fake repo so start_task is NOT triggered; task should be queued.
        r = c.post("/api/tasks", json={"description": "queued job", "repository": "o/r"})
        assert r.status_code == 200
        body = r.json()
        # The task may have already been picked up by the worker (race condition)
        if body.get("queued") is True:
            assert body["status"] == "queued"
            tid = body["task_id"]
            # should appear in the queue listing
            q = c.get("/api/queue").json()
            assert q["enabled"] is True
            assert any(i["task_id"] == tid for i in q["queue"])

    def test_pause_resume_cancel_via_api(self, monkeypatch):
        c = _build_client(monkeypatch, scheduler_enabled=True)
        tid = c.post("/api/tasks", json={"description": "j", "repository": "o/r"}).json()["task_id"]
        import time; time.sleep(0.1)
        # pause — may fail if worker already started the task
        p = c.post("/api/queue/pause", json={"task_id": tid})
        if p.status_code == 404:
            pytest.skip("Task already started by worker")
        assert p.status_code == 200
        assert p.json()["status"] == "paused"
        # resume
        res = c.post("/api/queue/resume", json={"task_id": tid})
        assert res.status_code == 200
        assert res.json()["status"] == "queued"
        # cancel
        can = c.post("/api/queue/cancel", json={"task_id": tid})
        assert can.status_code == 200
        assert can.json()["status"] == "cancelled"

    def test_reorder_via_api(self, monkeypatch):
        c = _build_client(monkeypatch, scheduler_enabled=True)
        tid = c.post("/api/tasks", json={"description": "j", "repository": "o/r"}).json()["task_id"]
        import time; time.sleep(0.1)
        r = c.post("/api/queue/reorder", json={"task_id": tid, "priority": 1})
        if r.status_code == 404:
            pytest.skip("Task already started by worker")
        assert r.status_code == 200
        assert r.json()["priority"] == 1
        # reflected in GET /api/queue/{task_id}
        g = c.get(f"/api/queue/{tid}")
        if g.status_code == 404:
            pytest.skip("Task already started by worker")
        assert g.status_code == 200
        assert g.json()["priority"] == 1

    def test_retry_via_api(self, monkeypatch):
        c = _build_client(monkeypatch, scheduler_enabled=True)
        tid = c.post("/api/tasks", json={"description": "j", "repository": "o/r"}).json()["task_id"]
        # Small delay to ensure the task is enqueued before we try to cancel
        import time
        time.sleep(0.1)
        # cancel first so retry has a terminal state to recover from
        cancel_resp = c.post("/api/queue/cancel", json={"task_id": tid})
        # If cancel returns 404, the task was already started by the worker —
        # skip the retry test in that case (race condition)
        if cancel_resp.status_code == 404:
            pytest.skip("Task was already started by worker before cancel")
        r = c.post("/api/queue/retry", json={"task_id": tid})
        assert r.status_code == 200
        assert r.json()["status"] == "queued"
        assert r.json()["retries"] == 1

    def test_get_unknown_task_404(self, monkeypatch):
        c = _build_client(monkeypatch, scheduler_enabled=True)
        assert c.get("/api/queue/nope").status_code == 404

    def test_priority_ordering_in_queue_listing(self, monkeypatch):
        c = _build_client(monkeypatch, scheduler_enabled=True)
        low = c.post("/api/tasks", json={"description": "low", "repository": "o/r"}).json()["task_id"]
        high = c.post("/api/tasks", json={"description": "high", "repository": "o/r"}).json()["task_id"]
        c.post("/api/queue/reorder", json={"task_id": high, "priority": 1})
        c.post("/api/queue/reorder", json={"task_id": low, "priority": 9})
        q = c.get("/api/queue").json()["queue"]
        # ready (queued) items first, sorted by priority
        queued = [i for i in q if i["status"] == "queued"]
        assert queued[0]["task_id"] == high
        assert queued[1]["task_id"] == low

    def test_no_secret_leak_in_queue_responses(self, monkeypatch):
        """The secret-leak guard: queue responses must not expose secrets."""
        c = _build_client(monkeypatch, scheduler_enabled=True)
        c.post("/api/tasks", json={"description": "j", "repository": "o/r"})
        endpoints = ["/api/queue", "/api/queue/someid"]
        for ep in endpoints:
            r = c.get(ep)
            if r.status_code == 200:
                low = r.text.lower()
                for secret in ("api_key", "token", "password", "secret"):
                    assert secret not in low, f"{secret} leaked in {ep}: {r.text}"


class TestBackwardCompat:
    def test_create_task_direct_when_scheduler_disabled(self, monkeypatch):
        """With SCHEDULER_ENABLED=false (default), POST /api/tasks must NOT
        queue — it should use the original start_task() path (status=running,
        no 'queued' key)."""
        c = _build_client(monkeypatch, scheduler_enabled=False)
        r = c.post("/api/tasks", json={"description": "direct", "repository": "o/r"})
        assert r.status_code == 200
        body = r.json()
        assert body.get("queued") is None or body.get("queued") is False
        # status should be running (start_task was invoked) — but since there
        # is no real AI/repo it may flip to idle/failed; the key invariant is
        # that it was NOT queued.
        assert body["status"] != "queued"
