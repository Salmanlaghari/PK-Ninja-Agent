"""Repository Intelligence: AST-based indexing, SQLite caching, and project mapping."""
from __future__ import annotations

import ast
import datetime as _dt
import hashlib
import os
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
    """Incrementally index workspace files and symbols, caching in SQLite.

    v0.8.0 performance improvements (backward compatible — same return
    contract; the DB schema gains one optional column via a safe migration):

    1. **mtime + size fast path** — if the cached mtime matches the on-disk
       mtime (within 0.01s) *and* the file size matches the cached size,
       the file is skipped *without* reading its content or computing a
       hash. Both mtime and size come from a single ``os.stat`` call (no
       file read). This is correct: if both the modification time and the
       byte count are identical, the content is identical (barring
       deliberately adversarial same-size same-mtime rewrites, which do
       not occur in normal development workflows).

       A new ``size`` column is added to ``repo_files`` via
       ``ALTER TABLE`` (idempotent — wrapped in try/except so existing
       databases are migrated on first call and subsequent calls are
       no-ops). Rows created before the migration have ``size = NULL``;
       such rows fall through to the slow path once (to populate the
       column) and then benefit from the fast path on subsequent calls.

    2. **Batched upserts** — file metadata and symbol rows are accumulated
       and flushed via ``executemany`` in a single batch instead of one
       ``execute`` per file.

    3. **Batched deletes** — stale symbol rows for changed files and rows
       for deleted files are removed in batched calls.
    """
    # Ensure row_factory is set to Row
    db.row_factory = aiosqlite.Row

    # v0.8.0: migrate the repo_files table to add a size column.
    # Idempotent: if the column already exists the ALTER fails and we
    # swallow the error. This runs on every call but is a no-op after the
    # first successful migration (the PRAGMA check short-circuits).
    try:
        cur = await db.execute("PRAGMA table_info(repo_files)")
        cols = {row[1] for row in await cur.fetchall()}
        if "size" not in cols:
            await db.execute(
                "ALTER TABLE repo_files ADD COLUMN size INTEGER DEFAULT NULL"
            )
            await db.commit()
    except Exception:
        pass  # table doesn't exist yet or ALTER not supported — ignore

    # 1) Get current cache: path -> (hash, mtime, size)
    cache: Dict[str, Tuple[str, float, Optional[int]]] = {}
    async with db.execute(
        "SELECT path, hash, mtime, size FROM repo_files WHERE task_id=?",
        (task_id,),
    ) as cursor:
        rows = await cursor.fetchall()
        for r in rows:
            cache[r["path"]] = (r["hash"], r["mtime"], r["size"])

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

    # Batch accumulators (v0.8.0)
    file_upserts: List[Tuple] = []       # (task_id, path, hash, mtime, size, indexed_at)
    symbol_deletes: List[Tuple] = []     # (task_id, path) for changed files
    symbol_inserts: List[Tuple] = []     # (task_id, path, name, type, line_no)

    for rel_path in files:
        indexed_paths.add(rel_path)
        abs_path = ws.safe_path(rel_path)
        if not abs_path.exists() or abs_path.is_dir():
            continue

        try:
            stat_info = os.stat(abs_path)
            mtime = stat_info.st_mtime
            size = stat_info.st_size
        except OSError:
            continue

        # ── v0.8.0 mtime + size fast path ───────────────────────────────
        if rel_path in cache:
            cached_hash, cached_mtime, cached_size = cache[rel_path]
            mtime_match = abs(cached_mtime - mtime) < 0.01
            size_match = cached_size is not None and cached_size == size
            if mtime_match and size_match:
                # Both mtime and size unchanged → skip read + hash entirely.
                continue
            # If only mtime matches but size differs (same-second rewrite
            # with different content), fall through to the slow path below.

        # mtime/size changed or new file: read content + hash
        try:
            content = ws.read_file(rel_path)
        except Exception:
            continue

        file_hash = sha256_hash(content)

        if rel_path in cache:
            updated_cnt += 1
        else:
            added_cnt += 1

        # Parse symbols (only for .py files)
        symbols = parse_python_symbols(content) if rel_path.endswith(".py") else []

        # Queue batch operations instead of executing per-file
        symbol_deletes.append((task_id, rel_path))
        if symbols:
            for s in symbols:
                symbol_inserts.append(
                    (task_id, rel_path, s["name"], s["type"], s["line_no"])
                )
        file_upserts.append(
            (task_id, rel_path, file_hash, mtime, size, now_iso)
        )

    # 3) Cleanup deleted files
    deleted_paths = set(cache.keys()) - indexed_paths
    deleted_cnt = len(deleted_paths)

    # ── v0.8.0 batched DB writes ────────────────────────────────────────
    # Delete stale symbols for changed files
    if symbol_deletes:
        await db.executemany(
            "DELETE FROM repo_symbols WHERE task_id=? AND path=?",
            symbol_deletes,
        )
    # Insert new symbols
    if symbol_inserts:
        await db.executemany(
            "INSERT INTO repo_symbols (task_id, path, symbol_name, symbol_type, line_no) "
            "VALUES (?, ?, ?, ?, ?)",
            symbol_inserts,
        )
    # Upsert file metadata (note: includes size column now)
    if file_upserts:
        await db.executemany(
            "INSERT OR REPLACE INTO repo_files (task_id, path, hash, mtime, size, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            file_upserts,
        )
    # Delete rows for removed files (files + symbols)
    if deleted_paths:
        del_file_params = [(task_id, p) for p in deleted_paths]
        await db.executemany(
            "DELETE FROM repo_files WHERE task_id=? AND path=?", del_file_params
        )
        await db.executemany(
            "DELETE FROM repo_symbols WHERE task_id=? AND path=?", del_file_params
        )

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
