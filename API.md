# API Reference — PK Ninja Agent v1.1.0

Base URL: `http://localhost:8000`

---

## Health & Status

### `GET /health`
Public health check (no auth required).

**Response:** `{ "status": "ok", "version": "1.1.0" }`

### `GET /api/system/health`
Detailed system health with component breakdown.

**Response:**
```json
{
  "status": "ok",
  "version": "1.1.0",
  "environment": "development",
  "components": [
    {"name": "database", "status": "ok", "detail": "..."},
    {"name": "ai_provider", "status": "ok", "detail": "..."}
  ]
}
```

### `GET /api/config`
Non-secret configuration summary.

### `GET /metrics`
Prometheus scrape endpoint (requires `prometheus_client`).

---

## Authentication

### `GET /api/auth/status`
Check auth state (public, no auth required).

### `POST /api/auth/guest`
Create a guest session.
```json
{ "display_name": "Guest" }
```

### `POST /api/auth/github`
Sign in with GitHub token.
```json
{ "github_token": "ghp_..." }
```

### `POST /api/auth/logout`
Invalidate current session.

### `GET /api/me`
Get current user identity (requires auth).

---

## Tasks

### `POST /api/tasks`
Create a new task.
```json
{
  "description": "Fix the login bug",
  "repository": "owner/repo"  // optional
}
```

**Response:**
```json
{
  "task_id": "abc-123",
  "status": "running",
  "repository": "owner/repo"
}
```

### `GET /api/tasks`
List all tasks.

### `GET /api/tasks/{task_id}`
Get task details with events.

### `POST /api/tasks/{task_id}/cancel`
Cancel a running task.

### `GET /api/tasks/{task_id}/events`
Get task event log.

### `GET /api/tasks/{task_id}/stream`
SSE event stream for live task activity.

### `WS /api/tasks/{task_id}/ws`
WebSocket stream for live task activity (preferred).

### `POST /api/tasks/{task_id}/index`
Trigger repository indexing.

### `GET /api/tasks/{task_id}/tree`
Get repository file tree.

### `GET /api/tasks/{task_id}/symbols?q=...`
Search repository symbols.

### `POST /api/tasks/{task_id}/run`
Execute a sandboxed command.
```json
{ "command": "python3 -m pytest" }
```

---

## Git

### `GET /api/git/branches?task_id=...`
List branches.

### `POST /api/git/checkout`
Switch branch.
```json
{ "task_id": "...", "branch": "main", "create": false }
```

### `POST /api/git/stage`
Stage a file.
```json
{ "task_id": "...", "path": "file.py" }
```

### `POST /api/git/unstage`
Unstage a file.

### `POST /api/git/discard`
Discard changes to a file.

### `POST /api/git/branch`
Create a new branch.

### `POST /api/git/commit`
Commit all changes.
```json
{ "message": "feat: add new feature" }
```

### `POST /api/git/push`
Push to remote.

---

## Pull Requests

### `POST /api/pr/prepare`
Prepare a PR (dry-run).

### `POST /api/pr/create`
Create a PR on GitHub.

---

## Scheduler (opt-in, `SCHEDULER_ENABLED=true`)

### `GET /api/queue`
List queued tasks.

### `POST /api/queue/enqueue`
Enqueue a task with priority.

### `POST /api/queue/pause`
Pause a queued task.

### `POST /api/queue/resume`
Resume a paused task.

### `POST /api/queue/cancel`
Cancel a queued task.

### `POST /api/queue/retry`
Retry a failed task.

### `POST /api/queue/reorder`
Reorder task priority.

### `GET /api/worker`
Background worker status.

---

## Sessions

### `GET /api/sessions`
List all workspace sessions.

### `POST /api.sessions`
Create a session.

### `GET /api/sessions/{session_id}`
Get session details.

### `POST /api/sessions/{session_id}/restore`
Restore a session.

### `POST /api/sessions/{session_id}/close`
Close a session.

---

## Monitor

### `GET /api/monitor`
Live system + per-task metrics.

### `GET /api/monitor/system`
System-wide resource metrics.

---

## Recovery

### `GET /api/recovery`
Detect interrupted tasks.

### `POST /api/recovery/{task_id}/resume`
Resume an interrupted task.

### `POST /api/recovery/{task_id}/mark-failed`
Mark an interrupted task as failed.

---

## History

### `GET /api/history`
Search/filter job history. Query params: `repo`, `status`, `search`, `date_from`, `date_to`, `limit`, `offset`.

### `GET /api/history/{task_id}`
Get full job detail.

### `GET /api/history-stats`
Aggregate statistics.

---

## Export

### `GET /api/export/{task_id}?format=json|text|markdown`
Export task logs/report.

### `GET /api/export/history?format=json|csv`
Export filtered history.

---

## Security

### `GET /api/security/workspace/{name}`
Validate workspace safety.

### `POST /api/security/check-command`
Dry-run command validation.
```json
{ "command": "rm -rf /" }
```

### `POST /api/security/sensitive-path`
Check if a path is sensitive.
```json
{ "path": ".env" }
```

### `GET /api/security/status`
Security configuration summary.

---

## Settings

### `GET /api/settings`
Get user preferences.

### `PUT /api/settings`
Update preferences.
```json
{ "theme": "shinobi", "auto_save_enabled": true }
```

---

## Workspaces

### `GET /api/workspaces`
List all workspaces.

### `GET /api/workspaces/recent`
Recently accessed workspaces.

### `POST /api/workspaces`
Create workspace.
```json
{ "name": "my-project", "repo": "owner/repo" }
```

### `PUT /api/workspaces`
Rename workspace.

### `DELETE /api/workspaces/{name}`
Delete workspace.

### `POST /api/workspaces/switch`
Switch active workspace.

---

## Providers

PK Ninja Agent v1.1.0 ships a pluggable provider manager with four built-in providers: `local`, `gemini`, `jules`, and `mock`. The Jules provider (new in v1.1.0) wraps the official Jules asynchronous coding-agent REST API and is registered as a first-class provider alongside the others.

### `GET /api/providers`
Provider manager status. Returns the registry of available providers, their advertised capabilities, enabled/active flags, and per-provider health snapshots. Secrets (API keys) are never included in the response.

**Response (excerpt):**
```json
{
  "providers": [
    {
      "name": "local",
      "display_name": "Local (offline, bundled model)",
      "enabled": true,
      "active": true,
      "requires_api_key": false,
      "capability": {"streaming": false, "tool_calling": false, "code_editing": true}
    },
    {
      "name": "jules",
      "display_name": "Jules (official async coding agent)",
      "enabled": true,
      "active": false,
      "requires_api_key": true,
      "capability": {"streaming": true, "tool_calling": true, "code_editing": true}
    }
  ]
}
```

### `POST /api/providers/enable`
Enable a provider. Body: `{ "name": "jules" }`. A provider that `requires_api_key` cannot be activated until its key is configured (see *Jules configuration* below).

### `POST /api/providers/disable`
Disable a provider. Body: `{ "name": "jules" }`.

### `POST /api/providers/active`
Set the active provider used by all agents. Body: `{ "name": "jules" }`. If the active provider is unavailable or fails its health check, the manager falls back to the next healthy enabled provider, ultimately reaching `local`.

### `GET /api/providers/{name}/health`
Health check a provider. For Jules this performs a lightweight reachability/config check (it does not create a remote session) and returns a `ProviderHealth` snapshot.

### `GET /api/providers/{name}/capabilities`
Provider capabilities. For Jules the advertised `ProviderCapability` is:
```json
{
  "streaming": true,
  "tool_calling": true,
  "code_editing": true,
  "context_window": 0,
  "max_output": 0
}
```
`context_window` and `max_output` are reported as `0` because the Jules async agent does not expose fixed token limits through its REST API.

### Jules configuration

The Jules provider reads its credentials and tuning from the following settings (see `backend/config.py`):

| Variable | Default | Description |
|----------|---------|-------------|
| `JULES_API_KEY` | *(none)* | Jules API key. Falls back to `AI_API_KEY` then `GEMINI_API_KEY` via `Settings.effective_jules_key()`. |
| `JULES_BASE_URL` | `https://jules.googleapis.com/v1alpha` | Official Jules REST API base URL. |
| `JULES_POLL_INTERVAL_SECONDS` | `3.0` | Seconds between session-state polls. |
| `JULES_POLL_TIMEOUT_SECONDS` | `600` | Maximum seconds to wait for a session to reach a terminal state. |
| `JULES_MAX_RETRIES` | `3` | Maximum HTTP retry attempts for transient errors (429/5xx and network errors). |

Authentication uses the `x-goog-api-key` request header (not `Bearer`). The key is never logged, never returned by any endpoint, and is excluded from `diagnostics_summary()` / `diagnostics()`.

### Jules request/response lifecycle

Jules is an asynchronous, session-based coding agent rather than an OpenAI-compatible chat-completions endpoint. Each synchronous provider call (`generate`, `plan`, `edit`, `analyze_error`, `stream_chat`) therefore follows this lifecycle internally:

1. **Create session** — `POST {JULES_BASE_URL}/sessions` with a `userInput` prompt (and an optional `gitRepository` / `targetBranch` for repo-bound edits).
2. **Poll to terminal** — `GET /sessions/{id}` is polled every `JULES_POLL_INTERVAL_SECONDS` until the session reaches `COMPLETED` or `FAILED` (bounded by `JULES_POLL_TIMEOUT_SECONDS`).
3. **Auto-approve plan** — when the session enters the `AWAITING_PLAN_APPROVAL` state, the provider automatically calls `POST /sessions/{id}:approvePlan` so the agent can proceed without interactive approval.
4. **Collect artifacts** — `GET /sessions/{id}/activities` is fetched and the provider extracts `agentMessaged` event payloads (agent text) and any `changeSet.gitPatch.unidiffPatch` (code edits). The unidiff is parsed with `_parse_unidiff()` to reconstruct `{path, content}` edits.
5. **Return** — `generate`/`chat`/`review`/`summarize` return the agent text; `plan` returns a `Plan` object; `edit` returns the reconstructed edits; `stream_chat` emulates streaming by delivering the collected text in ~12-word chunks (Jules exposes no SSE endpoint).

Session states observed by the provider: `QUEUED`, `PLANNING`, `AWAITING_PLAN_APPROVAL`, `AWAITING_USER_FEEDBACK`, `IN_PROGRESS`, `PAUSED`, `FAILED`, `COMPLETED` (terminal: `COMPLETED`, `FAILED`).

### Jules retry & error handling

The HTTP layer (`JulesProvider._request`) retries only transient failures:
- **Retryable HTTP statuses:** `429`, `500`, `502`, `503`, `504` — retried with exponential backoff (`min(2**attempt, 8)` seconds), up to `JULES_MAX_RETRIES`.
- **Retryable network errors:** any `httpx.HTTPError` that is not an `HTTPStatusError` (connection reset, timeout, DNS, etc.) — always retried.
- **Non-retryable HTTP statuses:** `401`, `403`, `404`, `422`, etc. — fail immediately without retrying.

All failures are surfaced as `AIError` with a descriptive message; secrets are never included. `diagnostics_summary()` reports non-secret counters (`sessions_created`, `sessions_completed`, `sessions_failed`, `plans_auto_approved`, `retries`, `last_error_status`) for observability.

---

## Dashboard

### `GET /api/dashboard`
Aggregated dashboard data.

---

## Diff

### `GET /api/diff?task_id=...`
Get git diff for a task.

---

## Backup

### `POST /api/backup`
Create a database backup.

### `GET /api/backup`
List backups.

### `POST /api/backup/restore`
Restore from backup.
```json
{ "name": "pk_ninja_20260802_120000.db", "confirm": true }
```

---

## Pydantic Models

All request/response bodies use Pydantic v2 models. See `backend/models.py` for full schema definitions.

Key models:
- `TaskCreate` — task creation request
- `TaskStatus` — idle, running, planning, success, failed, cancelled, queued
- `EventType` — session_started, plan, edit, command_started, command_output, command_finished, completed, error, cancelled, etc.
- `QueueStatus` — idle, paused, running, completed, failed, cancelled
- `SessionOut` — session response
- `DashboardOut` — dashboard aggregation
- `ProviderCapability` — streaming, tool_calling, code_editing, context_window, max_output
- `ProviderHealth` — per-provider health snapshot (status, detail, last_checked)
- `ProviderInfo` — registry entry (name, display_name, capability, requires_api_key)

---

## Error Responses

All errors return JSON:
```json
{ "detail": "Error message" }
```

Status codes:
- `400` — Bad request (validation error)
- `401` — Unauthorized (auth required)
- `403` — Forbidden (auth failed)
- `404` — Not found
- `409` — Conflict (e.g., task still running)
- `500` — Internal server error (generic in production)
