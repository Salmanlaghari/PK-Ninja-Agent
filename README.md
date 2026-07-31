# 🥷 PK Ninja Agent

A mobile-friendly, web-based **coding agent** that connects to a GitHub repository, inspects and edits files, runs real terminal commands in a sandboxed workspace, streams live activity to your browser, and prepares a branch for a Pull Request.

This is a genuine working agent backend — not a mock. Every terminal event shown in the UI comes from a real command execution; every file change is a real edit on disk.

---

## 1. Project Overview

PK Ninja Agent runs a small agent loop against a GitHub repository:

```
USER TASK
  ↓
UNDERSTAND TASK
  ↓
SEARCH REPOSITORY
  ↓
READ RELEVANT FILES
  ↓
CREATE PLAN
  ↓
EDIT FILES
  ↓
RUN VERIFICATION
  ↓
IF FAILURE → ANALYZE ERROR → FIX → RUN AGAIN
  ↓
SHOW DIFF
  ↓
CREATE BRANCH
  ↓
COMMIT
  ↓
PUSH
```

The agent uses a pluggable **AI provider** interface. The MVP ships with a fully offline `LocalProvider` (no API key, deterministic, real edits for common safe tasks) and a ready-to-use `GeminiProvider` adapter that activates when `GEMINI_API_KEY` is set. Swapping providers does not require touching the GitHub, terminal, workspace, or UI code.

Since **v0.6.0** the project also ships a modular **Provider Plugin System** (`providers/` package) with a `ProviderManager`, capability detection, health monitoring, automatic fallback, and a UI management panel. It is strictly opt-in (`PROVIDER_MANAGER_ENABLED=false` by default) so the existing `get_provider()` factory keeps working unchanged. See §12 *AI Provider Plugin System* for details.

### Agent tools

`list_files`, `search_files`, `read_file`, `write_file`, `edit_file`, `create_file`, `delete_file`, `git_status`, `git_diff`, `create_branch`, `git_commit`, `git_push`, `run_command`.

### Live event types

`session_started`, `analyzing`, `searching`, `file_read`, `planning`, `editing`, `command_started`, `command_output`, `command_finished`, `test_started`, `test_finished`, `error`, `fixing`, `completed`, `info`. The frontend only ever displays events that actually happened.

---

## 2. Architecture

```
pk-ninja-agent/
├── backend/
│   ├── main.py          # FastAPI app: endpoints, SSE, SQLite, static UI
│   ├── agent.py         # event bus, agent loop, tool registry
│   ├── terminal.py      # real subprocess exec + allowlist/blocklist + timeout
│   ├── github.py        # clone/pull, branch, commit, push, PR prep (server-side)
│   ├── workspace.py     # path-safe file ops + git helpers (sandbox per task)
│   ├── ai_provider.py   # AIProvider interface + Local + Gemini adapter
│   ├── models.py        # pydantic schemas + EventType/TaskStatus enums
│   └── config.py        # env-driven settings (secrets stay server-side)
├── providers/           # v0.6.0 Provider Plugin System
│   ├── interface.py     # ProviderCapability, ProviderHealth, ProviderInfo, ProviderProtocol
│   ├── manager.py       # ProviderManager: registry, dynamic load, health, fallback
│   ├── local_provider.py     # LocalAdapter (wraps existing LocalProvider)
│   ├── openai_provider.py    # OpenAIAdapter (any OpenAI-compatible endpoint)
│   ├── gemini_provider.py    # GeminiAdapter (config-only, OpenAI-compatible route)
│   └── mock_provider.py      # MockProvider (deterministic test double)
├── frontend/
│   ├── index.html       # mobile-first ninja UI (+ Provider Management panel)
│   ├── app.js           # SSE consumer, panels, git controls, provider panel
│   └── style.css        # dark "shinobi" theme
├── tests/               # pytest suite (114 existing + 65 provider system tests)
├── .devcontainer/       # Codespaces config
├── requirements.txt
├── .env.example
└── README.md
```

**Data flow:** Browser → FastAPI (`POST /api/tasks`) → background thread runs `Agent` → emits `Event`s to the in-process `EventBus` → persisted to SQLite and streamed via SSE → browser renders activity + terminal + diff in real time.

**Security boundaries:**
- `workspace.py` resolves every path against the task workspace and rejects `..` traversal and absolute escapes.
- `terminal.py` validates the program against an allowlist, blocks destructive patterns, enforces a timeout, and always sets `cwd` to the workspace.
- `github.py` keeps all credentials server-side; tokens are never serialized into any API response or sent to the browser.

---

## 3. GitHub Token Setup

1. Create a Personal Access Token at https://github.com/settings/tokens with the **`repo`** scope (and `workflow` only if you plan to push workflow files).
2. In Codespaces, add it as a secret named `GITHUB_TOKEN` (Codespaces → Settings → Secrets). It is injected automatically into your codespace env.
3. Locally, put it in your `.env` file (see below). Never commit `.env`.

The token is read only by `config.py` and `github.py` on the server. It is never exposed to frontend JavaScript.

---

## 4. Environment Variable Setup

Copy `.env.example` to `.env` and fill in:

```bash
cp .env.example .env
```

```ini
GITHUB_TOKEN=ghp_xxx
GITHUB_OWNER=your-username
GITHUB_REPO=your-repo

WORKSPACE_ROOT=./workspaces
DATABASE_PATH=./pk_ninja.db

HOST=0.0.0.0
PORT=8000

AI_PROVIDER=local        # or "gemini"
GEMINI_API_KEY=          # only if AI_PROVIDER=gemini
GEMINI_MODEL=gemini-1.5-flash

# v0.6.0 Provider Plugin System (all optional, opt-in)
PROVIDER_MANAGER_ENABLED=false   # set "true" to use the ProviderManager + fallback
PROVIDER_ENABLED=local,openai,gemini   # comma-separated enabled provider list
PROVIDER_FALLBACK_ORDER=          # explicit fallback chain (overrides auto-built)
PROVIDER_HEALTH_INTERVAL=300      # seconds between background health probes

COMMAND_TIMEOUT_SECONDS=30
```

If GitHub vars are unset, the agent still runs in a local-only workspace (clone/push are skipped with an informational event).

---

## 5. Codespaces Setup

1. Open the repository on GitHub → click **Code → Codespaces → Create codespace on main**.
2. The `.devcontainer/devcontainer.json` installs Python 3.11, the GitHub CLI, and `requirements.txt` automatically.
3. Set the `GITHUB_TOKEN` secret (and optionally `GEMINI_API_KEY`) under your Codespaces secrets so they are available as env vars.
4. Port `8000` is forwarded and auto-opened in preview.
5. Run the backend (see §7). The forwarded URL serves both the API and the UI.

---

## 6. Local Development

```bash
git clone https://github.com/Salmanlaghari/PK-Ninja-Agent.git
cd PK-Ninja-Agent
python -m venv .venv && source .venv/bin/activate   # optional
pip install -r requirements.txt
cp .env.example .env   # then edit values
```

Run the test suite:

```bash
pytest -q
```

---

## 7. Running the Backend

From the repository root:

```bash
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

Or with the configured host/port:

```bash
python -c "from backend.config import get_settings; s=get_settings(); print(s.host, s.port)"
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Health check:

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

---

## 8. Running the Frontend

The frontend is served by the same FastAPI app — there is no separate build step.

1. Start the backend (§7).
2. Open `http://localhost:8000/` (or your Codespaces forwarded URL) on your phone or desktop.
3. The repository panel shows the connected repo. Enter a task and tap **Start Agent**.
4. Watch live activity, real terminal output, changed files, and the git diff appear in real time.
5. Use **Git Controls** to create a branch, commit, push, and prepare/create a Pull Request.

### API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/health` | Health check |
| `GET`  | `/api/repository` | Connected repo metadata |
| `POST` | `/api/tasks` | Create + start an agent task |
| `GET`  | `/api/tasks` | List tasks |
| `GET`  | `/api/tasks/{id}` | Task detail + events |
| `GET`  | `/api/tasks/{id}/events` | Persisted events |
| `GET`  | `/api/tasks/{id}/stream` | **SSE** live event stream |
| `POST` | `/api/tasks/{id}/cancel` | Cancel a running task |
| `GET`  | `/api/diff?task_id=` | Git diff for a task |
| `POST` | `/api/git/branch` | Create a branch |
| `POST` | `/api/git/commit` | Stage + commit |
| `POST` | `/api/git/push` | Push the branch |
| `POST` | `/api/pr/prepare` | Prepare a PR (no creation) |
| `POST` | `/api/pr/create` | Open a real PR (explicit action) |
| `GET`  | `/api/providers` | List registered providers + health + capabilities (v0.6.0) |
| `POST` | `/api/providers/enable` | Enable a provider by name |
| `POST` | `/api/providers/disable` | Disable a provider by name |
| `POST` | `/api/providers/active` | Set the active provider |
| `GET`  | `/api/providers/{name}/health` | Provider health (status, errors, avg latency) |
| `GET`  | `/api/providers/{name}/capabilities` | Provider capability flags |

---

## 9. Security Notes

- **Secrets never reach the browser.** `config.py` and `github.py` are server-side only; no endpoint serializes tokens or API keys.
- **Path traversal is blocked.** `Workspace.safe_path` rejects `..`, absolute escapes, and resolves every target under the task workspace root.
- **Terminal is sandboxed.** Commands run with `cwd` locked to the workspace; only allowlisted programs run; a blocklist catches `rm -rf /`, fork bombs, `dd` to devices, `curl|sh`, `eval`, `exec`, etc. A hard timeout kills runaway processes.
- **Workspace isolation.** Each task gets its own directory under `workspaces/<task_id>`. The agent cannot touch the host filesystem outside it.
- **Warnings for mutating commands.** `rm`, `mv`, `git reset/push`, `pip install`, etc. are flagged with a warning surfaced in the UI.
- **GitHub operations stay server-side.** Cloning uses a token-injected URL that is never printed; pushes go through `git`; PR creation is an explicit user action.
- **Validation.** All API inputs are validated via pydantic; file paths and branch names are validated before use.

---

## 10. Current MVP Limitations

- The bundled `LocalProvider` automates a focused set of safe tasks (adding module docstrings, resolving TODO/FIXME markers, generating a README). For arbitrary natural-language tasks it produces a real plan and reports honestly when it cannot safely auto-edit — it does not fabricate changes.
- The Gemini adapter calls the Generative Language REST API when a key is present; free-tier quotas and rate limits apply. The factory falls back to `LocalProvider` if the key is missing or the optional dependency is unavailable — no fake API is used.
- Shell operators (`|`, `&&`, `||`, `;`, `&`, `$()`, backticks) are intentionally disallowed in `run_command` for MVP safety; run one allowlisted command at a time.
- Pull Request creation is supported but kept behind an explicit user action (the **Create PR** button) per spec.
- There is no authentication on the API itself yet; in production put it behind a reverse proxy / auth layer.
- Events are stored in SQLite and an in-process bus; there is no multi-process pub/sub (e.g. Redis) yet.

---

## 11. Future Jules Integration

The architecture was deliberately built so a new AI provider — including Google's **Jules** coding agent — can be plugged in without rewriting GitHub, terminal, workspace, or UI code.

### Recommended next step

1. **Implement a `JulesProvider`** in `backend/ai_provider.py` that satisfies the `AIProvider` protocol (`plan`, `edit`, `analyze_error`). It can call the Jules API via `httpx` and parse its responses into the same `Plan` / edit-dict shapes the agent already consumes.
2. **Add `AI_PROVIDER=jules`** to `config.py` and wire it in the `get_provider()` factory alongside `local` and `gemini`. Add any `JULES_API_KEY` / `JULES_ENDPOINT` settings following the existing pattern.
3. **Keep the tool layer unchanged.** Jules (like any provider) only decides *what* to do; the actual file edits and command execution still flow through `workspace.py` and `terminal.py`, preserving all sandboxing and safety guarantees.
4. **Reuse the event bus.** A Jules provider can emit the same `EventType`s so the frontend needs zero changes — live activity and terminal output continue to render from real tool results.
5. **PR handoff.** Because `github.prepare_pull_request()` already builds the PR title/body/command, a Jules integration can optionally hand off the final commit/PR step to Jules's own git workflow by calling the same server-side functions.

This keeps the agent's safety model intact while letting a more capable model drive planning and editing.

---

## 12. AI Provider Plugin System (v0.6.0)

A modular, opt-in **Provider Plugin System** that adds dynamic provider loading, capability detection, health monitoring, and automatic fallback — all layered *on top of* the existing `backend/ai_provider.py` architecture. No existing functionality was removed or rewritten; backward compatibility is preserved by default.

### Design principles

- **Do not rebuild.** The new `providers/` package wraps the existing provider classes via the adapter pattern; `LocalAdapter`, `OpenAIAdapter`, and `GeminiAdapter` delegate to the original `LocalProvider`/`OpenAIProvider`/`GeminiProvider` implementations.
- **Backward compatible by default.** `PROVIDER_MANAGER_ENABLED=false` (the default) means `Agent` still uses the original `get_provider()` factory with zero behavior change. Only when the flag is `true` does the `ProviderManager` take over selection, fallback, and health tracking.
- **Provider independent.** The agent and UI never hard-code a provider; they ask the manager for the active provider and its capabilities.
- **No unsupported APIs.** The `GeminiAdapter` is configuration-only and routes through Google's documented OpenAI-compatible endpoint. No native Gemini/Vertex API is used or claimed. Providers that cannot be initialised (e.g. missing API key) degrade gracefully to `None` and are skipped in the fallback chain.

### Core components

| Component | File | Responsibility |
|-----------|------|----------------|
| `ProviderProtocol` | `providers/interface.py` | Common interface: `plan`, `chat`, `edit`, `review`, `summarize`, `stream` + original `AIProvider` members |
| `ProviderCapability` | `providers/interface.py` | Flags: `streaming`, `tool_calling`, `code_editing`, `context_window`, `max_output` |
| `ProviderHealth` | `providers/interface.py` | `status` (UNKNOWN/HEALTHY/DEGRADED/UNHEALTHY/DISABLED), `last_success`, `last_error`, `error_count`, `success_count`, `avg_response_time_ms` |
| `ProviderInfo` | `providers/interface.py` | Registry record: name, display name, description, capability, `requires_api_key`, enabled, configurable, health, fallback_for |
| `ProviderManager` | `providers/manager.py` | Central registry, dynamic loading, enable/disable, capability detection, health monitoring, fallback chain, `call()` wrapper |
| Adapters | `providers/{local,openai,gemini,mock}_provider.py` | Built-in adapters wrapping existing providers |

### Built-in providers

| Name | Class | API key | Notes |
|------|-------|---------|-------|
| `local` | `LocalAdapter` → `LocalProvider` | no | Offline, deterministic, safety net. Always available. |
| `openai` | `OpenAIAdapter` → `OpenAIProvider` | yes | Any OpenAI-compatible Chat Completions endpoint (OpenAI, DeepSeek, Together, OpenRouter, Ollama). Lazy init: missing key → not available, never crashes startup. |
| `gemini` | `GeminiAdapter` → `GeminiProvider` | yes | Configuration-only; routes through Google's OpenAI-compatible endpoint. No native Gemini API. |
| `mock` | `MockProvider` | no | Deterministic test double with `MockConfig` (fail injection, latency, canned responses). |

### Capability detection

Each adapter declares a `ProviderCapability` honestly. The manager exposes:

- `manager.capability(name)` → `ProviderCapability`
- `manager.providers_with_capability(flag)` → list of names supporting a given flag
- `/api/providers/{name}/capabilities` → JSON view

Capabilities are *declared* (static per adapter) rather than *probed*, so they never trigger an accidental billable API call. `context_window` and `max_output` are reported as `0` (rendered as unknown/null) when model-dependent.

### Health monitoring

`ProviderHealth` tracks every call routed through `manager.call()`:

- On success: `record_success(elapsed_ms)` updates `success_count`, `request_count`, and a running average of response time.
- On failure: `record_failure(message)` increments `error_count`, stores `last_error`/`last_error_message`.
- Status thresholds: 0 errors → `HEALTHY`; 3 errors → `DEGRADED`; 5 errors → `UNHEALTHY`. Disabled providers → `DISABLED`.
- `manager.health_check(name)` runs a lightweight `plan()` probe and returns the health dict.

### Fallback system

`manager.call(method, *args)` iterates the **fallback chain**: the active provider first, then compatible enabled providers, with `local` as the ultimate safety net. If a provider fails, its health is recorded and the next provider is tried. If the active provider becomes `UNHEALTHY`, the successful fallback is promoted to active. If *all* providers fail, the last exception is raised.

The chain is auto-built from enabled providers (active first, then by capability compatibility, `local` last) and can be overridden with `PROVIDER_FALLBACK_ORDER`.

### Configuration (environment variables, server-side only)

| Variable | Default | Purpose |
|----------|---------|---------|
| `PROVIDER_MANAGER_ENABLED` | `false` | Opt-in to the ProviderManager. `false` preserves original `get_provider()` behavior. |
| `PROVIDER_ENABLED` | `local,openai,gemini` | Comma-separated list of providers to enable at startup. |
| `PROVIDER_FALLBACK_ORDER` | *(auto)* | Explicit fallback chain, e.g. `openai,local`. Overrides auto-built chain. |
| `PROVIDER_HEALTH_INTERVAL` | `300` | Seconds between background health probes (configurable). |

Secrets (`AI_API_KEY`, `GEMINI_API_KEY`, etc.) remain server-side only and are never serialized into any API response. The `/api/config` endpoint returns a compact provider summary (names, enabled, available, health status, capability) that deliberately excludes `requires_api_key`; full provider metadata is served only at `/api/providers`.

### Provider Management UI

The sidebar now includes a **Provider Management** panel (`#panel-providers`) showing the active provider name, a live health pill, the list of available providers with enable/disable and set-active controls, and a per-provider detail view with capability flags and health metrics. A refresh button re-fetches `/api/providers`. The panel degrades gracefully when the manager is disabled (shows the classic single-provider status).

### How to add a new provider adapter

1. **Create the adapter module** `providers/myprovider_provider.py`. Implement a class that satisfies `ProviderProtocol` (at minimum `name`, `plan`, `edit`, `analyze_error`, `stream_chat`; optionally `chat`, `review`, `summarize`). Set class attributes `name`, `display_name`, `description`, `capability` (`ProviderCapability(...)`), and `requires_api_key`. If wrapping an existing `ai_provider.py` class, follow the `OpenAIAdapter` pattern: construct the inner provider lazily in `__init__`, catch `AIError` into `self._init_error`, and expose `self._inner`.
2. **Register the adapter.** Either call `register_adapter("myprovider", MyAdapter, ...)` at import time (add it to `providers/__init__.py`), or register dynamically at runtime via `ProviderManager.register(...)`. The `register_adapter()` function is the public extension point for plugins.
3. **Add settings (optional).** If your provider needs an API key or base URL, add the field to `backend/config.py` `Settings` following the existing `ai_api_key`/`gemini_api_key` pattern, and read it in your adapter's `__init__`.
4. **Enable it.** Add the name to `PROVIDER_ENABLED` (or call `manager.enable("myprovider")` via the API). Set it active with `POST /api/providers/active` or `PROVIDER_FALLBACK_ORDER`.
5. **Test it.** Add tests under `tests/` using `MockProvider`/`MockConfig` patterns for deterministic behavior. Run `pytest -q` — the full suite (existing + provider tests) must stay green.

No changes to `agent.py`, `workspace.py`, `terminal.py`, `github.py`, or the event bus are required — the tool and safety layers remain provider-independent.

### Backward compatibility guarantee

With `PROVIDER_MANAGER_ENABLED=false` (default):
- `Agent.__init__` calls `get_provider(self.settings)` exactly as before.
- `provider_status(settings)` and `/api/config` keep their original shape (the `providers` summary field is `null`).
- All 114 pre-existing tests pass unchanged.

With `PROVIDER_MANAGER_ENABLED=true`:
- `Agent._select_provider()` uses the manager's active provider, unwrapping the adapter's `_inner` so `isinstance(provider, LocalProvider)` checks still hold.
- Fallback and health tracking are active; the rest of the agent loop is unchanged.
