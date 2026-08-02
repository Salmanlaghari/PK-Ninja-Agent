"""Centralized SQLite connection helper for serverless-safe database access.

Why this exists
---------------
On Vercel (and other serverless platforms) the filesystem is **read-only
except for ``/tmp``**, and each HTTP request may be handled by a fresh,
concurrent function invocation. The original codebase opened SQLite
connections in ~8 different modules with plain ``aiosqlite.connect(db)``
and *no* PRAGMAs. Under concurrent invocations that reliably produced:

    sqlite3.OperationalError: unable to open database file
    sqlite3.OperationalError: database is locked

This module provides a single :func:`connect` (async) and :func:`connect_sync`
that every caller should use. They:

1. Resolve the DB path via :func:`resolve_db_path` which honours, in order:
   - ``DB_PATH`` env var (operator override)
   - ``DATABASE_PATH`` env var (legacy alias)
   - the serverless-aware ``settings.db_path`` (``/tmp/...`` on Vercel)
2. Ensure the parent directory exists (``/tmp`` is writable; project root
   is read-only on serverless — we never fail on a read-only mkdir).
3. Apply the full set of serverless-safe PRAGMAs on **every** connection:
   - ``journal_mode = WAL``      → readers don't block writers
   - ``synchronous = NORMAL``    → safe with WAL, far fewer fsync stalls
   - ``busy_timeout = 30000``    → wait up to 30s instead of instant "locked"
   - ``temp_store = MEMORY``     → no temp files on the read-only FS
   - ``mmap_size = 268435456``   → 256MB memory-mapped I/O for speed
4. Provide :func:`with_retry` — an async retry decorator with exponential
   backoff for transient ``OperationalError`` ("database is locked",
   "unable to open database file").

All of this works identically on local development (the path simply
resolves to ``./data/...`` or the existing ``./pk_ninja.db``).
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

import aiosqlite

log = logging.getLogger("pk_ninja.db")

T = TypeVar("T")

# PRAGMAs applied to every connection (serverless-safe).
_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=30000",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA mmap_size=26843545456" if False else "PRAGMA mmap_size=268435456",  # 256MB
)


def _is_serverless() -> bool:
    """Lightweight serverless check (mirrors config.is_serverless without the
    import cycle — config.py imports nothing from us)."""
    if os.environ.get("VERCEL") == "1":
        return True
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return True
    if os.environ.get("PK_NINJA_SERVERLESS", "").lower() in ("1", "true", "yes"):
        return True
    try:
        return not os.access(".", os.W_OK)
    except OSError:
        return True


def resolve_db_path(explicit: Optional[Path] = None) -> Path:
    """Resolve the SQLite database file path.

    Priority (highest first):
      1. ``explicit`` argument passed by the caller (already-resolved Path)
      2. ``DB_PATH`` environment variable (operator override, recommended on Vercel)
      3. ``DATABASE_PATH`` environment variable (legacy alias)
      4. ``settings.db_path`` — the serverless-aware property from config.py
         (redirects to ``/tmp/pk_ninja.db`` when ``VERCEL=1``)
      5. ``./pk_ninja.db`` (final fallback)

    The parent directory is created when writable; on a read-only serverless
    filesystem we silently skip (the path is already under ``/tmp`` which
    exists). The returned path is absolute.
    """
    if explicit is not None:
        p = Path(explicit)
    else:
        env = os.environ.get("DB_PATH") or os.environ.get("DATABASE_PATH")
        if env:
            p = Path(env)
        else:
            try:
                from config import get_settings  # local import avoids cycle

                p = Path(get_settings().db_path)
            except Exception:
                p = Path("./pk_ninja.db")
    p = p.expanduser().resolve()
    # Ensure parent exists (best-effort — never raise on read-only FS).
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Read-only filesystem (serverless project root). If the path is
        # under /tmp it already exists; if not, the connect() call will
        # surface a clear error. We must not crash at import/resolution time.
        log.debug("db parent mkdir skipped (read-only FS): %s", p.parent)
    return p


def _apply_pragmas_sync(conn: sqlite3.Connection) -> None:
    """Apply serverless-safe PRAGMAs to a sync sqlite3 connection."""
    cur = conn.cursor()
    for pragma in _PRAGMAS:
        try:
            cur.execute(pragma)
        except sqlite3.DatabaseError:
            # Some PRAGMAs can fail on a brand-new / corrupt DB; never let
            # a PRAGMA failure prevent the connection from being usable.
            log.debug("PRAGMA skipped: %s", pragma)
    cur.close()


async def _apply_pragmas(conn: aiosqlite.Connection) -> None:
    """Apply serverless-safe PRAGMAs to an async aiosqlite connection."""
    for pragma in _PRAGMAS:
        try:
            await conn.execute(pragma)
        except sqlite3.DatabaseError:
            log.debug("PRAGMA skipped: %s", pragma)


async def connect(db_path: Optional[Path] = None, *,
                  row_factory: bool = True) -> aiosqlite.Connection:
    """Open an async SQLite connection with full serverless-safe PRAGMAs.

    Parameters
    ----------
    db_path:
        Optional explicit path. If omitted, :func:`resolve_db_path` is used
        (honours ``DB_PATH`` / ``DATABASE_PATH`` env vars + serverless logic).
    row_factory:
        When True (default) sets ``aiosqlite.Row`` so callers get dict-like rows.

    Returns
    -------
    aiosqlite.Connection
        A connection with WAL mode, busy_timeout, etc. already applied.

    The caller is responsible for closing the connection (use
    ``async with db.connect() as conn:`` or close in a ``finally`` block).
    """
    path = resolve_db_path(db_path)
    conn = await aiosqlite.connect(str(path))
    if row_factory:
        conn.row_factory = aiosqlite.Row
    await _apply_pragmas(conn)
    return conn


def connect_sync(db_path: Optional[Path] = None, *,
                 row_factory: bool = True) -> sqlite3.Connection:
    """Open a synchronous SQLite connection with full serverless-safe PRAGMAs.

    Mirrors :func:`connect` for the (rare) sync call sites (e.g. backup.py).
    """
    path = resolve_db_path(db_path)
    conn = sqlite3.connect(str(path), timeout=30.0)
    if row_factory:
        conn.row_factory = sqlite3.Row
    _apply_pragmas_sync(conn)
    return conn


# ── Retry decorator for transient DB errors ──────────────────────────────

_TRANSIENT = ("unable to open database file", "database is locked",
              "database disk image is malformed", "no such savepoint")


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (sqlite3.OperationalError, aiosqlite.OperationalError)):
        msg = str(exc).lower()
        return any(t in msg for t in _TRANSIENT)
    return False


def with_retry(max_retries: int = 5, base_delay: float = 0.15):
    """Decorator: retry an async DB operation on transient SQLite errors.

    Uses exponential backoff (``base_delay * 2**attempt``) capped at ~2s,
    with a small jitter. Non-transient exceptions propagate immediately.
    """
    def deco(fn: Callable[..., Any]):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            delay = base_delay
            last_exc: Optional[BaseException] = None
            for attempt in range(max_retries + 1):
                try:
                    return await fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 — intentional broad
                    last_exc = exc
                    if not _is_transient(exc) or attempt == max_retries:
                        raise
                    jitter = (attempt % 3) * 0.05
                    log.debug("DB transient error (attempt %d): %s — retrying in %.2fs",
                              attempt + 1, exc, delay + jitter)
                    await asyncio.sleep(delay + jitter)
                    delay = min(delay * 2, 2.0)
            # Unreachable, but keeps type-checkers happy.
            if last_exc:
                raise last_exc
            raise RuntimeError("with_retry exhausted with no exception")
        return wrapper
    return deco


__all__ = [
    "connect",
    "connect_sync",
    "resolve_db_path",
    "with_retry",
]
