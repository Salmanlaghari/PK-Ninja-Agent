"""Workspace Manager for PK Ninja Agent (v0.7.0).

Manages the collection of named workspaces under the configured
``WORKSPACE_ROOT``. Each workspace is a top-level directory; the manager
provides list / create / rename / delete / switch operations and tracks
recently-used workspaces in SQLite.

Design:
* **Sandboxed.** All operations resolve names against ``workspace_root``
  and refuse anything that escapes it (path-traversal protection), reusing
  the same discipline as :mod:`workspace`.
* **Backward compatible.** Existing per-task workspaces (created by the
  agent) are surfaced in the list but never deleted by rename/delete of a
  managed name unless explicitly targeted.
* **Recent tracking.** A small ``recent_workspaces`` table records the
  last-accessed time per workspace name so the UI can show recents.
* **No secrets.** Only filesystem paths and metadata are handled.
"""
from __future__ import annotations

import datetime as _dt
import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

log = logging.getLogger("pk_ninja.workspace_manager")

_MAX_NAME = 120
# Names that must never be created/deleted (defensive).
_RESERVED = {"", ".", ".."}


class WorkspaceManagerError(Exception):
    """Raised on invalid workspace names or operations."""


def _safe_name(name: str) -> str:
    """Validate + sanitize a workspace name (must be a single path segment)."""
    if not name or name in _RESERVED:
        raise WorkspaceManagerError("Invalid workspace name.")
    name = name.strip()
    if len(name) > _MAX_NAME:
        raise WorkspaceManagerError("Workspace name too long.")
    # Reject path separators / traversal.
    if "/" in name or "\\" in name or name.startswith("."):
        raise WorkspaceManagerError("Workspace name may not contain path separators or start with a dot.")
    return name


def _root(settings: Any) -> Path:
    p = Path(getattr(settings, "workspace_root", "./workspaces")).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _path(settings: Any, name: str) -> Path:
    safe = _safe_name(name)
    return _root(settings) / safe


_SCHEMA = """
CREATE TABLE IF NOT EXISTS recent_workspaces (
    name TEXT PRIMARY KEY,
    last_accessed TEXT NOT NULL
);
"""


async def _connect(db_path: Path) -> aiosqlite.Connection:
    # Centralized serverless-safe connector (WAL + busy_timeout + dir create).
    from db import connect as _db_connect
    conn = await _db_connect(db_path)
    await conn.executescript(_SCHEMA)
    await conn.commit()
    return conn


def _is_git_repo(p: Path) -> bool:
    return (p / ".git").is_dir()


def _branch(p: Path) -> Optional[str]:
    if not _is_git_repo(p):
        return None
    try:
        import subprocess
        r = subprocess.run(
            ["git", "-C", str(p), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            b = r.stdout.strip()
            return b or None
    except Exception:  # noqa: BLE001
        pass
    return None


def _file_count(p: Path) -> int:
    n = 0
    try:
        for _ in p.rglob("*"):
            if _.is_file() and ".git" not in _.parts:
                n += 1
                if n > 100000:
                    break
    except Exception:  # noqa: BLE001
        pass
    return n


def _size_bytes(p: Path) -> int:
    total = 0
    try:
        for f in p.rglob("*"):
            if f.is_file() and ".git" not in f.parts:
                total += f.stat().st_size
                if total > 10 * 1024 * 1024 * 1024:  # cap at 10GB
                    break
    except Exception:  # noqa: BLE001
        pass
    return total


def _last_modified(p: Path) -> Optional[str]:
    try:
        return _dt.datetime.utcfromtimestamp(p.stat().st_mtime).isoformat() + "Z"
    except Exception:  # noqa: BLE001
        return None


def _describe(settings: Any, name: str, is_default: bool = False) -> Dict[str, Any]:
    p = _path(settings, name)
    exists = p.exists() and p.is_dir()
    return {
        "name": name,
        "path": str(p),
        "is_default": is_default,
        "is_git_repo": _is_git_repo(p) if exists else False,
        "branch": _branch(p) if exists else None,
        "file_count": _file_count(p) if exists else 0,
        "size_bytes": _size_bytes(p) if exists else 0,
        "last_modified": _last_modified(p) if exists else None,
    }


async def list_workspaces(settings: Any) -> List[Dict[str, Any]]:
    """List all workspace directories under the root, sorted by name."""
    root = _root(settings)
    default_ws = ""
    try:
        from settings_store import get_settings_for_user
        s = await get_settings_for_user(settings, None)
        default_ws = s.get("default_workspace", "") or ""
    except Exception:  # noqa: BLE001
        pass
    names = []
    if root.exists():
        for child in sorted(root.iterdir()):
            if child.is_dir():
                names.append(child.name)
    return [_describe(settings, n, is_default=(n == default_ws)) for n in names]


async def create_workspace(settings: Any, name: str,
                           repo: Optional[str] = None) -> Dict[str, Any]:
    """Create a new (empty) workspace directory, optionally cloning a repo."""
    p = _path(settings, name)
    if p.exists():
        raise WorkspaceManagerError(f"Workspace '{name}' already exists.")
    p.mkdir(parents=True, exist_ok=False)
    if repo and repo.strip():
        import subprocess
        try:
            r = subprocess.run(
                ["git", "clone", "--depth", "1", f"https://github.com/{repo.strip()}.git", "."],
                cwd=str(p), capture_output=True, text=True, timeout=120,
            )
            if r.returncode != 0:
                log.warning("git clone failed for %s: %s", repo, r.stderr[:200])
        except FileNotFoundError:
            log.warning("git not installed; workspace created without clone.")
    await _touch_recent(settings, name)
    return _describe(settings, name)


async def rename_workspace(settings: Any, old_name: str,
                           new_name: str) -> Dict[str, Any]:
    """Rename a workspace directory."""
    old_p = _path(settings, old_name)
    new_p = _path(settings, new_name)
    if not old_p.exists():
        raise WorkspaceManagerError(f"Workspace '{old_name}' does not exist.")
    if new_p.exists():
        raise WorkspaceManagerError(f"Workspace '{new_name}' already exists.")
    old_p.rename(new_p)
    await _touch_recent(settings, new_name)
    return _describe(settings, new_name)


async def delete_workspace(settings: Any, name: str) -> Dict[str, Any]:
    """Delete a workspace directory (recursive)."""
    p = _path(settings, name)
    if not p.exists():
        raise WorkspaceManagerError(f"Workspace '{name}' does not exist.")
    desc = _describe(settings, name)
    shutil.rmtree(p)
    # Remove from recents.
    try:
        db_path = Path(getattr(settings, "db_path", "pk_ninja.db"))
        conn = await _connect(db_path)
        try:
            await conn.execute("DELETE FROM recent_workspaces WHERE name = ?", (name,))
            await conn.commit()
        finally:
            await conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("failed to clear recent for %s: %s", name, exc)
    return desc


async def switch_workspace(settings: Any, name: str) -> Dict[str, Any]:
    """Mark a workspace as the current/active one (records recent access)."""
    p = _path(settings, name)
    if not p.exists():
        raise WorkspaceManagerError(f"Workspace '{name}' does not exist.")
    await _touch_recent(settings, name)
    return _describe(settings, name)


async def recent_workspaces(settings: Any, limit: int = 10) -> List[Dict[str, Any]]:
    """Return recently-accessed workspaces (most recent first)."""
    db_path = Path(getattr(settings, "db_path", "pk_ninja.db"))
    try:
        conn = await _connect(db_path)
        try:
            cursor = await conn.execute(
                "SELECT name FROM recent_workspaces ORDER BY last_accessed DESC LIMIT ?",
                (limit,))
            rows = await cursor.fetchall()
        finally:
            await conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("recent_workspaces read failed: %s", exc)
        return []
    default_ws = ""
    try:
        from settings_store import get_settings_for_user
        s = await get_settings_for_user(settings, None)
        default_ws = s.get("default_workspace", "") or ""
    except Exception:  # noqa: BLE001
        pass
    out = []
    for row in rows:
        nm = row["name"]
        p = _path(settings, nm)
        if p.exists():
            out.append(_describe(settings, nm, is_default=(nm == default_ws)))
    return out


async def _touch_recent(settings: Any, name: str) -> None:
    db_path = Path(getattr(settings, "db_path", "pk_ninja.db"))
    now = _dt.datetime.utcnow().isoformat()
    try:
        conn = await _connect(db_path)
        try:
            await conn.execute(
                "INSERT INTO recent_workspaces (name, last_accessed) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET last_accessed=excluded.last_accessed",
                (name, now))
            await conn.commit()
        finally:
            await conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("failed to touch recent for %s: %s", name, exc)
