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

# ruff: noqa: E402 — imports must come after sys.path manipulation above
from fastapi import (Depends, FastAPI, HTTPException, Request, WebSocket,
                     WebSocketDisconnect)
from fastapi.responses import (FileResponse, JSONResponse, Response,
                               StreamingResponse)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import (BUS, cancel_task, get_runtime,
                   list_runtimes, new_task_id, start_task)
from ai_provider import provider_status
from auth import (AuthError, InvalidTokenError, User,
                  get_auth_service)
from config import get_settings
from github import GitHubError, create_pull_request, prepare_pull_request, repo_info
from settings_store import (get_settings_for_user, update_settings_for_user)
from release_checks import system_health as _system_health
from workspace_manager import (WorkspaceManagerError, create_workspace,
                               delete_workspace, list_workspaces,
                               recent_workspaces, rename_workspace,
                               switch_workspace)
from models import (ConfigOut, DashboardOut, DashboardTaskItem, DiffOut,
                    EventType, GitHubLoginRequest, GitBranchRequest,
                    GitCommitRequest, GitPushRequest, GuestLoginRequest,
                    LoginResponse, PRPrepareRequest, ProviderActionRequest,
                    ProviderCapabilityOut, ProviderHealthOut,
                    ProviderInfoOut, ProviderManagerStatusOut, QueueActionRequest,
                    QueueListOut, RecoveryActionRequest, ReorderRequest, RetryRequest, SessionCreateRequest, SessionOut, SettingsOut, SettingsUpdate,
                    SystemHealthComponent, SystemHealthOut, TaskCreate,
                    TaskQueueItem, TaskStatus, UserOut,
                    WorkspaceActionRequest, WorkspaceCreateRequest,
                    WorkspaceOut, WorkspaceRenameRequest,
                    WorkspaceSessionListOut, WorkspaceSessionOut,
                    CommandCheckOut, CommandCheckRequest,
                    SensitivePathOut, SensitivePathRequest,
                    WorkspaceValidationOut, normalize_status)
from workspace import WorkspaceError

# v0.8.0 — Autonomous Execution Engine (opt-in; no-op when disabled)
from scheduler import (TaskScheduler, get_scheduler,
                       init_scheduler, reset_scheduler)
from worker import (get_worker, init_worker,
                    reset_worker)
from sessions import (close_session as _close_session, create_session,
                      delete_session as _delete_session, find_active_for_repo,
                      get_session, list_sessions, touch_session)
from monitor import monitor_snapshot, system_metrics
from recovery import (detect_interrupted, mark_task_failed, recovery_summary,
                      resume_task)
from history import (get_job_detail, history_stats, query_history)
from exporter import (export_history_csv, export_history_json,
                      export_logs_json, export_logs_text,
                      export_report_markdown)
from security import (full_command_check, is_sensitive_path,
                      validate_workspace)

from structured_logging import setup_logging, RequestLoggingMiddleware
from shutdown import register_shutdown_handlers

# Configure structured logging (JSON in production, plain in dev)
_app_env = os.environ.get("APP_ENV", "development")
setup_logging(
    json_format=_app_env == "production",
    log_level=os.environ.get("LOG_LEVEL", "INFO"),
    log_file=os.environ.get("LOG_FILE"),
)
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
    # Ensure schema exists on every connection (idempotent + defensive so the
    # app works even if the startup hook hasn't run yet, e.g. under reload).
    await conn.executescript(_SCHEMA_SQL)
    # v0.8.0: ensure the sessions table exists too (idempotent).
    from sessions import ensure_sessions_schema
    await ensure_sessions_schema(conn)
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

app = FastAPI(title="PK Ninja Agent", version="1.0.1")

# ── Production middleware & lifecycle ──────────────────────────────────────
app.add_middleware(RequestLoggingMiddleware)
register_shutdown_handlers(app)

# Metrics (optional — graceful degradation if prometheus_client not installed)
try:
    from metrics import setup_metrics
    setup_metrics(app)
except Exception as exc:
    log.info("Metrics setup skipped: %s", exc)

# ── Error handlers (v0.7.0 release prep) ──────────────────────────────────────
@app.exception_handler(404)
async def _not_found_handler(request: Request, exc):
    """Return JSON for API routes; serve SPA index for non-API (frontend) routes."""
    path = request.url.path
    if path.startswith("/api/"):
        return JSONResponse(status_code=404, content={"detail": "Not found", "path": path})
    # SPA fallback: serve index.html so client-side routing works.
    idx = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
    if idx.exists():
        return FileResponse(str(idx), status_code=200)
    return JSONResponse(status_code=404, content={"detail": "Not found"})


@app.exception_handler(500)
async def _server_error_handler(request: Request, exc):
    """Log internal errors without leaking stack traces to the client."""
    log.error("internal error on %s: %s", request.url.path, exc)
    env = getattr(get_settings(), "app_env", "development")
    detail = "Internal server error" if env == "production" else f"Internal server error: {exc}"
    return JSONResponse(status_code=500, content={"detail": detail})

# ── Auth dependency (v0.7.0) ────────────────────────────────────────────────
_bearer_scheme = HTTPBearer(auto_error=False)


def current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> User:
    """Resolve the current user from the request.

    When ``AUTH_ENABLED=false`` (default) this always returns the anonymous
    placeholder, so every endpoint remains open (backward compatible). When
    enabled, a valid session token is required.
    """
    svc = get_auth_service(get_settings())
    authz = credentials.credentials if credentials else None
    query_session = request.query_params.get("session")
    try:
        return svc.require_user_from_request(authz, query_session)
    except InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc))


def _user_to_out(user: User) -> UserOut:
    return UserOut(**user.to_dict())


# ── Authentication routes (v0.7.0) ──────────────────────────────────────────
@app.get("/api/auth/status")
async def auth_status(request: Request) -> dict:
    """Publicly report whether auth is enabled and the current identity.

    This endpoint does **not** require a session — it must be reachable
    before login so the frontend can decide whether to show the login
    screen. When auth is enabled and a valid token is present, the user
    identity is included; otherwise ``user`` is null.
    """
    svc = get_auth_service(get_settings())
    user: Optional[User] = None
    if svc.enabled:
        # Best-effort: resolve the user from the header/query, but never 401.
        authz = request.headers.get("authorization")
        query_session = request.query_params.get("session")
        try:
            user = svc.require_user_from_request(authz, query_session)
        except InvalidTokenError:
            user = None
    return SessionOut(
        authenticated=svc.enabled and user is not None and user.user_id != "anonymous",
        auth_enabled=svc.enabled,
        user=_user_to_out(user) if (user and user.user_id != "anonymous") else None,
    ).model_dump()


@app.post("/api/auth/guest")
async def auth_guest(body: GuestLoginRequest) -> dict:
    """Create a guest session (allowed when AUTH_GUEST_ALLOWED=true)."""
    svc = get_auth_service(get_settings())
    if not svc.enabled:
        # Auth disabled — return a no-op success with the anonymous user so
        # the frontend can treat it uniformly.
        return LoginResponse(
            session="",
            user=_user_to_out(User("anonymous", "anonymous", "Anonymous")),
            expires_in=0,
        ).model_dump()
    try:
        user = svc.login_guest(body.display_name)
    except AuthError as exc:
        raise HTTPException(403, str(exc))
    token = svc.create_session(user)
    ttl = svc._guest_ttl
    return LoginResponse(session=token, user=_user_to_out(user),
                         expires_in=ttl).model_dump()


@app.post("/api/auth/github")
async def auth_github(body: GitHubLoginRequest) -> dict:
    """Sign in with a GitHub token (token verified against /user, not stored)."""
    svc = get_auth_service(get_settings())
    if not svc.enabled:
        raise HTTPException(400, "Authentication is disabled.")
    try:
        user = svc.login_github(body.github_token)
    except AuthError as exc:
        raise HTTPException(401, str(exc))
    token = svc.create_session(user)
    return LoginResponse(session=token, user=_user_to_out(user),
                         expires_in=svc._user_ttl).model_dump()


@app.post("/api/auth/logout")
async def auth_logout(user: User = Depends(current_user)) -> dict:
    """Stateless logout — the client discards the token."""
    svc = get_auth_service(get_settings())
    # The token isn't passed here (it's in the header); logout is stateless.
    svc.logout("")
    return {"success": True}


@app.get("/api/me")
async def api_me(user: User = Depends(current_user)) -> dict:
    """Return the current user's identity (protected endpoint example)."""
    return _user_to_out(user).model_dump()


# ── User settings (v0.7.0) ──────────────────────────────────────────
@app.get("/api/settings")
async def get_user_settings(user: User = Depends(current_user)) -> dict:
    """Return the current user's non-secret preferences.

    Falls back to server config defaults when no persisted preferences
    exist (so the first call works without any setup).
    """
    data = await get_settings_for_user(get_settings(), user)
    return SettingsOut(**data).model_dump()


@app.put("/api/settings")
async def update_user_settings(body: SettingsUpdate,
                               user: User = Depends(current_user)) -> dict:
    """Update the current user's preferences (partial update)."""
    updates = body.model_dump(exclude_none=True)
    data = await update_settings_for_user(get_settings(), user, updates)
    return SettingsOut(**data).model_dump()


# ── Workspace Manager routes (v0.7.0) ────────────────────────────────────────
@app.get("/api/workspaces")
async def api_list_workspaces(user: User = Depends(current_user)) -> dict:
    """List all workspaces under the configured root."""
    items = await list_workspaces(get_settings())
    return {"workspaces": [WorkspaceOut(**w).model_dump() for w in items]}


@app.get("/api/workspaces/recent")
async def api_recent_workspaces(user: User = Depends(current_user)) -> dict:
    """Return recently-accessed workspaces."""
    items = await recent_workspaces(get_settings(), limit=10)
    return {"workspaces": [WorkspaceOut(**w).model_dump() for w in items]}


@app.post("/api/workspaces")
async def api_create_workspace(body: WorkspaceCreateRequest,
                               user: User = Depends(current_user)) -> dict:
    """Create a new workspace (optionally cloning a GitHub repo)."""
    try:
        item = await create_workspace(get_settings(), body.name,
                                      repo=body.repo)
    except WorkspaceManagerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return WorkspaceOut(**item).model_dump()


@app.put("/api/workspaces")
async def api_rename_workspace(body: WorkspaceRenameRequest,
                               user: User = Depends(current_user)) -> dict:
    """Rename an existing workspace."""
    try:
        item = await rename_workspace(get_settings(), body.old_name,
                                      body.new_name)
    except WorkspaceManagerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return WorkspaceOut(**item).model_dump()


@app.delete("/api/workspaces/{name}")
async def api_delete_workspace(name: str,
                               user: User = Depends(current_user)) -> dict:
    """Delete a workspace directory (recursive)."""
    try:
        item = await delete_workspace(get_settings(), name)
    except WorkspaceManagerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return WorkspaceOut(**item).model_dump()


@app.post("/api/workspaces/switch")
async def api_switch_workspace(body: WorkspaceActionRequest,
                               user: User = Depends(current_user)) -> dict:
    """Mark a workspace as active (records recent access)."""
    try:
        item = await switch_workspace(get_settings(), body.name)
    except WorkspaceManagerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return WorkspaceOut(**item).model_dump()


# ── Dashboard (v0.7.0) ────────────────────────────────────────────────────────
def _task_row_to_item(row: dict) -> DashboardTaskItem:
    return DashboardTaskItem(
        task_id=str(row.get("task_id", "")),
        description=str(row.get("description", "")),
        status=normalize_status(row.get("status", "idle")),
        created_at=str(row.get("created_at", "")),
        branch=row.get("branch"),
    )


@app.get("/api/dashboard")
async def api_dashboard(user: User = Depends(current_user)) -> dict:
    """Aggregated dashboard: tasks, agent, workspace, git, provider, health."""
    settings = get_settings()
    # Tasks
    rows = await db_list_tasks()
    for r in rows:
        _runtime_status(r.get("task_id", ""), r)
    recent = [_task_row_to_item(r) for r in rows[:10]]
    active = [_task_row_to_item(r) for r in rows
              if r.get("status") in ("running", "planning", "queued")]
    # Agent status
    runtimes = list_runtimes()
    agent_status = "idle"
    if any(rt.status.value in ("running", "planning") for rt in runtimes):
        agent_status = "busy"
    # Workspace status
    ws_status: dict = {"count": 0, "default": None}
    try:
        items = await list_workspaces(settings)
        ws_status = {
            "count": len(items),
            "default": next((w["name"] for w in items if w["is_default"]), None),
            "names": [w["name"] for w in items],
        }
    except Exception:  # noqa: BLE001
        pass
    # Git status
    git_status: dict = {"configured": False}
    try:
        info = repo_info(settings)
        git_status = {
            "configured": True,
            "full_name": info.full_name,
            "default_branch": info.default_branch,
            "private": info.private,
        }
    except Exception:  # noqa: BLE001
        pass
    # Provider status
    provider_status: dict = {"configured": False}
    try:
        ps = provider_status(settings)
        provider_status = {
            "configured": bool(ps.get("configured")),
            "provider": ps.get("provider"),
            "model": ps.get("model"),
            "streaming_supported": ps.get("streaming_supported"),
        }
    except Exception:  # noqa: BLE001
        pass
    # System health
    try:
        sh = _system_health(settings)
        sys_components = [SystemHealthComponent(**c) for c in sh.get("components", [])]
    except Exception:  # noqa: BLE001
        sys_components = []
    out = DashboardOut(
        recent_tasks=recent,
        active_tasks=active,
        agent_status=agent_status,
        workspace_status=ws_status,
        git_status=git_status,
        provider_status=provider_status,
        system_health=sys_components,
        multi_agent_enabled=bool(getattr(settings, "multi_agent_enabled", False)),
    )
    return out.model_dump()


@app.get("/api/system/health")
async def api_system_health() -> dict:
    """Public system-health snapshot (no auth, no secrets)."""
    sh = _system_health(get_settings())
    return SystemHealthOut(
        status=sh.get("status", "unknown"),
        version=sh.get("version", "0.8.0"),
        environment=sh.get("environment", "development"),
        components=[SystemHealthComponent(**c) for c in sh.get("components", [])],
        startup_checks=sh.get("components", []),
    ).model_dump()


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


# v0.8.0 — Eagerly (re)initialise the scheduler + worker singletons at module
# load so the app works even if the startup hook hasn't run yet (e.g. under
# test reload). When SCHEDULER_ENABLED=false (default) both stay None and the
# original fire-and-forget start_task() path is preserved.
def _init_scheduler_from_settings() -> None:
    _s = get_settings()
    if getattr(_s, "scheduler_enabled", False):
        sched = init_scheduler(
            default_priority=getattr(_s, "scheduler_default_priority", 5),
            default_retries=getattr(_s, "scheduler_default_retries", 1),
        )
        # Start the background worker so queued tasks get drained automatically.
        init_worker(
            sched,
            start_task,
            max_concurrency=getattr(_s, "worker_max_concurrency", 2),
            poll_interval=getattr(_s, "worker_poll_interval_seconds", 1.0),
            autostart=True,
        )
    else:
        reset_worker()
        reset_scheduler()


_init_scheduler_from_settings()


@app.on_event("startup")
async def _startup() -> None:
    await init_db()
    s = get_settings()
    env = getattr(s, "app_env", "development")
    # Production-safety warnings (non-blocking).
    if env == "production":
        if getattr(s, "debug", False):
            log.warning("PRODUCTION SAFETY: DEBUG=true is not recommended in production.")
        if not getattr(s, "auth_enabled", False):
            log.warning("PRODUCTION SAFETY: AUTH_ENABLED=false in production — dashboard is unprotected.")
        if not getattr(s, "auth_secret", ""):
            log.warning("PRODUCTION SAFETY: AUTH_SECRET is empty — sessions cannot be signed securely.")
    # Run release-prep startup checks (v0.7.0).
    try:
        from release_checks import run_startup_checks
        checks = run_startup_checks(s)
        for chk in checks:
            if chk.get("status") == "warn":
                log.warning("startup check: %s — %s", chk.get("name"), chk.get("detail"))
            else:
                log.info("startup check: %s — %s", chk.get("name"), chk.get("detail"))
    except Exception as exc:  # noqa: BLE001 — never block startup on checks
        log.warning("startup checks skipped: %s", exc)
    log.info("PK Ninja Agent v1.0.1 started (env=%s). DB at %s",
             env, s.db_path)
    # v0.8.0 — Autonomous Execution Engine: initialise the scheduler + worker
    # when opted in. When disabled (default), the original fire-and-forget
    # start_task() path is preserved for full backward compatibility.
    if getattr(s, "scheduler_enabled", False):
        sched = init_scheduler(
            default_priority=getattr(s, "scheduler_default_priority", 5),
            default_retries=getattr(s, "scheduler_default_retries", 1),
        )
        init_worker(
            sched,
            start_task,
            max_concurrency=getattr(s, "worker_max_concurrency", 2),
            poll_interval=getattr(s, "worker_poll_interval_seconds", 1.0),
            autostart=True,
        )
        log.info("Autonomous scheduler+worker ENABLED (priority=%s retries=%s concurrency=%s)",
                 getattr(s, "scheduler_default_priority", 5),
                 getattr(s, "scheduler_default_retries", 1),
                 getattr(s, "worker_max_concurrency", 2))
    else:
        reset_worker()
        reset_scheduler()


# ── Health ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "1.0.1"}


# ── Non-secret config (for the frontend) ────────────────────────────────
@app.get("/api/config")
async def api_config() -> dict:
    """Return a non-secret summary of the active AI provider + repo config."""
    # Read settings fresh so runtime env-var changes are reflected (tests and
    # hot-reload), rather than the module-level snapshot from import time.
    fresh = get_settings()
    ps = provider_status(fresh)
    # v0.6.0: also include a compact, secret-free provider manager summary
    # (provider names, enabled/health status, capability flags only). The full
    # provider info (including requires_api_key metadata) is served by the
    # dedicated /api/providers endpoint. We deliberately keep /api/config
    # minimal so the existing secret-leak guard continues to pass.
    providers_summary = None
    manager_enabled = getattr(fresh, "provider_manager_enabled", False)
    try:
        from providers import get_manager
        mgr = get_manager(fresh)
        mgr.set_active(fresh.ai_provider or "local")
        providers_summary = {
            name: {
                "name": info.name,
                "display_name": info.display_name,
                "enabled": info.enabled,
                "available": info.is_available,
                "health_status": info.health.status.value,
                "capability": info.capability.to_dict(),
            }
            for name, info in mgr.all_info().items()
        }
    except Exception:  # noqa: BLE001 — never let provider UI break /api/config
        providers_summary = None
    return ConfigOut(
        provider=ps["provider"],
        model=ps["model"],
        configured=ps["configured"],
        streaming_supported=ps["streaming_supported"],
        repository_configured=bool(fresh.github_repo_full()),
        provider_manager_enabled=manager_enabled,
        providers=providers_summary,
    ).model_dump()


# ── Provider management (v0.6.0) ────────────────────────────────────────────
def _provider_manager():
    """Return the shared ProviderManager, configured from current settings."""
    from providers import get_manager
    fresh = get_settings()
    mgr = get_manager(fresh)
    # Apply explicit enable list / fallback order if configured.
    enabled = fresh.provider_enabled_names()
    if enabled:
        for name in list(mgr.all_info().keys()):
            if name not in enabled:
                mgr.disable(name)
            else:
                mgr.enable(name)
    fallback = fresh.provider_fallback_names()
    if fallback:
        mgr.set_fallback_chain(fallback)
    # Ensure active matches the configured preferred provider.
    mgr.set_active(fresh.ai_provider or "local")
    return mgr


def _provider_info_to_out(info) -> ProviderInfoOut:
    cap = info.capability
    return ProviderInfoOut(
        name=info.name,
        display_name=info.display_name,
        description=info.description,
        capability=ProviderCapabilityOut(
            streaming=cap.streaming,
            tool_calling=cap.tool_calling,
            code_editing=cap.code_editing,
            context_window=cap.context_window or None,
            max_output=cap.max_output or None,
        ),
        requires_api_key=info.requires_api_key,
        enabled=info.enabled,
        configurable=info.configurable,
        is_available=info.is_available,
        health=ProviderHealthOut(**info.health.to_dict()),
        fallback_for=list(info.fallback_for),
    )


@app.get("/api/providers")
async def api_providers() -> dict:
    """Return the full provider manager status (no secrets)."""
    mgr = _provider_manager()
    status = mgr.status()
    active_cap = status.get("active_capability")
    active_health = status.get("active_health")
    providers_out = {
        name: _provider_info_to_out(info)
        for name, info in mgr.all_info().items()
    }
    return ProviderManagerStatusOut(
        active=status.get("active"),
        available=status.get("available", []),
        fallback_chain=status.get("fallback_chain", []),
        active_capability=ProviderCapabilityOut(**active_cap) if active_cap else None,
        active_health=ProviderHealthOut(**active_health) if active_health else None,
        providers=providers_out,
    ).model_dump()


@app.post("/api/providers/enable")
async def api_provider_enable(req: ProviderActionRequest) -> dict:
    """Enable a provider by name (server-side; no secrets returned)."""
    mgr = _provider_manager()
    ok = mgr.enable(req.name)
    return {"ok": ok, "name": req.name, "enabled": ok}


@app.post("/api/providers/disable")
async def api_provider_disable(req: ProviderActionRequest) -> dict:
    """Disable a provider by name."""
    mgr = _provider_manager()
    ok = mgr.disable(req.name)
    return {"ok": ok, "name": req.name, "disabled": ok}


@app.post("/api/providers/active")
async def api_provider_set_active(req: ProviderActionRequest) -> dict:
    """Select the active provider by name."""
    mgr = _provider_manager()
    ok = mgr.set_active(req.name)
    return {"ok": ok, "active": mgr.active_name}


@app.get("/api/providers/{name}/health")
async def api_provider_health(name: str) -> dict:
    """Run a lightweight health probe on a provider and return its health."""
    mgr = _provider_manager()
    health = mgr.health_check(name)
    return {"name": name, "health": health}


@app.get("/api/providers/{name}/capabilities")
async def api_provider_capabilities(name: str) -> dict:
    """Return the declared capabilities of a provider."""
    mgr = _provider_manager()
    cap = mgr.capability(name)
    if cap is None:
        raise HTTPException(status_code=404, detail=f"unknown provider: {name}")
    return {"name": name, "capability": cap.to_dict()}


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
    # v0.8.0: when the autonomous scheduler is enabled, enqueue the task so the
    # background worker picks it up by priority. Otherwise (default) keep the
    # original fire-and-forget behaviour for full backward compatibility.
    sched = get_scheduler()
    if sched is not None and getattr(fresh, "scheduler_enabled", False):
        item = sched.enqueue(
            task_id=task_id,
            description=body.description,
            repo_full=repo,
            enqueued_at=_dt.datetime.utcnow().timestamp(),
        )
        return {"task_id": task_id, "status": item.status.value,
                "repository": repo, "queued": True}
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
    # v0.8.0: when security hardening is enabled, run the enhanced pipeline
    # (extra blocklist + destructive-arg containment) before execution.
    s = get_settings()
    if s.security_hardening_enabled:
        allowed, reason, _issues = full_command_check(
            body.command, workspace_root=rt.workspace.root
        )
        if not allowed:
            return JSONResponse(
                status_code=400,
                content={"command": body.command, "error": reason,
                         "success": False, "returncode": 126},
            )
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


# ── Autonomous Execution Engine — Task Scheduler (v0.8.0) ──────────────────

def _scheduler_or_404() -> TaskScheduler:
    """Return the active scheduler or raise 404 when the feature is disabled."""
    sched = get_scheduler()
    if sched is None:
        raise HTTPException(
            404,
            "Task scheduler is disabled. Set SCHEDULER_ENABLED=true to enable "
            "the autonomous execution engine.",
        )
    return sched


@app.get("/api/queue")
async def api_queue_list() -> QueueListOut:
    fresh = get_settings()
    sched = get_scheduler()
    if sched is None:
        return QueueListOut(
            enabled=bool(getattr(fresh, "scheduler_enabled", False)),
            queue=[], queue_length=0, running_count=0,
            max_concurrency=getattr(fresh, "worker_max_concurrency", 2),
        )
    items = sched.list_items()
    return QueueListOut(
        enabled=True,
        queue=[TaskQueueItem(**i.to_dict()) for i in items],
        queue_length=sched.queue_length(),
        running_count=sched.running_count(),
        max_concurrency=getattr(fresh, "worker_max_concurrency", 2),
    )


@app.post("/api/queue/pause")
async def api_queue_pause(body: QueueActionRequest) -> TaskQueueItem:
    sched = _scheduler_or_404()
    item = sched.pause(body.task_id)
    if item is None:
        raise HTTPException(404, "Task not found in queue")
    return TaskQueueItem(**item.to_dict())


@app.post("/api/queue/resume")
async def api_queue_resume(body: QueueActionRequest) -> TaskQueueItem:
    sched = _scheduler_or_404()
    item = sched.resume(body.task_id)
    if item is None:
        raise HTTPException(404, "Task not found in queue")
    return TaskQueueItem(**item.to_dict())


@app.post("/api/queue/cancel")
async def api_queue_cancel(body: QueueActionRequest) -> TaskQueueItem:
    sched = _scheduler_or_404()
    item = sched.cancel(body.task_id)
    if item is None:
        raise HTTPException(404, "Task not found in queue")
    # Best-effort: also cancel the live runtime if the task is already running.
    try:
        cancel_task(body.task_id)
    except Exception:  # noqa: BLE001
        pass
    return TaskQueueItem(**item.to_dict())


@app.post("/api/queue/retry")
async def api_queue_retry(body: RetryRequest) -> TaskQueueItem:
    sched = _scheduler_or_404()
    item = sched.retry(body.task_id)
    if item is None:
        raise HTTPException(404, "Task not found in queue")
    if body.priority is not None:
        item = sched.reorder(body.task_id, body.priority)
    return TaskQueueItem(**item.to_dict())


@app.post("/api/queue/reorder")
async def api_queue_reorder(body: ReorderRequest) -> TaskQueueItem:
    sched = _scheduler_or_404()
    item = sched.reorder(body.task_id, body.priority)
    if item is None:
        raise HTTPException(404, "Task not found in queue")
    return TaskQueueItem(**item.to_dict())


@app.get("/api/queue/{task_id}")
async def api_queue_get(task_id: str) -> TaskQueueItem:
    sched = _scheduler_or_404()
    item = sched.get(task_id)
    if item is None:
        raise HTTPException(404, "Task not found in queue")
    return TaskQueueItem(**item.to_dict())


@app.get("/api/worker")
async def api_worker_status() -> dict:
    """Report background worker status (opt-in; reports disabled when off)."""
    fresh = get_settings()
    w = get_worker()
    if w is None:
        return {"enabled": False, "running": False, "active": 0,
                "max_concurrency": getattr(fresh, "worker_max_concurrency", 2),
                "completed": 0, "failed": 0}
    return {"enabled": True, "running": w.is_running, "active": w.active_count,
            "max_concurrency": getattr(fresh, "worker_max_concurrency", 2),
            "completed": w.completed_count, "failed": w.failed_count}


# ── Autonomous Execution Engine — Workspace Sessions (v0.8.0) ──────────────

@app.get("/api/sessions")
async def api_sessions_list(
    repo_full: Optional[str] = None,
    state: Optional[str] = None,
) -> WorkspaceSessionListOut:
    fresh = get_settings()
    items = await list_sessions(
        Path(fresh.db_path), repo_full=repo_full, state=state,
    )
    return WorkspaceSessionListOut(
        sessions=[WorkspaceSessionOut(**i) for i in items], count=len(items),
    )


@app.post("/api/sessions")
async def api_sessions_create(body: SessionCreateRequest) -> WorkspaceSessionOut:
    fresh = get_settings()
    # If an active session for this repo already exists, reuse it (touch + return)
    existing = await find_active_for_repo(Path(fresh.db_path), body.repo_full)
    if existing is not None:
        updated = await touch_session(
            Path(fresh.db_path), existing["session_id"],
            branch=body.branch, task_id=body.task_id,
            description=body.description,
        )
        return WorkspaceSessionOut(**(updated or existing))
    created = await create_session(
        Path(fresh.db_path),
        repo_full=body.repo_full, workspace=body.workspace,
        branch=body.branch, task_id=body.task_id, description=body.description,
    )
    return WorkspaceSessionOut(**created)


@app.get("/api/sessions/{session_id}")
async def api_sessions_get(session_id: str) -> WorkspaceSessionOut:
    fresh = get_settings()
    sess = await get_session(Path(fresh.db_path), session_id)
    if sess is None:
        raise HTTPException(404, "Session not found")
    return WorkspaceSessionOut(**sess)


@app.post("/api/sessions/{session_id}/restore")
async def api_sessions_restore(session_id: str) -> WorkspaceSessionOut:
    """Mark a closed/interrupted session active again (reuse workspace)."""
    fresh = get_settings()
    updated = await touch_session(
        Path(fresh.db_path), session_id, state="active",
    )
    if updated is None:
        raise HTTPException(404, "Session not found")
    return WorkspaceSessionOut(**updated)


@app.post("/api/sessions/{session_id}/close")
async def api_sessions_close(session_id: str) -> WorkspaceSessionOut:
    fresh = get_settings()
    closed = await _close_session(Path(fresh.db_path), session_id)
    if closed is None:
        raise HTTPException(404, "Session not found")
    return WorkspaceSessionOut(**closed)


@app.delete("/api/sessions/{session_id}")
async def api_sessions_delete(session_id: str) -> dict:
    fresh = get_settings()
    ok = await _delete_session(Path(fresh.db_path), session_id)
    if not ok:
        raise HTTPException(404, "Session not found")
    return {"deleted": True, "session_id": session_id}


# ── Autonomous Execution Engine — Execution Monitor (v0.8.0) ───────────────

@app.get("/api/monitor")
async def api_monitor() -> dict:
    """Live execution monitor: system CPU/memory + per-task metrics.

    Always returns 200 even when psutil is unavailable (metrics report
    ``unavailable`` in that case).
    """
    runtimes = list_runtimes()
    rows = await db_list_tasks()
    rows_by_id = {r["task_id"]: r for r in rows}
    # gather recent events for each live runtime (bounded)
    events_by_id: dict = {}
    for rt in runtimes:
        try:
            events_by_id[rt.task_id] = await db_list_events(rt.task_id)
        except Exception:  # noqa: BLE001
            events_by_id[rt.task_id] = []
    return monitor_snapshot(runtimes, rows_by_id, events_by_id)


@app.get("/api/monitor/system")
async def api_monitor_system() -> dict:
    """System-wide resource metrics only (lightweight poll)."""
    return system_metrics()


# ── Autonomous Execution Engine — Recovery System (v0.8.0) ─────────────────

@app.get("/api/recovery")
async def api_recovery_detect() -> dict:
    """Detect interrupted tasks (live status but no in-memory runtime)."""
    interrupted = await detect_interrupted(db_list_tasks, lambda tid: get_runtime(tid) is not None)
    fresh = get_settings()
    summary = recovery_summary(interrupted)
    summary["auto_resume"] = bool(getattr(fresh, "recovery_auto_resume", False))
    summary["interrupted"] = interrupted
    return summary


@app.post("/api/recovery/mark-failed")
async def api_recovery_mark_failed(body: RecoveryActionRequest) -> dict:
    """Mark an interrupted task as failed (terminal). Preserves logs."""
    rt = get_runtime(body.task_id)
    if rt is not None:
        raise HTTPException(409, "Task still has a live runtime; cancel it first.")
    row = await db_get_task(body.task_id)
    if not row:
        raise HTTPException(404, "Task not found")

    async def _update(tid: str, status: str) -> None:
        await db_update_task_status(tid, TaskStatus(status), branch=None)

    await mark_task_failed(_update, body.task_id,
                           reason=body.reason or "interrupted")
    return {"task_id": body.task_id, "status": "failed",
            "reason": body.reason or "interrupted"}


@app.post("/api/recovery/resume")
async def api_recovery_resume(body: RecoveryActionRequest) -> dict:
    """Re-run an interrupted task from scratch (fresh execution, reused context)."""
    rt = get_runtime(body.task_id)
    if rt is not None:
        raise HTTPException(409, "Task already has a live runtime.")
    row = await db_get_task(body.task_id)
    if not row:
        raise HTTPException(404, "Task not found")
    repo = row.get("repo") or ""
    desc = row.get("description") or ""
    resume_task(body.task_id, desc, repo, start_task)
    return {"task_id": body.task_id, "status": "running", "resumed": True}


# -- v0.8.0 Job History ------------------------------------------------------
@app.get("/api/history")
async def api_history(
    repo: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    include_events: int = 0,
) -> dict:
    """Searchable, filterable job history over the tasks + events tables."""
    result = await query_history(
        repo=repo,
        status=status,
        search=search,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
        include_events=include_events,
    )
    return result


@app.get("/api/history/{task_id}")
async def api_history_detail(task_id: str) -> dict:
    """Full detail for one historical job including its event log."""
    detail = await get_job_detail(task_id)
    if not detail:
        raise HTTPException(404, "Task not found in history")
    return detail


@app.get("/api/history-stats")
async def api_history_stats() -> dict:
    """Aggregate statistics over the entire task history."""
    return await history_stats()


# -- v0.8.0 Export -----------------------------------------------------------
@app.get("/api/export/{task_id}")
async def api_export_task(task_id: str, format: str = "json") -> Response:
    """Export a single task's logs or report.

    ``format``: ``json`` (structured log), ``text`` (line log),
    ``markdown`` (executive summary report).
    """
    detail = await get_job_detail(task_id)
    if not detail:
        raise HTTPException(404, "Task not found in history")
    events = detail.get("events", [])
    fmt = (format or "json").lower()
    if fmt == "text":
        content = export_logs_text(detail, events)
        media = "text/plain"
    elif fmt == "markdown":
        content = export_report_markdown(detail, events)
        media = "text/markdown"
    else:
        content = export_logs_json(detail, events)
        media = "application/json"
    return Response(content=content, media_type=media)


@app.get("/api/export-history")
async def api_export_history(
    format: str = "json",
    repo: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
) -> Response:
    """Export the filtered task history as JSON or CSV."""
    result = await query_history(
        repo=repo, status=status, search=search,
        date_from=date_from, date_to=date_to,
        limit=limit, offset=0,
    )
    fmt = (format or "json").lower()
    if fmt == "csv":
        content = export_history_csv(result["items"])
        media = "text/csv"
    else:
        content = export_history_json(result["items"], result["count"])
        media = "application/json"
    return Response(content=content, media_type=media)


# ── Frontend ────────────────────────────────────────────────────────────

# v0.8.0 Phase 9: Security hardening endpoints


@app.get("/api/security/workspace/{name}")
async def api_validate_workspace(name: str) -> WorkspaceValidationOut:
    """Validate a named workspace for safety (symlinks, permissions, containment).

    Walks the workspace directory and checks that no symlinks escape the
    workspace root, no directories are world-writable, and the workspace
    is contained within the configured ``workspace_root``.
    """
    s = get_settings()
    # Resolve the workspace path the same way workspace_manager does.
    from workspace_manager import _path as _wm_path
    try:
        ws_dir = _wm_path(s, name)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ws_dir.exists():
        raise HTTPException(status_code=404, detail=f"Workspace '{name}' not found.")
    try:
        result = validate_workspace(
            ws_dir,
            workspace_root=str(s.workspace_root),
            max_files=s.security_max_workspace_files,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return WorkspaceValidationOut(
        valid=result.valid,
        root=result.root,
        issues=result.issues,
        checked_files=result.checked_files,
        checked_dirs=result.checked_dirs,
        symlinks=result.symlinks,
    )


@app.post("/api/security/check-command")
async def api_check_command(req: CommandCheckRequest) -> CommandCheckOut:
    """Dry-run validate a command against the full security pipeline.

    Runs the extra blocklist, existing terminal validation, and destructive-
    argument containment.  Does **not** execute the command.
    """
    s = get_settings()
    ws_root = None
    if s.security_hardening_enabled:
        ws_root = Path(s.workspace_root).resolve()
    allowed, reason, issues = full_command_check(
        req.command, workspace_root=ws_root
    )
    return CommandCheckOut(allowed=allowed, reason=reason, issues=issues)


@app.post("/api/security/sensitive-path")
async def api_sensitive_path(req: SensitivePathRequest) -> SensitivePathOut:
    """Check whether a path looks like a sensitive file (secrets/keys)."""
    return SensitivePathOut(
        sensitive=is_sensitive_path(req.path), path=req.path
    )


@app.get("/api/security/status")
async def api_security_status() -> dict:
    """Return the current security configuration (no secrets)."""
    import security as _sec
    s = get_settings()
    return {
        "security_hardening_enabled": s.security_hardening_enabled,
        "max_workspace_files": s.security_max_workspace_files,
        "command_timeout_seconds": s.command_timeout_seconds,
        "extra_blocked_patterns": len(_sec.EXTRA_BLOCKED_PATTERNS),
        "sensitive_patterns": len(_sec.SENSITIVE_PATTERNS),
        "destructive_programs": sorted(_sec.DESTRUCTIVE_PROGRAMS),
    }


_frontend_dir = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(_frontend_dir / "index.html"))


app.mount("/static", StaticFiles(directory=str(_frontend_dir)), name="static")
