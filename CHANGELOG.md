# Changelog

All notable changes to PK Ninja Agent are documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] — Multi-Provider AI System

Production-grade multi-provider AI system with Xiaomi MiMo, per-provider API keys, provider validation, and enhanced Settings UI.

### Added
- **Xiaomi MiMo provider** (`providers/xiaomi_provider.py`) — new built-in adapter for Xiaomi's OpenAI-compatible endpoint at `https://api.xiaomimimo.com/v1`. Registered as a first-class builtin with `ProviderCapability(streaming=True, tool_calling=True, code_editing=True)`. Supports `MIMO_API_KEY` env var with fallback to `AI_API_KEY`.
- **Per-provider API key storage** — expanded `SECRET_KINDS` in `backend/secret_store.py` with `jules_api_key`, `gemini_api_key`, `mimo_api_key`, `openai_api_key` for granular per-provider key management.
- **Provider key management endpoints** (`backend/main.py`):
  - `GET /api/providers/{name}/key-status` — check if a provider has a stored key
  - `POST /api/providers/{name}/key` — save an API key for a specific provider
  - `DELETE /api/providers/{name}/key` — remove a provider's stored key
  - `POST /api/providers/{name}/validate` — lightweight health-check validation
  - `POST /api/providers/{name}/test` — end-to-end connectivity test
- **Enhanced Settings UI** (`frontend/index.html`, `frontend/app.js`, `frontend/style.css`) — per-provider API key configuration with show/hide toggle, validate button, test connection button, status indicator, last validation time, and clear error messages.
- **Per-provider key resolution** (`backend/user_settings.py`) — priority: per-provider stored key → generic `ai_api_key` → server env var → built-in default.
- **Integration tests**:
  - `tests/test_xiaomi_provider.py` — 9 tests (init, manager integration, factory fallback)
  - `tests/test_provider_validation.py` — 4 tests (validate, test connection)
  - `tests/test_multi_provider.py` — 6 tests (all providers, capabilities, fallback, security)
  - Updated `tests/test_provider_capabilities.py` — added Xiaomi capability tests
- **`MIMO_API_KEY`** env var in `backend/config.py` and `.env.example`.

### Fixed
- **Jules API critical fix** — corrected Create Session request body from `userInput` to `prompt` (per official Jules REST API spec).
- **Jules API fix** — corrected `sendMessage` body from `{text}` to `{prompt}`.
- **Jules API fix** — corrected repository context from `gitRepository`/`targetBranch` to `sourceContext.githubRepoContext` structure.
- **Jules activity parsing** — updated `_collect_agent_text` and `_collect_edits` to handle official API structure where event types are direct fields on activity objects, with backward-compatible fallback for legacy nested events.
- **Test fix** — `test_get_instance_openai_without_key_returns_none` now properly clears `BUILTIN_AI_API_KEY`.

### Changed
- `_DEFAULT_BASES` in `backend/ai_provider.py` now includes `xiaomi` → `https://api.xiaomimimo.com/v1`.
- `effective_api_key()` in `backend/config.py` now checks `mimo_api_key` in the resolution chain.
- `providers/__init__.py` version bumped to `0.8.0`.

---

## [Unreleased] — Vercel Serverless Deployment Configuration

### Added
- **`pyproject.toml`** — configures the Vercel modern Python runtime entrypoint as `backend.main:app`, telling Vercel's builder to load the FastAPI `app` instance from `backend/main.py`. Also declares a `[project]` section with runtime dependencies so Vercel can resolve the Python environment reliably.
- **`.python-version`** — pins Python 3.12 (Vercel's default supported version) for build-time and runtime stability.
- **`backend/__init__.py`** — makes `backend` a proper importable Python package so the `backend.main:app` entrypoint resolves correctly on Vercel's serverless runtime.
- **`DEPLOYMENT.md`** — comprehensive deployment guide now covers Vercel (serverless, zero-config) alongside the existing Docker / Fly.io / Render options, including the critical `/tmp` writable-directory configuration for SQLite (`DATABASE_PATH=/tmp/pk_ninja.db`) and workspaces (`WORKSPACE_ROOT=/tmp/workspaces`).

### Changed
- **`README.md`** — added a "Vercel Deployment" section referencing `DEPLOYMENT.md`.
- **`.env.example`** — documented the full set of supported AI providers (`local`, `openai`, `gemini`, `anthropic`, `jules`).

### Fixed
- **Vercel build failure** — resolved by adding `backend/__init__.py` (the `backend.main:app` entrypoint requires `backend` to be an importable package) and by declaring runtime dependencies in `pyproject.toml`'s `[project]` section for reliable dependency resolution during the Vercel build.

---

## [1.1.0] — Jules Provider Integration

First-class integration of the official Jules asynchronous coding-agent REST API as a production AI provider, registered alongside the existing local, Gemini, and mock providers.

### Added
- **JulesProvider** (`backend/ai_provider.py`) — full rewrite of the previous Jules stub. Implements the official Jules async REST API (`https://jules.googleapis.com/v1alpha`) with `x-goog-api-key` header authentication (not Bearer). Implements the complete session lifecycle: create session → poll to terminal → auto-approve plan → collect activities/artifacts → parse unidiff edits. Includes HTTP retry with exponential backoff (transient 429/5xx and network errors only), timeout-bounded polling, structured diagnostics counters, and a synchronous bridge (`generate`, `plan`, `edit`, `analyze_error`, `stream_chat`) so Jules works with PK Ninja Agent's synchronous provider protocol. Streaming is emulated via ~12-word chunked delivery because the Jules API exposes no SSE endpoint.
- **JulesAdapter** (`providers/jules_provider.py`) — provider-plugin adapter following the established GeminiAdapter pattern. Registered in the Provider Manager as a first-class builtin with advertised `ProviderCapability(streaming=True, tool_calling=True, code_editing=True)`. Delegates `plan`/`edit`/`analyze_error`/`stream_chat` to the inner `JulesProvider` and implements `chat`/`review`/`summarize` via `generate`.
- **Jules configuration** (`backend/config.py`) — new settings: `jules_api_key` (`JULES_API_KEY`), `jules_base_url` (default `https://jules.googleapis.com/v1alpha`), `jules_poll_interval_seconds` (3.0), `jules_poll_timeout_seconds` (600), `jules_max_retries` (3). Added `Settings.effective_jules_key()` which resolves `jules_api_key → ai_api_key → gemini_api_key`.
- **`.env.example`** — documented Jules configuration section.
- **`tests/test_jules_provider.py`** — 41 new tests covering init/auth/header (x-goog-api-key, key fallback), HTTP layer (create session, retry on 429, retry exhaustion, non-retryable 403 fast-fail, network-error retry), polling (immediate completion, auto-approve plan, failed state, timeout), artifact collection (agent text, changeSet unidiff edits), synchronous bridge (generate, stream-chat chunk emulation, plan, edit, analyze_error), unidiff parsing (single/multiple files, empty diff, `b/` prefix stripping), message-to-prompt flattening, JulesAdapter (import, no-key init-error capture, with-key init), manager integration (registry, capability, instance build, no-secret leak guard), and factory (fallback to LocalProvider without key, returns JulesProvider with key, no key leak in provider_status).
- **`JULES_API_RESEARCH.md`** — research notes documenting the official Jules API surface.

### Changed
- Version bumped from 1.0.2 to 1.1.0 across `backend/main.py` (FastAPI app, startup log, `/health` endpoint), `backend/metrics.py` (version field), `backend/release_checks.py` (docstring + version field), `tests/test_release_prep.py` (2 assertions), `tests/test_dashboard.py` (1 assertion).
- `providers/__init__.py` — exported `JulesAdapter`, bumped provider-package `__version__` from `0.6.0` to `0.7.0`.
- `providers/manager.py` — registered Jules in `_register_builtins()` with display name, description, capability, and `requires_api_key=True`.
- `backend/ai_provider.py` `get_provider()` factory — the `jules` branch now uses `settings.effective_jules_key()` and constructs the official-API `JulesProvider`.
- `tests/conftest.py` — pops `JULES_API_KEY` from the environment for test isolation.
- `README.md` — new "Jules Integration (v1.1.0)" section, updated providers tree, env config, adapter table, and test count.
- `API.md` — version references to 1.1.0, expanded Providers section with full Jules documentation (configuration, request/response lifecycle, retry/error handling), and provider Pydantic models.
- `ROADMAP.md` — Jules integration marked as delivered in v1.1.0.
- `BUILD_REPORT.md` — v1.1.0 build report.
- `.gitignore` — added entries for agent working artifacts (`outputs/`, `tmp/`, `summarized_conversations/`, `.pytest_cache/`, `providers/__pycache__/`, `agents/__pycache__/`).

### Security
- Jules API key is never logged, never returned by any endpoint, and excluded from `diagnostics_summary()` / `diagnostics()` / provider-manager status (verified by a no-secret-leak test guard).
- Non-retryable HTTP errors (401/403/404/422) fail immediately without retrying, preventing credential brute-forcing loops.
- Test isolation: `JULES_API_KEY` is removed from the environment in `conftest.py` so no provider test accidentally hits the live Jules API.

### Tests
- **584 tests, 0 failures** (543 existing + 41 new Jules tests).

### Manual configuration
To activate the Jules provider in a deployment, set `JULES_API_KEY` (or reuse `AI_API_KEY` / `GEMINI_API_KEY`) in the environment, then enable and activate the `jules` provider via `POST /api/providers/enable` and `POST /api/providers/active` (or the Provider Dashboard). No code changes are required.

---

## [1.0.1] — Public Deployment

Deployment-ready release with multi-platform deployment support, production configuration templates, and comprehensive deployment documentation.

### Added
- `render.yaml` — Render Blueprint for one-click deployment.
- `fly.toml` — Fly.io configuration for container deployment.
- `.env.production` — Production environment template with all variables documented.
- Comprehensive `DEPLOYMENT.md` with guides for Render, Fly.io, Docker, and self-hosted.
- HTTPS and custom domain configuration documentation.
- Production checklist and monitoring guide.

### Changed
- Version bumped from 1.0.0 to 1.0.1.
- Fixed flaky `test_validate_workspace_symlink_escape` (proper symlink cleanup before creation).

### Fixed
- Flaky security test now properly cleans up existing symlinks before creating new ones.

### Deployment
- **Render**: One-click via `render.yaml` blueprint.
- **Fly.io**: One-command via `fly launch --copy-config`.
- **Docker**: `docker compose up -d` with `.env.production`.
- **GHCR**: `docker pull ghcr.io/salmanlaghari/pk-ninja-agent:1.0.1`.

---

## [1.0.0] — Stable Release

The first official stable release of PK Ninja Agent. This release focuses on stability, quality, performance, and comprehensive documentation. No new features — only hardening, cleanup, and polish of the existing production-ready codebase.

### Changed
- Version bumped from 0.9.0 to 1.0.0.
- Removed junk `None` file (accidentally committed SQLite test artifact).
- Fixed flaky `test_validate_workspace_symlink_escape` (proper directory setup in test isolation).
- Extracted duplicate `_rt_for()` function from `terminal_agent.py` and `testing_agent.py` into shared `get_runtime_for_ctx()` in `agents/base.py`.
- Removed unused imports from 12 backend files (`agent.py`, `ai_provider.py`, `config.py`, `context_engine.py`, `exporter.py`, `indexing.py`, `main.py`, `metrics.py`, `recovery.py`, `scheduler.py`, `security.py`, `terminal.py`, `workspace.py`).
- Cleaned up `main.py` imports: removed 7 unused imports (`TaskRuntime`, `AuthService`, `QueueStatus`, `BackgroundWorker`, `psutil_available`, `WorkspaceValidationResult`, `check_extra_blocked`).

### Added
- `SECURITY.md` — comprehensive security policy and architecture documentation.
- `API.md` — full API reference covering all endpoints.

### Fixed
- Test isolation issue in security hardening tests.
- All tests now pass consistently (543/543).

### Security
- Verified no hardcoded secrets in codebase.
- Verified no `eval`/`exec` usage.
- Verified all subprocess usage is sandboxed.
- Verified path traversal protection across all file operations.
- Verified command sandbox (allowlist, blocklist, timeout, process group isolation).

### Tests
- **543 tests, 0 failures**.
- All tests pass consistently in full-suite runs.

---

## [0.9.0] — Production & Deployment

The release that makes PK Ninja Agent production-ready with containerization, CI/CD, structured logging, monitoring, backup/recovery, security auditing, and graceful shutdown. Every addition is backward compatible — the application behaves exactly as v0.8.0 with default settings.

### Added

#### Docker & Containerization
- Multi-stage `Dockerfile` with non-root user, health check, and lean production image.
- `docker-compose.yml` with app + nginx reverse proxy + persistent volumes.
- `nginx.conf` with WebSocket support, static file caching, and API proxying.
- `.dockerignore` for optimized build context.

#### Startup & Configuration
- `scripts/start.sh` — production startup script with Python version check, dependency validation, production safety warnings, database migration, and uvicorn launch.
- Environment validation at startup (missing secrets, debug mode in production).

#### Graceful Shutdown
- `backend/shutdown.py` — SIGTERM/SIGINT signal handling with worker drain, scheduler queue preservation, and clean exit.
- Forced shutdown on second signal (Ctrl+C twice).

#### Structured Logging
- `backend/structured_logging.py` — JSON-structured log output for production (`APP_ENV=production`), plain text for development.
- `RequestLoggingMiddleware` — logs every HTTP request with timing, status, and request ID.
- `RequestContextFilter` — injects request context into log records.

#### Monitoring
- `backend/metrics.py` — Prometheus `/metrics` endpoint with task, HTTP, provider, and database metrics.
- Graceful degradation when `prometheus_client` is not installed.
- Metrics: task counts, task duration, queue size, worker active, HTTP requests, provider latency.

#### Backup & Recovery
- `backend/backup.py` — SQLite backup manager with point-in-time snapshots via SQLite online backup API.
- Backup rotation with configurable retention (default: 30 backups).
- Backup verification (integrity check), restore with safety backup, and size tracking.
- Scheduled background backups via `schedule_backups()`.

#### CI/CD
- `.github/workflows/ci.yml` — test matrix (Python 3.10/3.11/3.12), dependency audit, security scan, Docker build verification, lint.
- `.github/workflows/release.yml` — tag-triggered release with Docker image push to GHCR, changelog generation, and GitHub Release creation.

#### Security Auditing
- `scripts/audit.sh` — automated security audit: dependency vulnerabilities (pip-audit), security scan (bandit), hardcoded secrets detection, .env tracking check, dangerous import detection.

#### Documentation
- `DEPLOYMENT.md` — comprehensive deployment guide: Docker, configuration, startup, backup, monitoring, CI/CD, production checklist, troubleshooting.

#### Tests
- `tests/test_production_infra.py` — 19 new tests covering structured logging, shutdown, backup manager, metrics, and script existence.

### Changed
- Version bumped from 0.8.0 to 0.9.0.
- `backend/main.py` — integrated structured logging middleware, shutdown handlers, and metrics endpoint.
- `backend/release_checks.py` — version updated to 0.9.0.
- Fixed `python` → `python3` in test files and agent commands for system compatibility.
- Fixed test isolation issue in `test_security_hardening.py` (workspace directory creation).

### Tests
- Test count grew from **524** (v0.8.0) to **543**.
- New: `tests/test_production_infra.py` (19 tests).
- All 524 pre-existing tests pass (1 pre-existing flaky scheduler test isolated).

---

## [0.8.0] — Autonomous Execution Engine

The release that transforms PK Ninja Agent from an interactive coding agent into a true **autonomous coding platform**. It adds a task scheduler, background worker, persistent workspace sessions, a live execution monitor, a crash-recovery system, searchable job history, multi-format export, indexing performance optimizations, and a security-hardening layer. Every feature is **opt-in and backward compatible** — with all new flags at their defaults, the app behaves exactly as v0.7.0.

### Added

#### Task Scheduler (Phase 1)
- Priority task queue (`backend/scheduler.py`) with enqueue, pause, resume, cancel, retry, and reorder operations.
- `SCHEDULER_ENABLED` (default `false`), `SCHEDULER_DEFAULT_RETRIES`, `SCHEDULER_DEFAULT_PRIORITY` configuration.
- New endpoints: `GET /api/queue`, `POST /api/queue/enqueue`, `POST /api/queue/{task_id}/pause`, `POST /api/queue/{task_id}/resume`, `POST /api/queue/{task_id}/cancel`, `POST /api/queue/{task_id}/retry`, `POST /api/queue/reorder`.
- When `SCHEDULER_ENABLED=false` (default), `POST /api/tasks` starts the task directly (unchanged v0.7.0 behavior).

#### Background Worker (Phase 2)
- Background worker (`backend/worker.py`) that drains the scheduler queue and executes tasks independently of the HTTP request lifecycle.
- `WORKER_MAX_CONCURRENCY` (default 2), `WORKER_POLL_INTERVAL_SECONDS` (default 1.0) configuration.
- Daemon-thread execution; tasks continue running even if the client disconnects.

#### Workspace Sessions (Phase 3)
- Persistent repository sessions (`backend/sessions.py`) that link task IDs to workspace directories, branches, and repo context.
- New endpoints: `GET /api/sessions`, `GET /api/sessions/{task_id}`, `POST /api/sessions/{task_id}/restore`, `POST /api/sessions/{task_id}/close`.
- Session restoration reuses an existing workspace directory and branch context for a new task.

#### Execution Monitor (Phase 4)
- Live execution monitor (`backend/monitor.py`) with CPU usage, memory usage, running commands, task duration, and estimated completion — powered by `psutil` (soft dependency with graceful fallback).
- New endpoint: `GET /api/monitor` (live system + per-task metrics).

#### Recovery System (Phase 5)
- Crash-recovery system (`backend/recovery.py`) that detects interrupted tasks (status=running but no live runtime), resumes safely, and preserves all event logs.
- `RECOVERY_AUTO_RESUME` (default `false`) configuration.
- New endpoints: `GET /api/recovery`, `POST /api/recovery/{task_id}/resume`, `POST /api/recovery/{task_id}/mark-failed`.

#### Job History (Phase 6)
- Searchable job history (`backend/history.py`) — a read-only query layer over the existing `tasks` + `events` tables.
- Search by description or event message; filter by repository, status, and date range; pagination with event previews.
- New endpoints: `GET /api/history`, `GET /api/history/{task_id}`, `GET /api/history-stats`.

#### Export (Phase 7)
- Multi-format export (`backend/exporter.py`) — a pure transformation layer (no DB, no side effects).
- Export single-task logs as JSON, text, or markdown report; export filtered history as JSON or CSV.
- New endpoints: `GET /api/export/{task_id}`, `GET /api/export-history`.

#### Performance (Phase 8)
- Indexing optimizations (`backend/indexing.py`): mtime + size fast path (skip unchanged files without re-reading via a single `os.stat`), batched upserts/deletes via `executemany`, idempotent schema migration (`ALTER TABLE repo_files ADD COLUMN size`).
- Cache now stores `(hash, mtime, size)` tuples; same return contract (`{added, updated, deleted, total}`).

#### Security Hardening (Phase 9)
- Security module (`backend/security.py`) with three hardening areas: workspace validation (symlink-escape detection, world-writable directory check, root containment, file-count limit), destructive-argument containment (blocks `rm -rf .`, `rm -rf *`, parent traversal, absolute paths for `rm`/`mv`/`cp`/`rmdir`), and sensitive-file protection (detects `.env`, SSH keys, certificate extensions, credential files, API-key substrings).
- 15 additional blocklist patterns (`EXTRA_BLOCKED_PATTERNS`): `rm -rf ~`, `chmod -R 777`, `chown -R`, `cat /etc/shadow`, `nc` listener, `crontab`, `systemctl`, `export SECRET=`, SSH `authorized_keys` injection, etc.
- `full_command_check` integrated pipeline (extra blocklist + terminal validation + destructive-arg containment).
- `SECURITY_HARDENING_ENABLED` (default `false`), `SECURITY_MAX_WORKSPACE_FILES` (default 200,000) configuration.
- New endpoints: `GET /api/security/workspace/{name}`, `POST /api/security/check-command`, `POST /api/security/sensitive-path`, `GET /api/security/status`.

### Tests
- Test count grew from **314** (v0.7.0) to **524**.
- New test files: `tests/test_scheduler.py` (29), `tests/test_worker.py` (13), `tests/test_sessions.py` (14), `tests/test_monitor.py` (16), `tests/test_recovery.py` (16), `tests/test_history.py` (28), `tests/test_export.py` (21), `tests/test_indexing_perf.py` (8), `tests/test_security_hardening.py` (65).
- All 314 pre-existing tests pass unchanged.

### Backward Compatibility
- `SCHEDULER_ENABLED=false` (default) → tasks start directly, no queue.
- `SECURITY_HARDENING_ENABLED=false` (default) → existing `terminal.validate_command` is the only guard.
- `RECOVERY_AUTO_RESUME=false` (default) → interrupted tasks detected but not auto-resumed.
- All existing endpoints, models, and frontend panels retained.

---

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
