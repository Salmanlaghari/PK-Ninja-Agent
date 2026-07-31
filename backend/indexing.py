"""Repository Intelligence: AST-based indexing, SQLite caching, and project mapping."""
from __future__ import annotations

import ast
import datetime as _dt
import hashlib
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite
from workspace import Workspace


def sha256_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def parse_python_symbols(content: str) -> List[Dict[str, Any]]:
    """Extract classes, functions, async functions, and imports from python code."""
    symbols = []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []  # Return empty list on syntax errors

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            symbols.append({
                "name": node.name,
                "type": "class",
                "line_no": node.lineno,
            })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append({
                "name": node.name,
                "type": "function",
                "line_no": node.lineno,
            })
        elif isinstance(node, ast.Import):
            for name in node.names:
                symbols.append({
                    "name": name.name,
                    "type": "import",
                    "line_no": node.lineno,
                })
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for name in node.names:
                full_name = f"{module}.{name.name}" if module else name.name
                symbols.append({
                    "name": full_name,
                    "type": "import",
                    "line_no": node.lineno,
                })
    return symbols


async def index_workspace(task_id: str, ws: Workspace, db: aiosqlite.Connection) -> dict:
    """Incrementally index workspace files and symbols, caching in SQLite."""
    # Ensure row_factory is set to Row
    db.row_factory = aiosqlite.Row

    # 1) Get current cache
    cache = {}
    async with db.execute(
        "SELECT path, hash, mtime FROM repo_files WHERE task_id=?", (task_id,)
    ) as cursor:
        rows = await cursor.fetchall()
        for r in rows:
            cache[r["path"]] = (r["hash"], r["mtime"])

    # 2) Get disk files
    try:
        files = ws.list_files()
    except Exception:
        return {"added": 0, "updated": 0, "deleted": 0, "total": 0}

    indexed_paths = set()
    added_cnt = 0
    updated_cnt = 0
    deleted_cnt = 0

    now_iso = _dt.datetime.utcnow().isoformat() + "Z"

    for rel_path in files:
        indexed_paths.add(rel_path)
        abs_path = ws.safe_path(rel_path)
        if not abs_path.exists() or abs_path.is_dir():
            continue

        mtime = os.path.getmtime(abs_path)
        try:
            content = ws.read_file(rel_path)
        except Exception:
            continue

        file_hash = sha256_hash(content)

        # Cache check
        if rel_path in cache:
            cached_hash, cached_mtime = cache[rel_path]
            if cached_hash == file_hash and abs(cached_mtime - mtime) < 0.01:
                # Up to date, skip
                continue
            updated_cnt += 1
        else:
            added_cnt += 1

        # File changed or new: parse and update SQLite
        symbols = parse_python_symbols(content) if rel_path.endswith(".py") else []

        # Delete old symbols
        await db.execute(
            "DELETE FROM repo_symbols WHERE task_id=? AND path=?", (task_id, rel_path)
        )

        # Insert new symbols
        if symbols:
            await db.executemany(
                "INSERT INTO repo_symbols (task_id, path, symbol_name, symbol_type, line_no) "
                "VALUES (?, ?, ?, ?, ?)",
                [(task_id, rel_path, s["name"], s["type"], s["line_no"]) for s in symbols]
            )

        # Upsert file metadata
        await db.execute(
            "INSERT OR REPLACE INTO repo_files (task_id, path, hash, mtime, indexed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, rel_path, file_hash, mtime, now_iso)
        )

    # 3) Cleanup deleted files
    deleted_paths = set(cache.keys()) - indexed_paths
    for dpath in deleted_paths:
        deleted_cnt += 1
        await db.execute("DELETE FROM repo_files WHERE task_id=? AND path=?", (task_id, dpath))
        await db.execute("DELETE FROM repo_symbols WHERE task_id=? AND path=?", (task_id, dpath))

    await db.commit()
    return {
        "added": added_cnt,
        "updated": updated_cnt,
        "deleted": deleted_cnt,
        "total": len(indexed_paths),
    }


async def search_symbols(task_id: str, query: str, db: aiosqlite.Connection) -> List[dict]:
    """Search for symbols across the repository index (case-insensitive)."""
    db.row_factory = aiosqlite.Row
    async with db.execute(
        "SELECT path, symbol_name, symbol_type, line_no FROM repo_symbols "
        "WHERE task_id=? AND symbol_name LIKE ? ORDER BY symbol_name ASC LIMIT 100",
        (task_id, f"%{query}%"),
    ) as cursor:
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_project_map(task_id: str, ws: Workspace, db: aiosqlite.Connection) -> str:
    """Generate a structured text hierarchy map of the project files, classes, and functions."""
    db.row_factory = aiosqlite.Row
    lines = []
    lines.append("## Project Map (Structure & Symbols)")
    lines.append("")

    # Load all indexed files and symbols
    files = sorted(ws.list_files())
    symbols_by_file = {}
    async with db.execute(
        "SELECT path, symbol_name, symbol_type, line_no FROM repo_symbols "
        "WHERE task_id=? ORDER BY line_no ASC", (task_id,)
    ) as cursor:
        rows = await cursor.fetchall()
        for r in rows:
            symbols_by_file.setdefault(r["path"], []).append(dict(r))

    # Helper to print nested structure
    for rel_path in files:
        lines.append(f"- `{rel_path}`")
        file_symbols = symbols_by_file.get(rel_path, [])
        classes = [s for s in file_symbols if s["symbol_type"] == "class"]
        funcs = [s for s in file_symbols if s["symbol_type"] == "function"]

        if classes:
            lines.append("  * Classes:")
            for c in classes:
                lines.append(f"    - `{c['symbol_name']}` (line {c['line_no']})")
        if funcs:
            lines.append("  * Functions:")
            for f in funcs:
                lines.append(f"    - `{f['symbol_name']}` (line {f['line_no']})")

    return "\n".join(lines)


async def build_tree_nodes(task_id: str, ws: Workspace, db: aiosqlite.Connection) -> List[dict]:
    """Build a nested file explorer tree structure, including symbols for files."""
    db.row_factory = aiosqlite.Row
    try:
        files = ws.list_files()
    except Exception:
        return []

    # Get file symbols
    symbols_by_file = {}
    async with db.execute(
        "SELECT path, symbol_name, symbol_type, line_no FROM repo_symbols WHERE task_id=?", (task_id,)
    ) as cursor:
        rows = await cursor.fetchall()
        for r in rows:
            symbols_by_file.setdefault(r["path"], []).append({
                "name": r["symbol_name"],
                "type": r["symbol_type"],
                "line": r["line_no"],
            })

    # Build recursive structure
    root_node: Dict[str, Any] = {"name": "root", "type": "dir", "children": {}}

    for path in files:
        parts = path.split("/")
        curr = root_node
        for idx, part in enumerate(parts):
            is_last = (idx == len(parts) - 1)
            if is_last:
                curr["children"][part] = {
                    "name": part,
                    "type": "file",
                    "path": path,
                    "symbols": symbols_by_file.get(path, []),
                }
            else:
                if part not in curr["children"]:
                    curr["children"][part] = {
                        "name": part,
                        "type": "dir",
                        "children": {},
                    }
                curr = curr["children"][part]

    def convert_node(node: dict) -> dict:
        if node["type"] == "file":
            return node
        sorted_children = []
        dirs = []
        files = []
        for name, child in node["children"].items():
            converted = convert_node(child)
            if converted["type"] == "dir":
                dirs.append(converted)
            else:
                files.append(converted)
        dirs.sort(key=lambda x: x["name"])
        files.sort(key=lambda x: x["name"])
        return {
            "name": node["name"],
            "type": "dir",
            "children": dirs + files,
        }

    tree = convert_node(root_node)
    return tree.get("children", [])
