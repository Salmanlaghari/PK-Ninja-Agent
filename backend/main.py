"""FastAPI application: endpoints, SSE streaming, SQLite persistence, static UI.

Run:
    uvicorn backend.main:app --reload --port 8000

Security:
  * Secrets live only in server-side settings; never serialized to responses.
  * All file/path operations go through Workspace.safe_path (traversal guard).
  * Terminal execution is allowlisted and workspace-locked in terminal.py.
  * GitHub ops are server-side only (github.py).
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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import (BUS, TaskRuntime, cancel_task, get_runtime,
                   list_runtimes, new_task_id, start_task)
from config import Settings, get_settings
from github import GitHubError, create_pull_request, prepare_pull_request, repo_info
from models import (DiffOut, EventOut, EventType, GitBranchRequest,
                    GitCommitRequest, GitPushRequest, PRPrepareRequest,
                    TaskCreate, TaskStatus, TaskSummary)
from workspace import Workspace, WorkspaceError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pk_ninja.main")

# ── SQLite persistence ─────────────────────────────────────────────────────
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
    return conn


async def init_db() -> None:
    conn = await _db()
    try:
        await conn.executescript(
            """
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
            """
        )
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
            (task_id, description, TaskStatus.pending.value, repo, now, now),
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


# ── App ────────────────────────────────────────────────────────────────────
settings = get_settings()

app = FastAPI(title="PK Ninja Agent", version="0.1.0")

# Wire event persistence into the bus.
def _persist(event) -> None:
    try:
        asyncio.run(db_persist_event(event.task_id, event.type.value,
                                     event.message, event.data,
                                     event.timestamp.isoformat() + "Z"))
    except RuntimeError:
        # No running loop (called from within one) — schedule it.
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(db_persist_event(
                event.task_id, event.type.value, event.message, event.data,
                event.timestamp.isoformat() + "Z"))
        else:
            raise

BUS.set_persist(_persist)


@app.on_event("startup")
async def _startup() -> None:
    await init_db()
    log.info("PK Ninja Agent started. DB at %s", settings.db_path)


# ── Health ─────────────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ── Repository ─────────────────────────────────────────────────────────────
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


# ── Tasks ──────────────────────────────────────────────────────────────────
@app.post("/api/tasks")
async def api_create_task(body: TaskCreate) -> dict:
    task_id = new_task_id()
    repo = body.repository or settings.github_repo_full()
    await db_create_task(task_id, body.description, repo)
    start_task(task_id, body.description, repo_full=repo)
    return {"task_id": task_id, "status": TaskStatus.running.value,
            "repository": repo}


@app.get("/api/tasks")
async def api_list_tasks() -> List[dict]:
    rows = await db_list_tasks()
    # Merge live status from runtime if present.
    for r in rows:
        rt = get_runtime(r["task_id"])
        if rt:
            r["status"] = rt.status.value
            if rt.branch:
                r["branch"] = rt.branch
    return rows


@app.get("/api/tasks/{task_id}")
async def api_get_task(task_id: str) -> dict:
    row = await db_get_task(task_id)
    if not row:
        raise HTTPException(404, "Task not found")
    rt = get_runtime(task_id)
    if rt:
        row["status"] = rt.status.value
        if rt.branch:
            row["branch"] = rt.branch
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


# ── SSE live event stream ──────────────────────────────────────────────────
@app.get("/api/tasks/{task_id}/stream")
async def api_task_stream(task_id: str, request: Request):
    """Server-Sent Events stream of live agent activity for a task."""
    q = BUS.subscribe(task_id)
    # Send recent history first.
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
                    if ev.type == EventType.completed or ev.type == EventType.error:
                        # Keep stream open a moment so the client sees the final
                        # event, then close.
                        await asyncio.sleep(0.5)
                        break
                except queue.Empty:
                    # No new event; send a comment as keepalive.
                    yield ": keepalive\n\n"
                except Exception:  # pragma: no cover
                    break
        finally:
            BUS.unsubscribe(task_id, q)

    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ── Diff ───────────────────────────────────────────────────────────────────
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


# ── Git controls (explicit user actions) ───────────────────────────────────
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


# ── PR preparation (explicit user action) ──────────────────────────────────
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


# ── Frontend ───────────────────────────────────────────────────────────────
_frontend_dir = Path(__file__).resolve().parent.parent / "frontend"

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(_frontend_dir / "index.html"))


app.mount("/static", StaticFiles(directory=str(_frontend_dir)), name="static")
