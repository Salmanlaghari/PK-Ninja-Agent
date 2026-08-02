"""v0.8.0 — Autonomous Execution Engine: Workspace Sessions.

Persistent repository sessions that survive across tasks and server restarts.

A *session* binds a repository (``owner/repo``) to a concrete on-disk workspace
directory and the last-known branch / state. This lets a new task *reuse* an
existing workspace instead of re-cloning, and lets the operator *restore* a
previous session (switch back to its branch, re-index, re-link context).

Schema (added to the main DB via ``ensure_sessions_schema``)::

    CREATE TABLE IF NOT EXISTS sessions (
        session_id  TEXT PRIMARY KEY,        -- uuid hex
        repo_full   TEXT NOT NULL,           -- owner/repo
        branch      TEXT,                    -- last active branch
        workspace   TEXT NOT NULL,           -- absolute path to workspace dir
        state       TEXT NOT NULL,           -- active | closed | interrupted
        task_id     TEXT,                    -- last task linked to this session
        description TEXT,                    -- last task description (for display)
        created_at  TEXT NOT NULL,
        last_active TEXT NOT NULL
    );

All functions are async and take an open ``aiosqlite.Connection`` (or open one
from ``settings.db_path`` when none is supplied).
"""

from __future__ import annotations

import datetime as _dt
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    repo_full   TEXT NOT NULL,
    branch      TEXT,
    workspace   TEXT NOT NULL,
    state       TEXT NOT NULL,
    task_id     TEXT,
    description TEXT,
    created_at  TEXT NOT NULL,
    last_active TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_repo ON sessions(repo_full);
CREATE INDEX IF NOT EXISTS idx_sessions_state ON sessions(state);
"""


def _now() -> str:
    return _dt.datetime.utcnow().isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


async def ensure_sessions_schema(conn: aiosqlite.Connection) -> None:
    """Idempotently create the sessions table + indexes."""
    await conn.executescript(_SCHEMA)
    await conn.commit()


async def _connect(db_path: Path) -> aiosqlite.Connection:
    # Use the centralized, serverless-safe connector (WAL + busy_timeout +
    # /tmp resolution) instead of a bare aiosqlite.connect().
    from db import connect as _db_connect
    conn = await _db_connect(db_path)
    await ensure_sessions_schema(conn)
    return conn


def _row_to_dict(row: aiosqlite.Row) -> Dict[str, Any]:
    return {
        "session_id": row["session_id"],
        "repo_full": row["repo_full"],
        "branch": row["branch"],
        "workspace": row["workspace"],
        "state": row["state"],
        "task_id": row["task_id"],
        "description": row["description"],
        "created_at": row["created_at"],
        "last_active": row["last_active"],
    }


# ── CRUD ───────────────────────────────────────────────────────────────────

async def create_session(
    db_path: Path,
    *,
    repo_full: str,
    workspace: str,
    branch: Optional[str] = None,
    task_id: Optional[str] = None,
    description: Optional[str] = None,
    conn: Optional[aiosqlite.Connection] = None,
) -> Dict[str, Any]:
    """Create a new session row. Returns the session dict."""
    own = conn is None
    if conn is None:
        conn = await _connect(db_path)
    try:
        sid = _new_id()
        now = _now()
        await conn.execute(
            """INSERT INTO sessions
               (session_id, repo_full, branch, workspace, state, task_id,
                description, created_at, last_active)
               VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
            (sid, repo_full, branch, workspace, task_id, description, now, now),
        )
        await conn.commit()
        return {
            "session_id": sid, "repo_full": repo_full, "branch": branch,
            "workspace": workspace, "state": "active", "task_id": task_id,
            "description": description, "created_at": now, "last_active": now,
        }
    finally:
        if own:
            await conn.close()


async def list_sessions(
    db_path: Path,
    *,
    repo_full: Optional[str] = None,
    state: Optional[str] = None,
    conn: Optional[aiosqlite.Connection] = None,
) -> List[Dict[str, Any]]:
    """List sessions, optionally filtered by repo / state."""
    own = conn is None
    if conn is None:
        conn = await _connect(db_path)
    try:
        sql = "SELECT * FROM sessions"
        clauses, params = [], []
        if repo_full is not None:
            clauses.append("repo_full = ?")
            params.append(repo_full)
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY last_active DESC"
        async with conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        if own:
            await conn.close()


async def get_session(
    db_path: Path,
    session_id: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> Optional[Dict[str, Any]]:
    own = conn is None
    if conn is None:
        conn = await _connect(db_path)
    try:
        async with conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_dict(row) if row else None
    finally:
        if own:
            await conn.close()


async def find_active_for_repo(
    db_path: Path,
    repo_full: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """Return the most-recent active session for a repo, if any."""
    own = conn is None
    if conn is None:
        conn = await _connect(db_path)
    try:
        async with conn.execute(
            """SELECT * FROM sessions
               WHERE repo_full = ? AND state = 'active'
               ORDER BY last_active DESC LIMIT 1""",
            (repo_full,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_dict(row) if row else None
    finally:
        if own:
            await conn.close()


async def touch_session(
    db_path: Path,
    session_id: str,
    *,
    branch: Optional[str] = None,
    task_id: Optional[str] = None,
    description: Optional[str] = None,
    state: Optional[str] = None,
    conn: Optional[aiosqlite.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """Update a session's last_active + optional fields. Returns updated row."""
    own = conn is None
    if conn is None:
        conn = await _connect(db_path)
    try:
        now = _now()
        sets = ["last_active = ?"]
        params: List[Any] = [now]
        if branch is not None:
            sets.append("branch = ?")
            params.append(branch)
        if task_id is not None:
            sets.append("task_id = ?")
            params.append(task_id)
        if description is not None:
            sets.append("description = ?")
            params.append(description)
        if state is not None:
            sets.append("state = ?")
            params.append(state)
        params.append(session_id)
        await conn.execute(
            f"UPDATE sessions SET {', '.join(sets)} WHERE session_id = ?",
            params,
        )
        await conn.commit()
        async with conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_dict(row) if row else None
    finally:
        if own:
            await conn.close()


async def close_session(
    db_path: Path,
    session_id: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """Mark a session closed (workspace may still exist on disk)."""
    return await touch_session(db_path, session_id, state="closed", conn=conn)


async def delete_session(
    db_path: Path,
    session_id: str,
    *,
    conn: Optional[aiosqlite.Connection] = None,
) -> bool:
    """Remove a session row entirely. Returns True if it existed."""
    own = conn is None
    if conn is None:
        conn = await _connect(db_path)
    try:
        cur = await conn.execute(
            "DELETE FROM sessions WHERE session_id = ?", (session_id,),
        )
        await conn.commit()
        deleted = cur.rowcount > 0
        await cur.close()
        return deleted
    finally:
        if own:
            await conn.close()


__all__ = [
    "ensure_sessions_schema",
    "create_session",
    "list_sessions",
    "get_session",
    "find_active_for_repo",
    "touch_session",
    "close_session",
    "delete_session",
]
