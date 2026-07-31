"""Unit tests for the Repository Context Engine (Phase 2)."""
import os
import pytest
import aiosqlite
from workspace import Workspace
from ai_provider import LocalProvider
from context_engine import (
    extract_keywords,
    find_candidate_files,
    ai_select_relevant_files,
)

@pytest.fixture
def temp_workspace(tmp_path) -> Workspace:
    from config import Settings
    settings = Settings(workspace_root=str(tmp_path), database_path=str(tmp_path / "test.db"))
    ws = Workspace("test_task_context", root=tmp_path, settings=settings)
    return ws


def test_extract_keywords():
    kw = extract_keywords("Add module docstring documentation to main.py and utils.py file")
    assert "docstring" in kw or "documentation" in kw
    # check that STOP_WORDS like "and", "with", "file" are filtered out
    assert "and" not in kw
    assert "file" not in kw


@pytest.mark.asyncio
async def test_find_candidate_files_matching(temp_workspace):
    ws = temp_workspace
    db_path = ws.settings.database_path

    # Write initial files
    ws.write_file("docstring_helper.py", "def add(a, b):\n    return a + b\n")
    ws.write_file("unrelated.txt", "Random file")

    async with aiosqlite.connect(str(db_path)) as conn:
        # Load schema
        await conn.executescript(
            "CREATE TABLE IF NOT EXISTS repo_files (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, path TEXT NOT NULL, hash TEXT NOT NULL, mtime REAL NOT NULL, indexed_at TEXT NOT NULL, UNIQUE(task_id, path));"
            "CREATE TABLE IF NOT EXISTS repo_symbols (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, path TEXT NOT NULL, symbol_name TEXT NOT NULL, symbol_type TEXT NOT NULL, line_no INTEGER NOT NULL);"
        )
        await conn.commit()

        # Insert index row
        await conn.execute(
            "INSERT INTO repo_files (task_id, path, hash, mtime, indexed_at) VALUES (?, ?, ?, ?, ?)",
            ("test_task_context", "docstring_helper.py", "abc", 123.4, "now")
        )
        await conn.execute(
            "INSERT INTO repo_symbols (task_id, path, symbol_name, symbol_type, line_no) VALUES (?, ?, ?, ?, ?)",
            ("test_task_context", "docstring_helper.py", "add", "function", 1)
        )
        await conn.commit()

        # Find candidates
        candidates = await find_candidate_files("test_task_context", "Add docstring python helper", str(db_path), ws)
        assert "docstring_helper.py" in candidates


@pytest.mark.asyncio
async def test_ai_select_relevant_files():
    provider = LocalProvider()
    candidates = ["main.py", "utils.py", "unrelated.py"]

    # Simple candidates list - under or equal to 3, it should return directly
    res = await ai_select_relevant_files("Add docstrings", candidates, provider)
    assert res == candidates
