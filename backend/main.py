"""FastAPI application: endpoints, SSE + WebSocket streaming, SQLite, static UI.

Run:
    uvicorn backend.main:app --reload --port 8000

Security:
  * Secrets live only in server-side settings; never serialized to responses.
  * All file/path operations go through Workspace.safe_path (traversal guard).
  * Terminal execution is allowlisted and workspace-locked in terminal.py.
  * GitHub ops are server-side only (github.py).

Transport:
  * SSE  — GET /api/tasks/{task_id}/stream   (EventSource; works everywhere)
  * WS   — WS  /api/tasks/{task_id}/ws        (WebSocket; lower latency, both ways)
  The frontend prefers WebSocket and falls back to SSE if WS isn't available.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import queue
import sys
from pathlib import Path
from typing import List, Optional

# Make the sibling backend modules importable whether the app is launched as
# `uvicorn backend.main:app` (package form) or `uvicorn main:app` from inside
# backend/ (script form). All backend modules use bare imports (e.g.
# `from agent import ...`) which require backend/ on sys.path.
_BACKEND_DIR = Path(__file__).resolve().parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import (BUS, TaskRuntime, cancel_task, get_runtime,
                   list_runtimes, new_task_id, start_task)
from ai_provider import provider_status
from config import Settings, get_settings
from github import GitHubError, create_pull_request, prepare_pull_request, repo_info
from models import (ConfigOut, DiffOut, EventOut, EventType,
                    GitBranchRequest, GitCommitRequest, GitPushRequest,
                    PRPrepareRequest, TaskCreate, TaskStatus, TaskSummary,
                    normalize_status)
from workspace import Workspace, WorkspaceError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pk_ninja.main")

# ── SQLite persistence ──────────────────────────────────────────────────
import aiosqlite

_DB_PATH: Optional[Path] = None


async def _db() -> aiosqlite.Connection:
    settings = get_settings()
    global _DB_PATH
    _DB_PATH = settings.db_path
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(_DB_PATH))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    # Ensure schema exists on every connection (idempotent + defensive).
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()
    return conn


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    repo TEXT,
    branch TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    type TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);

CREATE TABLE IF NOT EXISTS repo_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    path TEXT NOT NULL,
    hash TEXT NOT NULL,
    mtime REAL NOT NULL,
    indexed_at TEXT NOT NULL,
    UNIQUE(task_id, path)
);
CREATE INDEX IF NOT EXISTS idx_files_task ON repo_files(task_id);

CREATE TABLE IF NOT EXISTS repo_symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    path TEXT NOT NULL,
    symbol_name TEXT NOT NULL,
    symbol_type TEXT NOT NULL,
    line_no INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbols_task ON repo_symbols(task_id);

CREATE TABLE IF NOT EXISTS task_memory (
    task_id TEXT PRIMARY KEY,
    task_context TEXT,
    repo_context TEXT,
    analysis_summary TEXT,
    plan_steps TEXT,
    updated_at TEXT NOT NULL
);
"""


async def init_db() -> None:
    conn = await _db()
    try:
        await conn.commit()
    finally:
        await conn.close()


async def db_create_task(task_id: str, description: str, repo: Optional[str]) -> None:
    now = _dt.datetime.utcnow().isoformat() + "Z"
    conn = await _db()
    try:
        await conn.execute(
            "INSERT INTO tasks (task_id, description, status, repo, branch, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, NULL, ?, ?)",
            (task_id, description, TaskStatus.idle.value, repo, now, now),
        )
        await conn.commit()
    finally:
        await conn.close()


async def db_update_task_status(task_id: str, status: TaskStatus,
                                branch: Optional[str] = None) -> None:
    now = _dt.datetime.utcnow().isoformat() + "Z"
    conn = await _db()
    try:
        if branch is not None:
            await conn.execute(
                "UPDATE tasks SET status=?, branch=?, updated_at=? WHERE task_id=?",
                (status.value, branch, now, task_id))
        else:
            await conn.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                (status.value, now, task_id))
        await conn.commit()
    finally:
        await conn.close()


async def db_get_task(task_id: str) -> Optional[dict]:
    conn = await _db()
    try:
        cur = await conn.execute("SELECT * FROM tasks WHERE task_id=?",
                                 (task_id,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()


async def db_list_tasks() -> List[dict]:
    conn = await _db()
    try:
        cur = await conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 100")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def db_persist_event(task_id: str, etype: str, message: str,
                           data: dict, ts: str) -> None:
    conn = await _db()
    try:
        await conn.execute(
            "INSERT INTO events (task_id, type, message, data, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, etype, message, json.dumps(data, default=str), ts))
        await conn.commit()
    except Exception:  # pragma: no cover
        log.exception("persist event failed")
    finally:
        await conn.close()


async def db_list_events(task_id: str) -> List[dict]:
    conn = await _db()
    try:
        cur = await conn.execute(
            "SELECT type, message, data, timestamp FROM events "
            "WHERE task_id=? ORDER BY id ASC", (task_id,))
        rows = await cur.fetchall()
        out = []
        for r in rows:
            try:
                data = json.loads(r["data"]) if r["data"] else {}
            except json.JSONDecodeError:
                data = {}
            out.append({"type": r["type"], "message": r["message"],
                        "data": data, "timestamp": r["timestamp"]})
        return out
    finally:
        await conn.close()


async def db_save_task_memory(task_id: str, task_context: str, repo_context: str,
                              analysis_summary: str, plan_steps: str) -> None:
    now = _dt.datetime.utcnow().isoformat() + "Z"
    conn = await _db()
    try:
        await conn.execute(
            "INSERT OR REPLACE INTO task_memory (task_id, task_context, repo_context, "
            "analysis_summary, plan_steps, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, task_context, repo_context, analysis_summary, plan_steps, now)
        )
        await conn.commit()
    finally:
        await conn.close()


async def db_get_task_memory(task_id: str) -> Optional[dict]:
    conn = await _db()
    try:
        cur = await conn.execute("SELECT * FROM task_memory WHERE task_id=?", (task_id,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()


# ── App ─────────────────────────────────────────────────────────────────
settings = get_settings()

app = FastAPI(title="PK Ninja Agent", version="0.2.0")


# Wire event persistence into the bus + keep DB task status in sync.
def _persist(event) -> None:
    try:
        asyncio.run(db_persist_event(event.task_id, event.type.value,
                                     event.message, event.data,
                                     event.timestamp.isoformat() + "Z"))
    except RuntimeError:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(db_persist_event(
                event.task_id, event.type.value, event.message, event.data,
                event.timestamp.isoformat() + "Z"))
        else:
            raise
    # Mirror terminal status changes into the tasks table.
    status_map = {
        EventType.session_started.value: TaskStatus.running,
        EventType.completed.value: TaskStatus.success,
        EventType.cancelled.value: TaskStatus.cancelled,
    }
    if event.type.value in status_map:
        try:
            asyncio.run(db_update_task_status(
                event.task_id, status_map[event.type.value],
                branch=event.data.get("branch")))
        except RuntimeError:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(db_update_task_status(
                    event.task_id, status_map[event.type.value],
                    branch=event.data.get("branch")))
            # else: best-effort; runtime status is authoritative anyway.


BUS.set_persist(_persist)


@app.on_event("startup")
async def _startup() -> None:
    await init_db()
    log.info("PK Ninja Agent v0.2 started. DB at %s", settings.db_path)


# ── Health ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "0.2.0"}


# ── Non-secret config (for the frontend) ────────────────────────────────
@app.get("/api/config")
async def api_config() -> dict:
    """Return a non-secret summary of the active AI provider + repo config."""
    # Read settings fresh so runtime env-var changes are reflected (tests and
    # hot-reload), rather than the module-level snapshot from import time.
    fresh = get_settings()
    ps = provider_status(fresh)
    return ConfigOut(
        provider=ps["provider"],
        model=ps["model"],
        configured=ps["configured"],
        streaming_supported=ps["streaming_supported"],
        repository_configured=bool(fresh.github_repo_full()),
    ).model_dump()


# ── Repository ──────────────────────────────────────────────────────────
@app.get("/api/repository")
async def api_repository() -> dict:
    """Return public repo metadata. Never includes tokens."""
    try:
        info = repo_info(settings)
        return {
            "full_name": info.full_name,
            "default_branch": info.default_branch,
            "private": info.private,
            "description": info.description,
            "html_url": info.html_url,
            "configured": True,
        }
    except GitHubError as exc:
        return {"configured": False, "error": str(exc)}


# ── Tasks ───────────────────────────────────────────────────────────────
def _runtime_status(task_id: str, row: dict) -> None:
    """Overlay live runtime status onto a DB row (runtime is authoritative)."""
    rt = get_runtime(task_id)
    if rt:
        row["status"] = rt.status.value
        if rt.branch:
            row["branch"] = rt.branch
    else:
        row["status"] = normalize_status(row.get("status", "idle"))


@app.post("/api/tasks")
async def api_create_task(body: TaskCreate) -> dict:
    task_id = new_task_id()
    fresh = get_settings()
    repo = body.repository or fresh.github_repo_full()
    await db_create_task(task_id, body.description, repo)
    start_task(task_id, body.description, repo_full=repo)
    return {"task_id": task_id, "status": TaskStatus.running.value,
            "repository": repo}


@app.get("/api/tasks")
async def api_list_tasks() -> List[dict]:
    rows = await db_list_tasks()
    for r in rows:
        _runtime_status(r["task_id"], r)
    return rows


@app.get("/api/tasks/{task_id}")
async def api_get_task(task_id: str) -> dict:
    row = await db_get_task(task_id)
    if not row:
        raise HTTPException(404, "Task not found")
    _runtime_status(task_id, row)
    row["events"] = await db_list_events(task_id)
    return row


@app.post("/api/tasks/{task_id}/cancel")
async def api_cancel_task(task_id: str) -> dict:
    ok = cancel_task(task_id)
    if ok:
        await db_update_task_status(task_id, TaskStatus.cancelled)
    return {"cancelled": ok}


@app.get("/api/tasks/{task_id}/events")
async def api_task_events(task_id: str) -> List[dict]:
    return await db_list_events(task_id)


# ── SSE live event stream ───────────────────────────────────────────────
@app.get("/api/tasks/{task_id}/stream")
async def api_task_stream(task_id: str, request: Request):
    """Server-Sent Events stream of live agent activity for a task."""
    q = BUS.subscribe(task_id)
    history = BUS.history(task_id)

    async def event_gen():
        try:
            for ev in history:
                yield f"data: {json.dumps(ev.to_dict())}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: q.get(timeout=1.0)
                    )
                    yield f"data: {json.dumps(ev.to_dict())}\n\n"
                    if ev.type in (EventType.completed, EventType.error,
                                   EventType.cancelled):
                        await asyncio.sleep(0.5)
                        break
                except queue.Empty:
                    yield ": keepalive\n\n"
                except Exception:  # pragma: no cover
                    break
        finally:
            BUS.unsubscribe(task_id, q)

    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ── WebSocket live event stream ─────────────────────────────────────────
@app.websocket("/api/tasks/{task_id}/ws")
async def api_task_ws(ws: WebSocket, task_id: str):
    """WebSocket stream of live agent activity.

    Bidirectional: the client can send ``{"type":"cancel"}`` to cancel the
    task. Server pushes JSON events as they happen (including streamed
    ``thinking`` tokens). This is the preferred transport for the frontend.
    """
    await ws.accept()
    q = BUS.subscribe(task_id)
    # Send recent history first.
    for ev in BUS.history(task_id):
        await ws.send_text(json.dumps(ev.to_dict()))
        if ev.type in (EventType.completed, EventType.error, EventType.cancelled):
            # Already finished; send history then close.
            BUS.unsubscribe(task_id, q)
            await ws.close()
            return

    loop = asyncio.get_event_loop()

    # Reader: listen for client messages (cancel).
    async def _reader():
        try:
            while True:
                msg = await ws.receive_text()
                try:
                    obj = json.loads(msg)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "cancel":
                    cancel_task(task_id)
                    await db_update_task_status(task_id, TaskStatus.cancelled)
        except WebSocketDisconnect:
            pass
        except Exception:  # pragma: no cover
            pass

    reader_task = asyncio.ensure_future(_reader())
    try:
        while True:
            try:
                ev = await loop.run_in_executor(None, lambda: q.get(timeout=1.0))
                await ws.send_text(json.dumps(ev.to_dict()))
                if ev.type in (EventType.completed, EventType.error,
                               EventType.cancelled):
                    await asyncio.sleep(0.3)
                    break
            except queue.Empty:
                # Send a lightweight keepalive ping.
                try:
                    await ws.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break
            except WebSocketDisconnect:
                break
            except Exception:  # pragma: no cover
                break
    finally:
        reader_task.cancel()
        BUS.unsubscribe(task_id, q)
        try:
            await ws.close()
        except Exception:
            pass


# ── Repository Intelligence (Phase 3) ───────────────────────────────────
@app.post("/api/tasks/{task_id}/index")
async def api_index_task(task_id: str) -> dict:
    """Manually trigger or rerun incremental indexing for a task's workspace."""
    rt = get_runtime(task_id)
    if not rt or not rt.workspace:
        raise HTTPException(404, "Task workspace not found")

    from indexing import index_workspace
    conn = await _db()
    try:
        results = await index_workspace(task_id, rt.workspace, conn)
        return {"task_id": task_id, "success": True, "results": results}
    finally:
        await conn.close()


@app.get("/api/tasks/{task_id}/tree")
async def api_task_tree(task_id: str) -> List[dict]:
    """Retrieve the visual folder and file structure, including class/func symbols."""
    rt = get_runtime(task_id)
    if not rt or not rt.workspace:
        raise HTTPException(404, "Task workspace not found")

    from indexing import build_tree_nodes, index_workspace
    conn = await _db()
    try:
        # Run automatic indexing first to ensure tree is up-to-date
        await index_workspace(task_id, rt.workspace, conn)
        tree = await build_tree_nodes(task_id, rt.workspace, conn)
        return tree
    finally:
        await conn.close()


@app.get("/api/tasks/{task_id}/symbols")
async def api_task_symbols(task_id: str, q: str = "") -> List[dict]:
    """Search symbols (classes/functions/imports) within a task's indexed repository."""
    rt = get_runtime(task_id)
    if not rt or not rt.workspace:
        raise HTTPException(404, "Task workspace not found")

    from indexing import search_symbols
    conn = await _db()
    try:
        results = await search_symbols(task_id, q, conn)
        return results
    finally:
        await conn.close()


# ── Diff ────────────────────────────────────────────────────────────────
@app.get("/api/diff")
async def api_diff(task_id: str) -> dict:
    rt = get_runtime(task_id)
    if not rt or not rt.workspace:
        raise HTTPException(404, "Task workspace not found")
    ws = rt.workspace
    return DiffOut(
        task_id=task_id,
        branch=rt.branch,
        staged=ws.git_diff(staged=True),
        unstaged=ws.git_diff(staged=False),
        files=ws.git_changed_files(),
    ).model_dump()


# ── Run a terminal command (real execution, sandboxed) ──────────────────
class RunCommandRequest(BaseModel):
    command: str


@app.post("/api/tasks/{task_id}/run")
async def api_run_command(task_id: str, body: RunCommandRequest) -> dict:
    """Run a real, sandboxed command in the task's workspace.

    This lets the user (or the UI) run a quick command and see real output.
    The command is validated by terminal.py's allowlist/blocklist and runs
    with cwd locked to the workspace. Never fakes output.
    """
    from terminal import TerminalError, run_command as _run
    rt = get_runtime(task_id)
    if not rt or not rt.workspace:
        raise HTTPException(404, "Task workspace not found")
    try:
        result = _run(body.command, rt.workspace, rt=rt)
        return {
            "command": body.command,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "success": result.success,
        }
    except TerminalError as exc:
        return JSONResponse(
            status_code=400,
            content={"command": body.command, "error": str(exc),
                     "success": False, "returncode": 126},
        )


# ── Git controls (explicit user actions) ────────────────────────────────
class GitCheckoutRequest(BaseModel):
    task_id: str
    branch: str
    create: bool = False

class GitStageFileRequest(BaseModel):
    task_id: str
    path: str

class GitUnstageFileRequest(BaseModel):
    task_id: str
    path: str

class GitDiscardFileRequest(BaseModel):
    task_id: str
    path: str

@app.get("/api/git/branches")
async def api_git_list_branches(task_id: str) -> dict:
    rt = get_runtime(task_id)
    if not rt or not rt.workspace:
        raise HTTPException(404, "Task workspace not found")
    ws = rt.workspace
    try:
        branches = ws.git_list_branches()
        current = ws.git_current_branch() or "HEAD"
        return {"task_id": task_id, "branches": branches, "current": current}
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc))

@app.post("/api/git/checkout")
async def api_git_checkout(body: GitCheckoutRequest) -> dict:
    rt = get_runtime(body.task_id)
    if not rt or not rt.workspace:
        raise HTTPException(404, "Task workspace not found")
    ws = rt.workspace
    try:
        res = ws.git_checkout(body.branch, create=body.create)
        if res.success:
            rt.branch = body.branch
            await db_update_task_status(body.task_id, rt.status, branch=body.branch)
        return {"success": res.success, "branch": body.branch, "stdout": res.stdout, "stderr": res.stderr}
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc))

@app.post("/api/git/stage")
async def api_git_stage(body: GitStageFileRequest) -> dict:
    rt = get_runtime(body.task_id)
    if not rt or not rt.workspace:
        raise HTTPException(404, "Task workspace not found")
    ws = rt.workspace
    try:
        res = ws.git_stage_file(body.path)
        return {"success": res.success, "path": body.path, "stdout": res.stdout, "stderr": res.stderr}
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc))

@app.post("/api/git/unstage")
async def api_git_unstage(body: GitUnstageFileRequest) -> dict:
    rt = get_runtime(body.task_id)
    if not rt or not rt.workspace:
        raise HTTPException(404, "Task workspace not found")
    ws = rt.workspace
    try:
        res = ws.git_unstage_file(body.path)
        return {"success": res.success, "path": body.path, "stdout": res.stdout, "stderr": res.stderr}
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc))

@app.post("/api/git/discard")
async def api_git_discard(body: GitDiscardFileRequest) -> dict:
    rt = get_runtime(body.task_id)
    if not rt or not rt.workspace:
        raise HTTPException(404, "Task workspace not found")
    ws = rt.workspace
    try:
        res = ws.git_discard_file(body.path)
        return {"success": res.success, "path": body.path, "stdout": res.stdout, "stderr": res.stderr}
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc))

@app.post("/api/git/branch")
async def api_git_branch(body: GitBranchRequest) -> dict:
    rt = get_runtime(body.task_id)
    if not rt or not rt.workspace:
        raise HTTPException(404, "Task workspace not found")
    ws = rt.workspace
    try:
        res = ws.create_branch(body.branch)
        rt.branch = body.branch
        await db_update_task_status(body.task_id, rt.status, branch=body.branch)
        return {"branch": body.branch, "success": res.success,
                "stdout": res.stdout, "stderr": res.stderr}
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/git/commit")
async def api_git_commit(body: GitCommitRequest) -> dict:
    rt = get_runtime(body.task_id)
    if not rt or not rt.workspace:
        raise HTTPException(404, "Task workspace not found")
    ws = rt.workspace
    ws.git_add_all()
    res = ws.git_commit(body.message)
    return {"success": res.success, "message": body.message,
            "stdout": res.stdout, "stderr": res.stderr,
            "changed_files": ws.git_changed_files()}


@app.post("/api/git/push")
async def api_git_push(body: GitPushRequest) -> dict:
    rt = get_runtime(body.task_id)
    if not rt or not rt.workspace:
        raise HTTPException(404, "Task workspace not found")
    ws = rt.workspace
    if not rt.branch:
        raise HTTPException(400, "No branch set; create a branch first.")
    res = ws.git_push(branch=rt.branch)
    return {"success": res.success, "branch": rt.branch,
            "stdout": res.stdout, "stderr": res.stderr}


# ── PR preparation (explicit user action) ───────────────────────────────
@app.post("/api/pr/prepare")
async def api_pr_prepare(body: PRPrepareRequest) -> dict:
    rt = get_runtime(body.task_id)
    if not rt or not rt.workspace:
        raise HTTPException(404, "Task workspace not found")
    try:
        return prepare_pull_request(rt.workspace, title=body.title, body=body.body)
    except GitHubError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/pr/create")
async def api_pr_create(body: PRPrepareRequest) -> dict:
    """Actually open a PR — explicit user action only."""
    rt = get_runtime(body.task_id)
    if not rt or not rt.workspace:
        raise HTTPException(404, "Task workspace not found")
    try:
        return create_pull_request(rt.workspace, title=body.title, body=body.body)
    except GitHubError as exc:
        raise HTTPException(400, str(exc))


# ── Frontend ────────────────────────────────────────────────────────────
_frontend_dir = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(_frontend_dir / "index.html"))


app.mount("/static", StaticFiles(directory=str(_frontend_dir)), name="static")
