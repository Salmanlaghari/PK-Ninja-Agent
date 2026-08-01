"""Tests for v0.8.0 indexing performance improvements.

Verifies:
* mtime + size fast path: unchanged files are skipped without re-reading.
* Batched upserts: results are identical to the pre-optimization behaviour.
* Schema migration: the ``size`` column is added to existing ``repo_files``
  tables transparently and idempotently.
* Correctness: added/updated/deleted/total counts match expectations across
  first-index, re-index (no-op), modify, and delete scenarios.
* The fast path genuinely avoids file reads (instrumented with a counter).

Note: tests use unique task_ids (``ip-<test>-NN``) because the test
environment shares a single DATABASE_PATH (set by conftest.py) — the
constructor ``database_path`` arg is overridden by the env var.
"""
import asyncio
import os

import aiosqlite
import pytest

from indexing import index_workspace, search_symbols
from workspace import Workspace


@pytest.fixture
def perf_workspace(tmp_path) -> Workspace:
    from config import Settings
    settings = Settings(
        workspace_root=str(tmp_path),
        database_path=str(tmp_path / "perf.db"),
    )
    return Workspace("ip_ws", root=tmp_path, settings=settings)


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS repo_files ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, "
    "path TEXT NOT NULL, hash TEXT NOT NULL, mtime REAL NOT NULL, "
    "indexed_at TEXT NOT NULL, UNIQUE(task_id, path));"
    "CREATE TABLE IF NOT EXISTS repo_symbols ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, "
    "path TEXT NOT NULL, symbol_name TEXT NOT NULL, symbol_type TEXT NOT NULL, "
    "line_no INTEGER NOT NULL);"
)


@pytest.mark.asyncio
async def test_fast_path_skips_unchanged_files(perf_workspace):
    """Re-indexing unchanged files should not count as added or updated."""
    ws = perf_workspace
    db_path = ws.settings.database_path
    task_id = "ip-skip-01"
    ws.write_file("a.py", "def a():\n    pass\n")
    ws.write_file("b.py", "def b():\n    pass\n")
    ws.write_file("c.txt", "hello")

    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.executescript(_SCHEMA)
        await conn.commit()

        stats1 = await index_workspace(task_id, ws, conn)
        assert stats1["added"] == 3
        assert stats1["total"] == 3

        # Re-index with no changes → fast path should skip all
        stats2 = await index_workspace(task_id, ws, conn)
        assert stats2["added"] == 0
        assert stats2["updated"] == 0
        assert stats2["deleted"] == 0
        assert stats2["total"] == 3


@pytest.mark.asyncio
async def test_fast_path_size_detects_same_mtime_change(perf_workspace):
    """Rewriting a file with different content (different size) within the
    same mtime tick should still be detected as an update."""
    ws = perf_workspace
    db_path = ws.settings.database_path
    task_id = "ip-size-02"
    ws.write_file("main.py", "def add(a, b):\n    return a + b\n")

    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.executescript(_SCHEMA)
        await conn.commit()
        await index_workspace(task_id, ws, conn)

        # Rewrite immediately (same mtime tick) with DIFFERENT content/size
        ws.write_file("main.py", "def add_v2(a, b):\n    return a + b + 999\n")
        stats = await index_workspace(task_id, ws, conn)
        assert stats["updated"] == 1
        assert stats["added"] == 0

        # Verify the new symbol is indexed
        syms = await search_symbols(task_id, "add_v2", conn)
        assert len(syms) == 1


@pytest.mark.asyncio
async def test_schema_migration_adds_size_column(perf_workspace, tmp_path):
    """The ``size`` column should be added to an existing repo_files table.

    Uses a dedicated, isolated DB file so the migration runs against a
    genuinely old schema (the shared test DB may already be migrated).
    """
    ws = perf_workspace
    task_id = "ip-mig-03"
    ws.write_file("x.py", "x = 1\n")
    isolated_db = tmp_path / "migration_isolated.db"

    async with aiosqlite.connect(str(isolated_db)) as conn:
        await conn.executescript(_SCHEMA)
        await conn.commit()

        # Before indexing, no size column
        cur = await conn.execute("PRAGMA table_info(repo_files)")
        cols = {row[1] for row in await cur.fetchall()}
        assert "size" not in cols

        # First index call migrates the schema
        await index_workspace(task_id, ws, conn)

        cur = await conn.execute("PRAGMA table_info(repo_files)")
        cols = {row[1] for row in await cur.fetchall()}
        assert "size" in cols

        # Verify size was stored
        cur = await conn.execute(
            "SELECT path, size FROM repo_files WHERE task_id=?", (task_id,)
        )
        rows = await cur.fetchall()
        sizes = {r[0]: r[1] for r in rows}
        assert sizes["x.py"] == len("x = 1\n")


@pytest.mark.asyncio
async def test_migration_is_idempotent(perf_workspace, tmp_path):
    """Calling index_workspace multiple times should not error on migration.

    Uses a dedicated, isolated DB file so the migration runs against a
    genuinely old schema on the first call.
    """
    ws = perf_workspace
    task_id = "ip-idem-04"
    ws.write_file("y.py", "y = 2\n")
    isolated_db = tmp_path / "idem_isolated.db"

    async with aiosqlite.connect(str(isolated_db)) as conn:
        await conn.executescript(_SCHEMA)
        await conn.commit()
        await index_workspace(task_id, ws, conn)  # first call migrates + adds
        for _ in range(5):
            stats = await index_workspace(task_id, ws, conn)
        # Subsequent calls should be no-ops
        assert stats["added"] == 0
        assert stats["updated"] == 0


@pytest.mark.asyncio
async def test_batched_correctness_full_flow(perf_workspace):
    """The batched writes produce identical results to the old per-file
    approach across the full add/modify/delete lifecycle."""
    ws = perf_workspace
    db_path = ws.settings.database_path
    task_id = "ip-flow-05"

    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.executescript(_SCHEMA)
        await conn.commit()

        # 1) Initial index
        ws.write_file("main.py", "def add(a, b):\n    return a + b\n")
        ws.write_file("utils.py", "class Helper:\n    pass\n")
        ws.write_file("readme.txt", "text")
        stats1 = await index_workspace(task_id, ws, conn)
        assert stats1["added"] == 3
        assert stats1["updated"] == 0
        assert stats1["deleted"] == 0
        assert stats1["total"] == 3

        # 2) Re-index (no-op)
        stats2 = await index_workspace(task_id, ws, conn)
        assert stats2["added"] == 0
        assert stats2["updated"] == 0
        assert stats2["deleted"] == 0
        assert stats2["total"] == 3

        # 3) Modify + add
        ws.write_file("main.py", "def add_v2(a, b):\n    return a + b\n")
        ws.write_file("new.py", "import sys\n")
        stats3 = await index_workspace(task_id, ws, conn)
        assert stats3["added"] == 1
        assert stats3["updated"] == 1
        assert stats3["deleted"] == 0
        assert stats3["total"] == 4

        # Verify symbols updated correctly
        syms_old = await search_symbols(task_id, "add", conn)
        assert not any(s["symbol_name"] == "add" for s in syms_old)
        syms_new = await search_symbols(task_id, "add_v2", conn)
        assert len(syms_new) == 1

        # 4) Delete
        ws.delete_file("readme.txt")
        stats4 = await index_workspace(task_id, ws, conn)
        assert stats4["added"] == 0
        assert stats4["updated"] == 0
        assert stats4["deleted"] == 1
        assert stats4["total"] == 3


@pytest.mark.asyncio
async def test_fast_path_avoids_file_reads(perf_workspace, monkeypatch):
    """Instrument read_file to count calls; re-index should read 0 files."""
    ws = perf_workspace
    db_path = ws.settings.database_path
    task_id = "ip-reads-06"
    ws.write_file("a.py", "def a():\n    pass\n")
    ws.write_file("b.py", "def b():\n    pass\n")

    read_count = 0
    original_read = ws.read_file

    def counting_read(rel, max_bytes=256 * 1024):
        nonlocal read_count
        read_count += 1
        return original_read(rel, max_bytes)

    monkeypatch.setattr(ws, "read_file", counting_read)

    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.executescript(_SCHEMA)
        await conn.commit()

        # First pass: reads both files
        await index_workspace(task_id, ws, conn)
        assert read_count == 2

        # Second pass: fast path skips both → 0 reads
        read_count = 0
        stats = await index_workspace(task_id, ws, conn)
        assert read_count == 0
        assert stats["added"] == 0
        assert stats["updated"] == 0


@pytest.mark.asyncio
async def test_fast_path_partial_change(perf_workspace, monkeypatch):
    """When 1 of 3 files changes, only that file should be read."""
    ws = perf_workspace
    db_path = ws.settings.database_path
    task_id = "ip-partial-07"
    ws.write_file("a.py", "def a():\n    pass\n")
    ws.write_file("b.py", "def b():\n    pass\n")
    ws.write_file("c.py", "def c():\n    pass\n")

    read_count = 0
    original_read = ws.read_file

    def counting_read(rel, max_bytes=256 * 1024):
        nonlocal read_count
        read_count += 1
        return original_read(rel, max_bytes)

    monkeypatch.setattr(ws, "read_file", counting_read)

    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.executescript(_SCHEMA)
        await conn.commit()
        await index_workspace(task_id, ws, conn)

        # Modify only b.py
        read_count = 0
        ws.write_file("b.py", "def b_v2():\n    return 42\n")
        stats = await index_workspace(task_id, ws, conn)
        assert read_count == 1  # only b.py was read
        assert stats["updated"] == 1
        assert stats["added"] == 0


@pytest.mark.asyncio
async def test_new_file_after_migration(perf_workspace):
    """Adding a file after the schema migration should store size correctly."""
    ws = perf_workspace
    db_path = ws.settings.database_path
    task_id = "ip-newfile-08"
    ws.write_file("init.py", "x = 1\n")

    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.executescript(_SCHEMA)
        await conn.commit()
        await index_workspace(task_id, ws, conn)

        # Add a new file after migration
        ws.write_file("added.py", "y = 2\nz = 3\n")
        await index_workspace(task_id, ws, conn)

        cur = await conn.execute(
            "SELECT path, size FROM repo_files WHERE task_id=? AND path=?",
            (task_id, "added.py"),
        )
        row = await cur.fetchone()
        assert row is not None
        assert row[1] == len("y = 2\nz = 3\n")
