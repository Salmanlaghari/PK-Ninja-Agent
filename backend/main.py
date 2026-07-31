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

from fastapi import (Depends, FastAPI, HTTPException, Request, WebSocket,
                     WebSocketDisconnect)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import (BUS, TaskRuntime, cancel_task, get_runtime,
                   list_runtimes, new_task_id, start_task)
from ai_provider import provider_status
from auth import (AuthError, AuthService, InvalidTokenError, User,
                  get_auth_service, reset_auth_service)
from config import Settings, get_settings
from github import GitHubError, create_pull_request, prepare_pull_request, repo_info
from settings_store import (get_settings_for_user, update_settings_for_user)
from release_checks import system_health as _system_health
from workspace_manager import (WorkspaceManagerError, create_workspace,
                               delete_workspace, list_workspaces,
                               recent_workspaces, rename_workspace,
                               switch_workspace)
from models import (ConfigOut, DashboardOut, DashboardTaskItem, DiffOut,
                    EventOut, EventType, GitHubLoginRequest, GitBranchRequest,
                    GitCommitRequest, GitPushRequest, GuestLoginRequest,
                    LoginResponse, PRPrepareRequest, ProviderActionRequest,
                    ProviderCapabilityOut, ProviderHealthOut,
                    ProviderInfoOut, ProviderManagerStatusOut, SessionOut,
                    SettingsOut, SettingsUpdate, SystemHealthComponent,
                    SystemHealthOut, TaskCreate, TaskStatus, TaskSummary,
                    UserOut, WorkspaceActionRequest, WorkspaceCreateRequest,
                    WorkspaceOut, WorkspaceRenameRequest, normalize_status)
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
    # Ensure schema exists on every connection (idempotent + defensive so the
    # app works even if the startup hook hasn't run yet, e.g. under reload).
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

app = FastAPI(title="PK Ninja Agent", version="0.7.0")

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
        version=sh.get("version", "0.7.0"),
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
    log.info("PK Ninja Agent v0.7.0 started (env=%s). DB at %s",
             env, s.db_path)


# ── Health ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "0.7.0"}


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
