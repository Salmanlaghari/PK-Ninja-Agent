# Changelog

All notable changes to PK Ninja Agent are documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.0] — Beta: Product & Deployment Phase

The release that transforms PK Ninja Agent from a development prototype into a real beta product. Every new feature is opt-in and backward compatible — with all new flags at their defaults, the app behaves exactly as v0.6.0.

### Added

#### Authentication (Phase 1)
- Modular authentication system (`backend/auth.py`) with GitHub token login, guest mode, stateless HMAC-signed sessions, and logout.
- `AUTH_ENABLED` (default `false`), `AUTH_GUEST_ALLOWED` (default `true`), `AUTH_GITHUB_ENABLED` (default `false`), `AUTH_SECRET`, `AUTH_GUEST_TTL_SECONDS`, `AUTH_USER_TTL_SECONDS` configuration.
- New endpoints: `GET /api/auth/status` (public), `POST /api/auth/guest`, `POST /api/auth/github`, `POST /api/auth/logout`, `GET /api/me`.
- Frontend login overlay, user menu, and a transparent `window.fetch` wrapper that attaches `Authorization: Bearer <token>` headers from `sessionStorage`.
- `current_user` FastAPI dependency returns an anonymous placeholder when auth is disabled, so every existing endpoint works unchanged.

#### Settings (Phase 2)
- Persistent per-user settings store (`backend/settings_store.py`) backed by a SQLite `user_settings` table.
- Preferences: theme, AI provider, default workspace, terminal preferences, git preferences, auto-save, auto-commit, notifications.
- New endpoints: `GET /api/settings`, `PUT /api/settings`.
- Frontend settings modal with Save and Reset-to-defaults.

#### Workspace Manager (Phase 3)
- Sandboxed workspace manager (`backend/workspace_manager.py`) with path-traversal protection and SQLite recent-workspaces tracking.
- New endpoints: `GET /api/workspaces`, `GET /api/workspaces/recent`, `POST /api/workspaces`, `PUT /api/workspaces`, `DELETE /api/workspaces/{name}`, `POST /api/workspaces/switch`.
- Frontend Workspaces modal with create/list/recent/switch/rename/delete.

#### Provider Manager UI (Phase 4)
- Dedicated provider management modal reusing v0.6.0 `/api/providers` endpoints (no backend changes).
- Summary (active, health, manager status), provider cards with capability tags, detail view with enable/disable, set-active, and health-check actions.

#### Dashboard (Phase 5)
- Aggregated dashboard endpoint `GET /api/dashboard` (recent/active tasks, agent status, workspace/git/provider status, system health, multi-agent flag).
- Public system-health endpoint `GET /api/system/health` (status, version, environment, components — no secrets).
- Frontend Dashboard modal.

#### Release Preparation (Phase 6)
- 404 handler: JSON for `/api/*` routes, SPA fallback (`index.html`) for non-API routes.
- 500 handler: server-side logging, generic message in production (no stack-trace leak), detail in development.
- Frontend loading banner and global error toast with a `UI` helper module.
- Startup checks (`backend/release_checks.py`): non-blocking checks for Python version, workspace root, database, GitHub, AI provider, and production safety.
- Production-safety warnings when `APP_ENV=production` (DEBUG, AUTH_ENABLED, AUTH_SECRET).
- `APP_ENV`, `DEBUG`, `SITE_URL` configuration.

#### Documentation
- README.md updated with v0.7.0 sections and all new environment variables.
- CHANGELOG.md (this file), ROADMAP.md, CONTRIBUTING.md created.

### Tests
- Test count grew from **231** (v0.6.0) to **314**.
- New test files: `tests/test_auth.py` (23), `tests/test_settings.py` (9), `tests/test_workspace_manager.py` (25), `tests/test_dashboard.py` (12), `tests/test_release_prep.py` (14).
- All 231 pre-existing tests pass unchanged.

### Backward Compatibility
- `AUTH_ENABLED=false` (default) → no auth required, anonymous placeholder user.
- `PROVIDER_MANAGER_ENABLED=false` (default) → original `get_provider()` factory used.
- `MULTI_AGENT_ENABLED=false` (default) → single-agent behavior.
- All existing endpoints, models, and frontend panels retained.

---

## [0.6.0] — AI Provider Plugin System

A modular, opt-in provider plugin system layered on top of the existing `backend/ai_provider.py` architecture. Adds dynamic provider loading, capability detection, health monitoring, and automatic fallback.

### Added
- `providers/` package with `ProviderProtocol`, `ProviderCapability`, `ProviderHealth`, `ProviderInfo`, and `ProviderManager`.
- Built-in adapters: `LocalAdapter`, `OpenAIAdapter`, `GeminiAdapter`, `MockProvider`.
- `PROVIDER_MANAGER_ENABLED`, `PROVIDER_ENABLED`, `PROVIDER_FALLBACK_ORDER`, `PROVIDER_HEALTH_INTERVAL` configuration.
- New endpoints: `/api/providers`, `/api/providers/enable`, `/api/providers/disable`, `/api/providers/active`, `/api/providers/{name}/health`, `/api/providers/{name}/capabilities`.
- Frontend provider management sidebar panel.
- Test count: 179 → 231.

---

## [0.5.0] — Multi-Agent Architecture

A multi-agent coordinator architecture that orchestrates specialized agents (planner, executor, reviewer) for complex tasks. Fully opt-in via `MULTI_AGENT_ENABLED`.

### Added
- `agents/` package with `Coordinator`, `PlannerAgent`, `ExecutorAgent`, `ReviewerAgent`.
- `MULTI_AGENT_ENABLED` configuration (default `false`).
- Test count: 161 → 179.

---

## [0.4.0] — Conversation Memory & Context Engine

Added persistent conversation memory and a context engine that builds task/repo context for the agent loop.

### Added
- `backend/context_engine.py`, `backend/conversation_memory.py`.
- Test count grew with memory and context tests.

---

## [0.3.0] — V3 Modern IDE Coding Workspace

The original web-based IDE coding workspace with task queue, repository explorer, git controls, live terminal, and diff viewer.

### Added
- FastAPI backend with SQLite persistence, SSE/WebSocket streaming.
- Vanilla JS/CSS/HTML frontend served at `/static/`.
- Real terminal execution in sandboxed workspaces.
- GitHub integration (clone, branch, commit, push, PR preparation).
