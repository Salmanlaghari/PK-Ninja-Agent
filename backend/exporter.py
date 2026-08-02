"""v0.8.0 — Autonomous Execution Engine: Export.

Export execution logs, reports, and task history in multiple formats.

The exporter is a *pure transformation* layer: given task + events data
(fetched by the caller, usually from ``history`` or the main DB helpers), it
produces formatted output strings. This keeps it trivially testable without
any database, and lets the API routes decide where the data comes from.

Supported exports
-----------------
* **Logs (JSON)** — structured event log as a pretty-printed JSON string.
* **Logs (text)** — human-readable line-oriented log (``[timestamp] type:
  message``).
* **Report (markdown)** — a one-task executive summary: header, metadata
  table, event timeline, and a footer with counts.
* **History (JSON)** — a list of task summaries (reuses ``history.query_history``).
* **History (CSV)** — the same list as RFC-4180 CSV (task_id, description,
  status, repo, branch, created_at, updated_at).

The module has **no side effects** and opens no connections itself; the API
routes in ``main.py`` fetch the data and hand it to these functions.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, List


# ── Per-task log exports ───────────────────────────────────────────────────

def export_logs_json(task: Dict[str, Any], events: List[Dict[str, Any]]) -> str:
    """Return the task + its events as a pretty-printed JSON string.

    ``events`` items are expected to have ``type``, ``message``, ``data``,
    ``timestamp`` keys (same shape as ``db_list_events`` output).
    """
    payload = {
        "task": _clean_task(task),
        "events": [
            {
                "type": e.get("type", ""),
                "message": e.get("message", ""),
                "data": e.get("data", {}) or {},
                "timestamp": e.get("timestamp", ""),
            }
            for e in events
        ],
        "event_count": len(events),
    }
    return json.dumps(payload, indent=2, default=str)


def export_logs_text(task: Dict[str, Any], events: List[Dict[str, Any]]) -> str:
    """Return a human-readable line log.

    Format::

        Task: <task_id> — <description>
        Status: <status>  Repo: <repo>  Branch: <branch>
        Created: <created_at>  Updated: <updated_at>
        Events (<N>):
        [2024-01-01T00:00:00] status: Started analysis
        ...
    """
    lines: List[str] = []
    tid = task.get("task_id", "?")
    desc = task.get("description", "")
    status = task.get("status", "?")
    repo = task.get("repo") or "(none)"
    branch = task.get("branch") or "(none)"
    created = task.get("created_at", "?")
    updated = task.get("updated_at", "?")
    lines.append(f"Task: {tid} — {desc}")
    lines.append(f"Status: {status}  Repo: {repo}  Branch: {branch}")
    lines.append(f"Created: {created}  Updated: {updated}")
    lines.append(f"Events ({len(events)}):")
    for e in events:
        ts = e.get("timestamp", "")
        etype = e.get("type", "")
        msg = e.get("message", "")
        lines.append(f"[{ts}] {etype}: {msg}")
    return "\n".join(lines)


# ── Per-task report (markdown) ─────────────────────────────────────────────

def export_report_markdown(task: Dict[str, Any], events: List[Dict[str, Any]]) -> str:
    """Return an executive-summary markdown report for a single task."""
    tid = task.get("task_id", "?")
    desc = task.get("description", "")
    status = task.get("status", "?")
    repo = task.get("repo") or "(none)"
    branch = task.get("branch") or "(none)"
    created = task.get("created_at", "?")
    updated = task.get("updated_at", "?")

    # Event type counts
    type_counts: Dict[str, int] = {}
    for e in events:
        t = e.get("type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1

    lines: List[str] = []
    lines.append(f"# Task Report: {tid}")
    lines.append("")
    lines.append(f"**{desc}**")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|-------|-------|")
    lines.append(f"| Task ID | `{tid}` |")
    lines.append(f"| Status | {status} |")
    lines.append(f"| Repository | {repo} |")
    lines.append(f"| Branch | {branch} |")
    lines.append(f"| Created | {created} |")
    lines.append(f"| Updated | {updated} |")
    lines.append(f"| Events | {len(events)} |")
    lines.append("")

    if type_counts:
        lines.append("## Event Breakdown")
        lines.append("")
        lines.append("| Type | Count |")
        lines.append("|------|-------|")
        for t in sorted(type_counts):
            lines.append(f"| {t} | {type_counts[t]} |")
        lines.append("")

    lines.append("## Event Timeline")
    lines.append("")
    if not events:
        lines.append("_No events recorded._")
    else:
        for e in events:
            ts = e.get("timestamp", "")
            etype = e.get("type", "")
            msg = e.get("message", "")
            lines.append(f"- **[{ts}] {etype}**: {msg}")
    lines.append("")
    lines.append("---")
    lines.append(f"_Generated by PK Ninja Agent v0.8.0_")
    return "\n".join(lines)


# ── Bulk history exports ───────────────────────────────────────────────────

def export_history_json(items: List[Dict[str, Any]], count: int) -> str:
    """Export a list of task summary dicts as JSON.

    ``items`` is the ``items`` field from ``history.query_history`` (or any
    list of task dicts). ``count`` is the total (pre-pagination) count.
    """
    payload = {
        "count": count,
        "exported": len(items),
        "tasks": [_clean_task(it) for it in items],
    }
    return json.dumps(payload, indent=2, default=str)


def export_history_csv(items: List[Dict[str, Any]]) -> str:
    """Export task summaries as RFC-4180 CSV.

    Columns: task_id, description, status, repo, branch, created_at,
    updated_at.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(
        ["task_id", "description", "status", "repo", "branch",
         "created_at", "updated_at"]
    )
    for it in items:
        writer.writerow([
            it.get("task_id", ""),
            it.get("description", ""),
            it.get("status", ""),
            it.get("repo") or "",
            it.get("branch") or "",
            it.get("created_at", ""),
            it.get("updated_at", ""),
        ])
    return buf.getvalue()


# ── internal helpers ───────────────────────────────────────────────────────

def _clean_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """Return a task dict with only the standard keys (no internal junk)."""
    return {
        "task_id": task.get("task_id", ""),
        "description": task.get("description", ""),
        "status": task.get("status", ""),
        "repo": task.get("repo"),
        "branch": task.get("branch"),
        "created_at": task.get("created_at", ""),
        "updated_at": task.get("updated_at", ""),
    }
