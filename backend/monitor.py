"""v0.8.0 — Autonomous Execution Engine: Execution Monitor.

Live resource + progress monitoring for running tasks.

Reports per-task and system-wide metrics:

* **CPU usage** — percent of one core (psutil). Falls back to ``unavailable``
  when psutil is not installed.
* **Memory usage** — RSS in MB (psutil). Same graceful fallback.
* **Running commands** — derived from :class:`TaskRuntime.current_proc`
  (the subprocess currently spawned by the terminal).
* **Task duration** — wall-clock since the task's first ``running`` event.
* **Current step** — the latest plan step / event message for the task.
* **Estimated completion** — heuristic ETA based on completed vs total plan
  steps (when a plan is available).

psutil is an optional dependency: if it cannot be imported, every metric that
needs it reports ``unavailable`` so the endpoint never crashes.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import psutil  # type: ignore
    _PSUTIL_OK = True
except Exception:  # noqa: BLE001
    psutil = None  # type: ignore
    _PSUTIL_OK = False


def psutil_available() -> bool:
    """Return True if psutil is importable."""
    return _PSUTIL_OK


# ── System-wide metrics ────────────────────────────────────────────────────

def system_metrics() -> Dict[str, Any]:
    """Return system-wide CPU / memory / process-count metrics."""
    if not _PSUTIL_OK:
        return {
            "cpu_percent": "unavailable",
            "memory_percent": "unavailable",
            "memory_used_mb": "unavailable",
            "memory_total_mb": "unavailable",
            "process_count": "unavailable",
            "psutil": False,
        }
    vm = psutil.virtual_memory()
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "memory_percent": vm.percent,
        "memory_used_mb": round(vm.used / 1024 / 1024, 1),
        "memory_total_mb": round(vm.total / 1024 / 1024, 1),
        "process_count": len(psutil.pids()),
        "psutil": True,
    }


# ── Per-task metrics ───────────────────────────────────────────────────────

def _proc_metrics(proc) -> Dict[str, Any]:
    """Best-effort metrics for a single subprocess (psutil.Process)."""
    if not _PSUTIL_OK or proc is None:
        return {"cpu_percent": "unavailable", "memory_mb": "unavailable",
                "pid": getattr(proc, "pid", None) if proc else None}
    try:
        p = psutil.Process(proc.pid)
        cpu = p.cpu_percent(interval=None)
        mem = p.memory_info().rss
        return {"cpu_percent": cpu, "memory_mb": round(mem / 1024 / 1024, 1),
                "pid": proc.pid}
    except Exception:  # noqa: BLE001 — process may have exited
        return {"cpu_percent": "unavailable", "memory_mb": "unavailable",
                "pid": getattr(proc, "pid", None) if proc else None}


def _duration_seconds(started_at: Optional[str]) -> Optional[float]:
    if not started_at:
        return None
    try:
        # events store ISO timestamps; tolerate 'Z' suffix
        ts = started_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=None)
        now = datetime.utcnow() if dt.tzinfo is None else datetime.now(dt.tzinfo)
        return max(0.0, (now - dt).total_seconds())
    except Exception:  # noqa: BLE001
        return None


def task_metrics(
    runtime,
    *,
    events: Optional[List[Dict[str, Any]]] = None,
    task_row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a live-metrics snapshot for one running task.

    Parameters
    ----------
    runtime : TaskRuntime | None
        The in-memory runtime (may be None if the task already finished).
    events : list[dict] | None
        Recent persisted events for the task (to compute current step + ETA).
    task_row : dict | None
        The tasks-table row (for created_at / branch / status).
    """
    rt = runtime
    status = "idle"
    if task_row:
        status = task_row.get("status", "idle")
    if rt is not None and rt.status:
        status = rt.status

    # running command
    running_command = None
    proc = None
    if rt is not None:
        with rt.current_proc_lock:
            proc = rt.current_proc
        if proc is not None:
            running_command = " ".join(getattr(proc, "args", None) or []) or f"pid:{getattr(proc, 'pid', '?')}"

    proc_info = _proc_metrics(proc)

    # duration: prefer the first 'running' event timestamp, fall back to row
    started_at = None
    current_step = None
    total_steps = 0
    completed_steps = 0
    last_message = None
    if events:
        for ev in events:
            t = ev.get("type", "")
            msg = ev.get("message")
            if msg:
                last_message = msg
            if t in ("status", "task_started", "running") and not started_at:
                started_at = ev.get("timestamp")
            if t in ("plan_step", "step", "progress"):
                total_steps += 1
                if ev.get("data", {}).get("done"):
                    completed_steps += 1
                current_step = msg  # latest plan step wins
            elif t in ("analysis", "plan", "action"):
                current_step = msg  # latest progress-like event wins
        # fall back to the very last event message if nothing matched
        if not current_step:
            current_step = last_message

    if not started_at and task_row:
        started_at = task_row.get("created_at")

    duration = _duration_seconds(started_at)

    # ETA heuristic
    eta_seconds: Optional[float] = None
    if total_steps and completed_steps < total_steps and duration:
        remaining = total_steps - completed_steps
        per_step = duration / max(1, completed_steps) if completed_steps else duration
        eta_seconds = round(per_step * remaining, 1)

    return {
        "task_id": getattr(rt, "task_id", None) or (task_row or {}).get("task_id"),
        "status": status,
        "cpu_percent": proc_info["cpu_percent"],
        "memory_mb": proc_info["memory_mb"],
        "pid": proc_info["pid"],
        "running_command": running_command,
        "duration_seconds": round(duration, 1) if duration is not None else None,
        "current_step": current_step,
        "completed_steps": completed_steps,
        "total_steps": total_steps,
        "eta_seconds": eta_seconds,
        "branch": getattr(rt, "branch", None) or (task_row or {}).get("branch"),
    }


# ── Aggregate snapshot ─────────────────────────────────────────────────────

def monitor_snapshot(runtimes, task_rows_by_id, events_by_id) -> Dict[str, Any]:
    """Build the full monitor snapshot.

    Parameters
    ----------
    runtimes : list[TaskRuntime]
        All live runtimes (``list_runtimes()``).
    task_rows_by_id : dict[str, dict]
        Tasks-table rows keyed by task_id.
    events_by_id : dict[str, list[dict]]
        Recent events keyed by task_id.
    """
    tasks = []
    for rt in runtimes:
        tid = rt.task_id
        tasks.append(task_metrics(
            rt,
            events=events_by_id.get(tid),
            task_row=task_rows_by_id.get(tid),
        ))
    return {
        "system": system_metrics(),
        "tasks": tasks,
        "active_count": sum(1 for t in tasks if t["status"] == "running"),
        "psutil_available": _PSUTIL_OK,
    }


__all__ = [
    "psutil_available",
    "system_metrics",
    "task_metrics",
    "monitor_snapshot",
]
