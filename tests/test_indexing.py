"""Unit tests for the repository intelligence and incremental indexing system (Phase 3)."""
import asyncio
from pathlib import Path
import pytest
import aiosqlite
from workspace import Workspace
from indexing import (
    sha256_hash,
    parse_python_symbols,
    index_workspace,
    search_symbols,
    get_project_map,
    build_tree_nodes,
)


@pytest.fixture
def temp_workspace(tmp_path) -> Workspace:
    from config import Settings
    settings = Settings(workspace_root=str(tmp_path), database_path=str(tmp_path / "test.db"))
    ws = Workspace("test_task_3", root=tmp_path, settings=settings)
    return ws


@pytest.mark.asyncio
async def test_sha256_hash():
    assert sha256_hash("hello") == sha256_hash("hello")
    assert sha256_hash("hello") != sha256_hash("world")


@pytest.mark.asyncio
async def test_parse_python_symbols():
    code = """
import os
from math import sin, cos

class MyClass:
    def __init__(self):
        pass

async def my_async_func():
    pass

def my_func():
    pass
"""
    symbols = parse_python_symbols(code)
    types = [s["type"] for s in symbols]
    names = [s["name"] for s in symbols]

    assert "import" in types
    assert "class" in types
    assert "function" in types

    assert "os" in names
    assert "math.sin" in names or "math.cos" in names
    assert "MyClass" in names
    assert "my_async_func" in names
    assert "my_func" in names


@pytest.mark.asyncio
async def test_incremental_indexing_flow(temp_workspace):
    ws = temp_workspace
    db_path = ws.settings.database_path

    # Write initial files
    ws.write_file("main.py", "def add(a, b):\n    return a + b\n")
    ws.write_file("utils.py", "class Helper:\n    pass\n")
    ws.write_file("readme.txt", "Plain text file")

    async with aiosqlite.connect(str(db_path)) as conn:
        # Load schema
        await conn.executescript(
            "CREATE TABLE IF NOT EXISTS repo_files (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, path TEXT NOT NULL, hash TEXT NOT NULL, mtime REAL NOT NULL, indexed_at TEXT NOT NULL, UNIQUE(task_id, path));"
            "CREATE TABLE IF NOT EXISTS repo_symbols (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, path TEXT NOT NULL, symbol_name TEXT NOT NULL, symbol_type TEXT NOT NULL, line_no INTEGER NOT NULL);"
        )
        await conn.commit()

        # 1) Run first indexing pass (all new)
        stats1 = await index_workspace("test_task_3", ws, conn)
        assert stats1["added"] == 3
        assert stats1["updated"] == 0
        assert stats1["deleted"] == 0
        assert stats1["total"] == 3

        # Verify symbols stored
        syms = await search_symbols("test_task_3", "add", conn)
        assert len(syms) == 1
        assert syms[0]["path"] == "main.py"
        assert syms[0]["symbol_type"] == "function"

        # 2) Run second indexing pass (unmodified - should skip)
        stats2 = await index_workspace("test_task_3", ws, conn)
        assert stats2["added"] == 0
        assert stats2["updated"] == 0
        assert stats2["deleted"] == 0
        assert stats2["total"] == 3

        # 3) Modify a file and add a new one
        ws.write_file("main.py", "def add_v2(a, b):\n    return a + b\n")
        ws.write_file("new.py", "import sys\n")

        stats3 = await index_workspace("test_task_3", ws, conn)
        assert stats3["added"] == 1    # new.py
        assert stats3["updated"] == 1  # main.py
        assert stats3["deleted"] == 0
        assert stats3["total"] == 4

        # Verify old symbol is removed and new is added
        syms_old = await search_symbols("test_task_3", "add", conn)
        assert not any(s["symbol_name"] == "add" for s in syms_old)
        syms_new = await search_symbols("test_task_3", "add_v2", conn)
        assert len(syms_new) == 1

        # 4) Delete a file
        ws.delete_file("readme.txt")
        stats4 = await index_workspace("test_task_3", ws, conn)
        assert stats4["added"] == 0
        assert stats4["updated"] == 0
        assert stats4["deleted"] == 1  # readme.txt
        assert stats4["total"] == 3


@pytest.mark.asyncio
async def test_build_tree_nodes(temp_workspace):
    ws = temp_workspace
    db_path = ws.settings.database_path

    # Write files in folder structure
    ws.write_file("main.py", "def main():\n    pass\n")
    ws.write_file("core/engine.py", "class Engine:\n    pass\n")
    ws.write_file("core/utils/math.py", "def add(x, y): return x + y")

    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.executescript(
            "CREATE TABLE IF NOT EXISTS repo_files (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, path TEXT NOT NULL, hash TEXT NOT NULL, mtime REAL NOT NULL, indexed_at TEXT NOT NULL, UNIQUE(task_id, path));"
            "CREATE TABLE IF NOT EXISTS repo_symbols (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, path TEXT NOT NULL, symbol_name TEXT NOT NULL, symbol_type TEXT NOT NULL, line_no INTEGER NOT NULL);"
        )
        await conn.commit()

        # Run index
        await index_workspace("test_task_3", ws, conn)

        # Build tree nodes
        tree = await build_tree_nodes("test_task_3", ws, conn)

        # Verify structure
        assert len(tree) == 2  # 'core' folder and 'main.py' file
        core_node = next(n for n in tree if n["name"] == "core")
        main_node = next(n for n in tree if n["name"] == "main.py")

        assert core_node["type"] == "dir"
        assert main_node["type"] == "file"
        assert len(main_node["symbols"]) == 1
        assert main_node["symbols"][0]["name"] == "main"

        # Check nested children
        assert len(core_node["children"]) == 2  # 'utils' folder and 'engine.py' file
        engine_node = next(n for n in core_node["children"] if n["name"] == "engine.py")
        assert len(engine_node["symbols"]) == 1
        assert engine_node["symbols"][0]["name"] == "Engine"


@pytest.mark.asyncio
async def test_get_project_map(temp_workspace):
    ws = temp_workspace
    db_path = ws.settings.database_path

    ws.write_file("main.py", "def run():\n    pass\n")

    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.executescript(
            "CREATE TABLE IF NOT EXISTS repo_files (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, path TEXT NOT NULL, hash TEXT NOT NULL, mtime REAL NOT NULL, indexed_at TEXT NOT NULL, UNIQUE(task_id, path));"
            "CREATE TABLE IF NOT EXISTS repo_symbols (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, path TEXT NOT NULL, symbol_name TEXT NOT NULL, symbol_type TEXT NOT NULL, line_no INTEGER NOT NULL);"
        )
        await conn.commit()

        await index_workspace("test_task_3", ws, conn)
        pmap = await get_project_map("test_task_3", ws, conn)

        assert "main.py" in pmap
        assert "run" in pmap
