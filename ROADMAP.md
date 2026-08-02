# Roadmap

This document tracks the planned evolution of PK Ninja Agent from the current beta toward a stable v1.0.0 and beyond. Progress is tracked against the version milestones below.

---

## Current State: v1.0.0 — Stable Release

PK Ninja Agent v1.0.0 is the first official stable release. It is a production-ready, self-hostable autonomous coding agent with 543 passing tests, comprehensive security, Docker containerization, CI/CD, structured logging, monitoring, backup/recovery, and full documentation.

The codebase is clean: dead code removed, duplicate logic extracted, unused imports cleaned, flaky tests fixed. No experimental features — only proven, tested functionality.

---

## v1.1.0+ — Future Directions

Ideas and directions for post-1.0 development. These are exploratory and not committed.

### Autonomous execution hardening
- **Worker persistence.** The background worker is in-memory (daemon threads). v1.0.0 should persist the worker queue to SQLite so in-flight tasks survive a process restart and are picked up by the recovery system automatically.
- **Scheduler persistence.** Similarly, the scheduler queue should be persisted so enqueued-but-not-started tasks survive a restart.
- **Task dependencies.** Allow tasks to declare dependencies (task B starts only after task A completes) for multi-step autonomous workflows.
- **Cron / scheduled tasks.** Time-based task triggers (nightly audits, scheduled builds) on top of the existing priority queue.

### Authentication hardening
- **OAuth flow.** Replace token-based GitHub login with a proper OAuth app flow (callback, state validation, refresh tokens) for multi-user deployments.
- **Server-side session revocation.** The current sessions are stateless HMAC tokens; v1.0.0 should add an optional server-side session store (SQLite) to support explicit revocation and session listing.
- **Rate limiting.** Add per-user rate limiting on auth endpoints to mitigate brute-force attempts.
- **CSRF protection.** Add CSRF tokens for state-changing endpoints when cookie-based auth is introduced.

### Authorization & multi-tenancy
- **Role-based access control.** Introduce roles (admin, contributor, viewer) and per-workspace permissions for multi-user deployments.
- **Workspace isolation.** Enforce per-user workspace ownership so users cannot access or modify each other's workspaces.

### Deployment & operations
- **~~Containerization.~~** ✅ Done in v0.9.0 — Dockerfile, docker-compose, nginx.
- **~~Configuration validation.~~** ✅ Done in v0.9.0 — startup script, production safety checks.
- **~~Metrics & observability.~~** ✅ Done in v0.9.0 — structured JSON logging, Prometheus /metrics.
- **~~Database migrations.~~** ✅ Done in v0.9.0 — idempotent schema, backup/restore manager.
- **~~CI/CD.~~** ✅ Done in v0.9.0 — GitHub Actions test + release workflows.

### AI provider ecosystem
- **Streaming-first providers.** Ensure all built-in adapters implement true streaming (SSE) end-to-end, including tool-calling providers.
- **Provider configuration UI.** Allow setting API keys and base URLs per provider from the settings UI (stored encrypted server-side).
- **Additional adapters.** Add Anthropic (Claude) and Mistral adapters following the existing adapter pattern.

### Testing & quality
- **Integration test suite.** Add end-to-end integration tests that exercise the full autonomous loop (create task → schedule → worker executes → plan → edit → verify → diff → export) against a real local workspace.
- **Load testing.** Validate concurrent task handling, worker concurrency limits, and SSE/WebSocket stability under load.
- **Test coverage reporting.** Wire up coverage reporting in CI.

### Documentation
- **User guide.** A dedicated user guide covering scheduler configuration, worker tuning, recovery, history search, and export workflows.
- **Admin guide.** Deployment, configuration, security hardening, and operations documentation for self-hosters.
- **API reference.** Auto-generated API reference (e.g. via FastAPI's OpenAPI schema export).

---

## v1.1.0+ — Future Directions

Ideas and directions for post-1.0 development. These are exploratory and not committed.

### Agent capabilities
- **Autonomous PR lifecycle.** End-to-end autonomous branch, PR creation, CI observation, and self-merge after review.
- **Multi-repo tasks.** Tasks that span multiple repositories (cross-repo refactoring, dependency updates).
- **Long-running background agents.** Scheduled or triggered agents that run without an active browser session (e.g. nightly dependency audits).
- **Agent memory persistence.** Long-term memory of past tasks, decisions, and learned patterns across sessions.

### Collaboration
- **Shared workspaces.** Workspaces shared between users with real-time collaboration indicators.
- **Task assignment & comments.** Assign tasks to users and add comments/discussion threads.
- **Activity feed.** A team-wide activity feed of tasks, commits, and PRs.

### UX
- **Theme system.** Full theme support (the settings model already has theme fields; wire up the actual CSS theme switching).
- **Keyboard shortcuts.** Power-user keyboard navigation for the IDE workspace.
- **Mobile-first polish.** Continue improving the mobile experience (the current mobile tab navigation is a starting point).

### Ecosystem
- **Plugin marketplace.** A mechanism for third-party provider and tool plugins.
- **Webhook integrations.** Outbound webhooks for task lifecycle events (started, completed, failed) to integrate with Slack, Discord, etc.
- **CLI companion.** A `pkninja` CLI that can create tasks and observe progress from the terminal.

---

## Versioning Policy

PK Ninja Agent follows [Semantic Versioning](https://semver.org/):

- **MAJOR** (e.g. 1.0.0): Breaking changes or the first stable release.
- **MINOR** (e.g. 0.7.0): New backward-compatible features.
- **PATCH** (e.g. 0.7.1): Backward-compatible bug fixes.

While in 0.x, minor versions may include breaking changes (pre-1.0 software). Once 1.0.0 is reached, breaking changes require a major version bump.
