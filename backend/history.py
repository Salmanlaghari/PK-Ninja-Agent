"""v0.8.0 — Autonomous Execution Engine: Job History.

Complete execution history over the existing ``tasks`` and ``events`` tables.

The history module is a *read* layer on top of the same SQLite database that
``main`` uses to persist every task and its events. It adds:

* **Search** — case-insensitive substring match against the task description
  *and* every event message for that task (so you can find a job by what the
  agent actually said/ did, not just the original prompt).
* **Filtering** — by repository (``repo`` column), by status, and by a date
  range on ``created_at``.
* **Pagination** — ``limit`` / ``offset`` so the UI can page through large
  histories without loading everything at once.
* **Aggregation** — per-task event count, first/last event timestamps, and a
  compact ``events`` preview (latest N) so the history list is useful without a
  second round-trip.

The module never mutates the database; it opens read-only connections to
``settings.db_path`` (same file the rest of the app uses) and degrades
gracefully when the tables do not exist yet (fresh install).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

try:
    from config import get_settings
except ImportError:  # pragma: no cover - relative import fallback
    from .config import get_settings

from models import normalize_status

logger = logging.getLogger(__name__)

# Columns we SELECT from ``tasks``. Kept explicit so the row dict is stable
# even if the table gains columns later.
_TASK_COLUMNS = (
    "task_id, description, status, repo, branch, "
    "created_at, updated_at"
)


async def _connect(db_path: Path) -> aiosqlite.Connection:
    """Open a read-only connection to the shared DB.

    Uses ``file:`` URI with ``mode=ro`` so we can never accidentally write.
    Falls back to a normal connection if the URI form is unsupported.
    """
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = await aiosqlite.connect(uri, uri=True)
    except Exception:  # pragma: no cover - extremely unlikely
        conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    return conn


def _tasks_table_exists(conn: aiosqlite.Connection) -> bool:
    """Synchronous check used inside ``_safe`` wrappers."""
    # Caller is async; this is a cheap pragma lookup done via a cursor.
    return True  # existence is verified at query time instead


async def _table_exists(conn: aiosqlite.Connection, name: str) -> bool:
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    row = await cur.fetchone()
    return row is not None


async def query_history(
    *,
    db_path: Optional[Path] = None,
    repo: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    include_events: int = 0,
) -> Dict[str, Any]:
    """Query the task history with optional filters.

    Parameters
    ----------
    repo:
        Filter by the ``repo`` column (exact match, case-insensitive). ``None``
        or empty string means "all repositories".
    status:
        Filter by normalized status. Legacy values (``pending``/``completed``)
        are normalised *before* comparison, so asking for ``idle`` returns old
        ``pending`` rows too.
    search:
        Case-insensitive substring. Matched against the task description OR any
        event message for that task (a sub-query finds task_ids whose events
        match, which we OR with the description LIKE).
    date_from, date_to:
        ISO-8601 strings (e.g. ``2025-01-01``). Compared lexically against the
        text ``created_at`` column, which works because ISO timestamps sort
        correctly as strings. ``date_from`` is inclusive lower bound,
        ``date_to`` inclusive upper bound.
    limit, offset:
        Pagination. ``limit`` is clamped to [0, 500].
    include_events:
        If > 0, attach the latest ``include_events`` events to each task under
        the ``events`` key. Clamped to [0, 50].

    Returns
    -------
    dict
        ``{"items": [...], "count": N, "limit": L, "offset": O,
        "filters": {...}}``. ``items`` is ordered by ``created_at DESC`` then
        ``task_id`` for stable ordering.
    """
    if db_path is None:
        db_path = get_settings().db_path

    limit = max(0, min(int(limit), 500))
    offset = max(0, int(offset))
    include_events = max(0, min(int(include_events), 50))

    where_parts: List[str] = []
    params: List[Any] = []

    if repo:
        where_parts.append("LOWER(repo) = LOWER(?)")
        params.append(repo)
    if status:
        canonical = normalize_status(status)
        # Normalise both legacy values in the DB so the filter is forgiving.
        if canonical == "idle":
            where_parts.append("LOWER(status) IN (?, ?)")
            params.extend(["idle", "pending"])
        elif canonical == "success":
            where_parts.append("LOWER(status) IN (?, ?)")
            params.extend(["success", "completed"])
        else:
            where_parts.append("LOWER(status) = LOWER(?)")
            params.append(canonical)
    if date_from:
        where_parts.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        where_parts.append("created_at <= ?")
        params.append(date_to)

    if search:
        search_like = f"%{search.lower()}%"
        where_parts.append(
            "(LOWER(description) LIKE ? "
            "OR task_id IN (SELECT task_id FROM events "
            "WHERE LOWER(message) LIKE ?))"
        )
        params.extend([search_like, search_like])

    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

    conn = await _connect(db_path)
    try:
        if not await _table_exists(conn, "tasks"):
            return {
                "items": [],
                "count": 0,
                "limit": limit,
                "offset": offset,
                "filters": _filters_dict(repo, status, search, date_from, date_to),
            }

        # Total count (ignoring pagination)
        count_sql = f"SELECT COUNT(*) AS c FROM tasks{where_sql}"
        cur = await conn.execute(count_sql, params)
        row = await cur.fetchone()
        total = int(row["c"]) if row else 0

        # Page of rows
        page_sql = (
            f"SELECT {_TASK_COLUMNS} FROM tasks{where_sql} "
            "ORDER BY created_at DESC, task_id DESC LIMIT ? OFFSET ?"
        )
        cur = await conn.execute(page_sql, params + [limit, offset])
        rows = await cur.fetchall()
        items: List[Dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            item["status"] = normalize_status(item.get("status") or "idle")
            items.append(item)

        # Attach event summaries if requested
        if include_events > 0 and items:
            task_ids = [it["task_id"] for it in items]
            placeholders = ",".join("?" * len(task_ids))
            ev_sql = (
                "SELECT task_id, type, message, timestamp FROM events "
                f"WHERE task_id IN ({placeholders}) "
                "ORDER BY id ASC"
            )
            cur = await conn.execute(ev_sql, task_ids)
            ev_rows = await cur.fetchall()
            events_by_task: Dict[str, List[Dict[str, Any]]] = {}
            for er in ev_rows:
                events_by_task.setdefault(er["task_id"], []).append(
                    {
                        "type": er["type"],
                        "message": er["message"],
                        "timestamp": er["timestamp"],
                    }
                )
            for it in items:
                evs = events_by_task.get(it["task_id"], [])
                it["event_count"] = len(evs)
                it["first_event_at"] = evs[0]["timestamp"] if evs else None
                it["last_event_at"] = evs[-1]["timestamp"] if evs else None
                it["events"] = evs[-include_events:]  # latest N

        return {
            "items": items,
            "count": total,
            "limit": limit,
            "offset": offset,
            "filters": _filters_dict(repo, status, search, date_from, date_to),
        }
    finally:
        await conn.close()


async def get_job_detail(
    task_id: str,
    *,
    db_path: Optional[Path] = None,
    event_limit: int = 500,
) -> Optional[Dict[str, Any]]:
    """Return a single task plus its full event log (capped at ``event_limit``)."""
    if db_path is None:
        db_path = get_settings().db_path
    event_limit = max(0, min(int(event_limit), 5000))

    conn = await _connect(db_path)
    try:
        if not await _table_exists(conn, "tasks"):
            return None
        cur = await conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM tasks WHERE task_id=?",
            (task_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        item = dict(row)
        item["status"] = normalize_status(item.get("status") or "idle")

        if await _table_exists(conn, "events"):
            cur = await conn.execute(
                "SELECT type, message, data, timestamp FROM events "
                "WHERE task_id=? ORDER BY id ASC LIMIT ?",
                (task_id, event_limit),
            )
            ev_rows = await cur.fetchall()
            events: List[Dict[str, Any]] = []
            for er in ev_rows:
                import json
                try:
                    data = json.loads(er["data"]) if er["data"] else {}
                except (json.JSONDecodeError, TypeError):
                    data = {}
                events.append(
                    {
                        "type": er["type"],
                        "message": er["message"],
                        "data": data,
                        "timestamp": er["timestamp"],
                    }
                )
            item["events"] = events
            item["event_count"] = len(events)
        else:
            item["events"] = []
            item["event_count"] = 0
        return item
    finally:
        await conn.close()


async def history_stats(
    *,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Aggregate statistics over the task history (counts by status, repos)."""
    if db_path is None:
        db_path = get_settings().db_path

    conn = await _connect(db_path)
    try:
        if not await _table_exists(conn, "tasks"):
            return {
                "total_tasks": 0,
                "by_status": {},
                "by_repo": {},
                "total_events": 0,
            }

        cur = await conn.execute(
            "SELECT status, COUNT(*) AS c FROM tasks GROUP BY status"
        )
        by_status: Dict[str, int] = {}
        for r in await cur.fetchall():
            by_status[normalize_status(r["status"] or "idle")] = int(r["c"])

        cur = await conn.execute(
            "SELECT COALESCE(repo, '') AS repo, COUNT(*) AS c "
            "FROM tasks GROUP BY repo ORDER BY c DESC"
        )
        by_repo: Dict[str, int] = {}
        for r in await cur.fetchall():
            key = r["repo"] or "(none)"
            by_repo[key] = int(r["c"])

        total_events = 0
        if await _table_exists(conn, "events"):
            cur = await conn.execute("SELECT COUNT(*) AS c FROM events")
            er = await cur.fetchone()
            total_events = int(er["c"]) if er else 0

        return {
            "total_tasks": sum(by_status.values()),
            "by_status": by_status,
            "by_repo": by_repo,
            "total_events": total_events,
        }
    finally:
        await conn.close()


def _filters_dict(
    repo: Optional[str],
    status: Optional[str],
    search: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
) -> Dict[str, Any]:
    return {
        "repo": repo or None,
        "status": status or None,
        "search": search or None,
        "date_from": date_from or None,
        "date_to": date_to or None,
    }
