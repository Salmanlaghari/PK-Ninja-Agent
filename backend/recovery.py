"""v0.8.0 — Autonomous Execution Engine: Recovery System.

Detects tasks that were interrupted (e.g. server crash / restart while a task
was running) and provides safe recovery actions.

Detection heuristic
-------------------
A task is **interrupted** when its persisted status is ``running`` (or
``busy``) but there is *no* live :class:`TaskRuntime` for it in memory. This
means the process died / was restarted and the task never reached a terminal
status.

Recovery actions
----------------
* **detect** — scan the tasks table for interrupted tasks.
* **mark_failed** — transition an interrupted task to ``failed`` (terminal),
  preserving all persisted events / memory so the operator can inspect logs.
* **resume** — re-run the task from scratch via :func:`start_task` (the agent
  re-reads persisted memory + repo context to reconstruct state). This is a
  *fresh* execution, not a mid-step resume, because the original subprocess is
  gone.

Safety
------
* ``RECOVERY_AUTO_RESUME=false`` (default) — detection only; no automatic
  resume. The operator explicitly triggers resume / mark-failed.
* Logs (events) are *never* deleted by recovery — they are the audit trail.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# Statuses that are considered "live" (may have a running subprocess).
_LIVE_STATUSES = {"running", "busy"}

# Statuses that are terminal (recovery should not touch them).
_TERMINAL_STATUSES = {"completed", "done", "failed", "cancelled", "error", "idle"}


def is_interrupted(task_row: Dict[str, Any], has_runtime: bool) -> bool:
    """Return True if a task row looks interrupted (live status, no runtime)."""
    status = (task_row.get("status") or "idle").lower()
    return status in _LIVE_STATUSES and not has_runtime


async def detect_interrupted(
    list_tasks_fn,
    has_runtime_fn,
) -> List[Dict[str, Any]]:
    """Return task rows that are interrupted.

    Parameters
    ----------
    list_tasks_fn : async callable -> List[dict]
        Returns all task rows (e.g. ``db_list_tasks``).
    has_runtime_fn : callable(str) -> bool
        Returns True if a live runtime exists for the task_id.
    """
    rows = await list_tasks_fn()
    interrupted = []
    for row in rows:
        tid = row.get("task_id")
        if tid and is_interrupted(row, has_runtime_fn(tid)):
            row = dict(row)
            row["interrupted"] = True
            interrupted.append(row)
    return interrupted


async def mark_task_failed(
    update_status_fn,
    task_id: str,
    reason: str = "interrupted",
) -> None:
    """Transition a task to 'failed' (terminal). Preserves events/memory.

    ``update_status_fn`` is called with a plain ``"failed"`` string; the
    production caller (main.py) wraps it so the TaskStatus enum is applied.
    """
    await update_status_fn(task_id, "failed")


def resume_task(
    task_id: str,
    description: str,
    repo_full: str,
    start_fn: Callable[[str, str, str], None],
) -> None:
    """Re-run an interrupted task from scratch via start_fn (start_task).

    The agent re-reads persisted task_memory / repo_context to reconstruct
    context, but the subprocess execution starts fresh.
    """
    start_fn(task_id, description, repo_full)


def recovery_summary(interrupted: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a compact summary of the detection result."""
    return {
        "interrupted_count": len(interrupted),
        "interrupted_task_ids": [r.get("task_id") for r in interrupted],
        "auto_resume": False,  # always reported; actual flag read by caller
    }


__all__ = [
    "is_interrupted",
    "detect_interrupted",
    "mark_task_failed",
    "resume_task",
    "recovery_summary",
]
