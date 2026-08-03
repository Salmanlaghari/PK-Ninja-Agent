"""v1.2.0 — Scheduler & Worker Persistence.

Persists the in-memory TaskScheduler queue to SQLite so in-flight tasks
survive a process restart. On startup, QUEUED and RUNNING tasks are
automatically recovered.

Design:
- Every enqueue/status-change is written to the ``scheduler_queue`` table.
- On startup, load QUEUED/RUNNING tasks back into the scheduler.
- RUNNING tasks are reset to QUEUED on recovery (they were interrupted).
- All DB operations are wrapped in try/except for graceful degradation.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from db import connect_sync

if TYPE_CHECKING:
    from scheduler import TaskScheduler

log = logging.getLogger("pk_ninja.scheduler_persistence")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduler_queue (
    task_id     TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    repo_full   TEXT NOT NULL DEFAULT '',
    priority    INTEGER NOT NULL DEFAULT 5,
    status      TEXT NOT NULL DEFAULT 'queued',
    retries     INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 1,
    enqueued_at REAL NOT NULL,
    started_at  REAL,
    error       TEXT,
    depends_on  TEXT DEFAULT '[]',
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sq_status ON scheduler_queue(status);
CREATE INDEX IF NOT EXISTS idx_sq_priority ON scheduler_queue(priority);
"""


def init_persistence_schema() -> None:
    """Create the scheduler_queue table if it doesn't exist."""
    try:
        conn = connect_sync()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        log.debug("scheduler_persistence: schema init skipped: %s", exc)


def persist_enqueue(item) -> None:
    """Write a newly enqueued item to the DB."""
    try:
        conn = connect_sync()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO scheduler_queue
                   (task_id, description, repo_full, priority, status,
                    retries, max_retries, enqueued_at, started_at, error,
                    depends_on, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.task_id,
                    item.description,
                    item.repo_full,
                    item.priority,
                    item.status.value,
                    item.retries,
                    item.max_retries,
                    item.enqueued_at,
                    item.started_at,
                    item.error,
                    json.dumps(getattr(item, "depends_on", [])),
                    time.time(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        log.debug("scheduler_persistence: persist_enqueue skipped (table missing?)")


def persist_status(task_id: str, status: str, *, error: str | None = None,
                   retries: int | None = None, started_at: float | None = None) -> None:
    """Update status fields for an existing queued item."""
    try:
        conn = connect_sync()
        try:
            sets = ["status = ?", "updated_at = ?"]
            vals: list = [status, time.time()]
            if error is not None:
                sets.append("error = ?")
                vals.append(error)
            if retries is not None:
                sets.append("retries = ?")
                vals.append(retries)
            if started_at is not None:
                sets.append("started_at = ?")
                vals.append(started_at)
            vals.append(task_id)
            conn.execute(
                f"UPDATE scheduler_queue SET {', '.join(sets)} WHERE task_id = ?",
                vals,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        log.debug("scheduler_persistence: persist_status skipped (table missing?)")


def persist_remove(task_id: str) -> None:
    """Remove a task from the persistent queue."""
    try:
        conn = connect_sync()
        try:
            conn.execute("DELETE FROM scheduler_queue WHERE task_id = ?", (task_id,))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        log.debug("scheduler_persistence: persist_remove skipped (table missing?)")


def load_queue(scheduler: "TaskScheduler") -> int:
    """Recover QUEUED/RUNNING tasks from the DB into the scheduler.

    RUNNING tasks are reset to QUEUED (they were interrupted by a restart).
    Returns the number of recovered tasks.
    """
    try:
        conn = connect_sync()
        try:
            rows = conn.execute(
                """SELECT task_id, description, repo_full, priority, status,
                          retries, max_retries, enqueued_at, started_at, error,
                          depends_on
                   FROM scheduler_queue
                   WHERE status IN ('queued', 'running', 'paused')
                   ORDER BY priority, enqueued_at"""
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return 0

    recovered = 0
    for row in rows:
        task_id = row["task_id"]
        # Don't double-add if already in scheduler
        if scheduler.get(task_id) is not None:
            continue
        status = row["status"]
        # Interrupted RUNNING tasks go back to QUEUED
        if status == "running":
            status = "queued"
        item = scheduler.enqueue(
            task_id=task_id,
            description=row["description"],
            repo_full=row["repo_full"],
            priority=row["priority"],
            max_retries=row["max_retries"],
            enqueued_at=row["enqueued_at"],
        )
        # Override status if it was paused
        if status == "paused":
            from scheduler import QueueStatus as QS
            item.status = QS.PAUSED
        item.retries = row["retries"]
        item.error = row["error"]
        depends = row["depends_on"]
        if depends:
            try:
                item.depends_on = json.loads(depends)
            except (json.JSONDecodeError, TypeError):
                item.depends_on = []
        recovered += 1

    if recovered:
        log.info("scheduler_persistence: recovered %d tasks from DB", recovered)
    return recovered


__all__ = [
    "init_persistence_schema",
    "persist_enqueue",
    "persist_status",
    "persist_remove",
    "load_queue",
]
