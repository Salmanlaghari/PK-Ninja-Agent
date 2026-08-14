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
├── frontend/
│   ├── index.html       # mobile-first ninja UI
│   ├── app.js           # SSE consumer, panels, git controls
│   └── style.css        # dark "shinobi" theme
├── tests/               # pytest suite
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

## 5.1 Vercel Deployment

PK Ninja Agent can be easily deployed to **Vercel** with zero-configuration using Vercel's modern Python runtime.

The repository includes `pyproject.toml` and `.python-version` configured for FastAPI routing on Vercel out of the box.

For complete, step-by-step instructions on setting up writable serverless directory paths (`DATABASE_PATH` and `WORKSPACE_ROOT` under `/tmp`), environmental configurations, and deployment steps, please refer to our detailed **[Vercel Deployment Guide (DEPLOYMENT.md)](./DEPLOYMENT.md)**.

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
