# PK Ninja Agent — v2 Interactive Coding Agent Build Report

## Status: ✅ Complete & verified

Upgrade of the merged MVP (PR #1) into a **real interactive fast coding agent**.
Branch: `feat/interactive-agent-v2`

> The project was **upgraded in place** — not rebuilt from scratch. All MVP
> behavior is preserved and extended; existing tests were fixed, not deleted.

---

## What changed (summary)

The MVP streamed canned progress and ran commands through a non-cancellable
`subprocess.run`. This upgrade turns it into a real, interactive, streaming
coding agent:

1. **Pluggable streaming AI provider** — a Protocol-based architecture
   (`AIProvider.stream_chat`) with a real OpenAI-compatible SSE adapter that
   works with Gemini, DeepSeek, OpenRouter, Together, Ollama and MiMo. A safe
   offline `LocalProvider` is the zero-config fallback so the agent always
   works without an API key.
2. **Live agent events** — the backend now emits real, granular events
   (`analyzing`, `searching`, `file_read`, `planning`, `editing`,
   `command_started/output/finished`, `test_started/finished`, `fixing`,
   `thinking`, `completed`, `cancelled`, `error`) over **both** SSE and a
   bidirectional **WebSocket**. The frontend prefers WebSocket and falls back
   to SSE. Nothing is faked — every message comes from real execution.
3. **Streaming AI "thinking" tokens** — when a streaming provider is
   configured, partial AI tokens are emitted as `thinking` events and rendered
   live in the UI with a typing animation.
4. **Cancellation** — a running task can be cancelled from the UI (cancel
   button) over the WebSocket (`{"type":"cancel"}`) or the REST
   `/api/tasks/{id}/cancel` endpoint. Cancellation sets a flag **and** kills
   any live subprocess via `os.killpg(SIGTERM)`, then emits a truthful
   `cancelled` event.
5. **Real terminal output** — the terminal panel now also accepts manual
   sandboxed commands (`/api/tasks/{id}/run`); all output is real.
6. **Task statuses** — `IDLE / RUNNING / SUCCESS / FAILED / CANCELLED`
   (with backward-compatible aliases for the old `pending`/`completed`).
7. **Provider status** — a new `/api/config` endpoint exposes a non-secret
   summary (provider, model, configured, streaming-supported) shown in the
   header as a live badge.
8. **Hardened sandbox** — the manual terminal endpoint now blocks absolute
   paths and parent-directory traversals, so `cat /etc/passwd`, `ls /`, and
   `cat ../../secret` are rejected. The workspace stays locked to its root.

---

## Files changed

**Backend** (`backend/`) — upgraded in place
- `config.py` — added `AI_PROVIDER`, `AI_API_KEY`, `AI_MODEL`, `AI_BASE_URL`,
  `AI_TEMPERATURE`, `AI_TIMEOUT_SECONDS`; `effective_api_key()` /
  `effective_model()` merge new + legacy env vars.
- `ai_provider.py` — rewritten: `AIProvider` Protocol with `stream_chat()`;
  `LocalProvider` (deterministic streaming); `OpenAIProvider` (real SSE
  streaming via `httpx`); `GeminiProvider`; `get_provider()` factory with
  graceful fallback; `provider_status()` non-secret summary.
- `models.py` — `TaskStatus` canonical set
  (`idle/running/success/failed/cancelled`) + legacy aliases +
  `normalize_status()`; `EventType.thinking`, `EventType.cancelled`;
  `ConfigOut` Pydantic model.
- `agent.py` — `_Cancelled` exception; `TaskRuntime` with subprocess tracking;
  `_stream_ai()` with `on_token` callback and buffered flushing;
  `_plan_with_stream`/`_edit_with_stream`/`_analyze_error_with_stream`;
  `cancel_task()` kills live subprocess; `Agent.run()` handles cancellation.
- `terminal.py` — `subprocess.Popen` (replaces `run`) for mid-run cancel;
  tracks `current_proc` on the runtime under a lock; kills the process group
  on timeout/cancel; **new sandbox path-containment check** that blocks
  absolute paths and `..` traversals outside the workspace.
- `main.py` — WebSocket endpoint `/api/tasks/{id}/ws` (bidirectional,
  cancel); `GET /api/config`; `POST /api/tasks/{id}/run`; SSE keepalive;
  `POST /api/tasks/{id}/cancel`; status persistence; fresh `get_settings()`
  per request for test isolation.

**Frontend** (`frontend/`) — modernized, mobile-first
- `style.css` — rewritten: dark "shinobi" theme with katana-red accents;
  status pills for all 5 states with a running pulse; provider badge with
  "live" indicator; streaming "thinking" token display with typing-dots
  animation; terminal panel with manual command input; file list with M/A/D/U
  badges; diff viewer with add/del/hunk coloring; cancel button; toast
  notifications; responsive breakpoint at 520px.
- `index.html` — provider badge in header; cancel button next to Start Agent;
  manual terminal command input row; thinking-label structure.
- `app.js` — rewritten: WebSocket (preferred) + SSE fallback; streaming token
  rendering for `thinking` events; cancel button (sends `{"type":"cancel"}`
  over WS + hits REST cancel); 5-state status badges; `/api/config` fetch for
  the provider badge; manual sandboxed terminal command runner.

**Tests** (`tests/`)
- `conftest.py` — autouse fixture clears `get_settings` cache per test;
  removes leaked `AI_API_KEY`/`GEMINI_API_KEY` from env.
- `test_api_health.py`, `test_task_and_events.py` — updated `/health`
  assertions for the new `version` field.
- `test_terminal.py` — +7 path-containment security tests.
- `test_workspace_restriction.py` — updated `ls /` test to assert it is now
  blocked (was previously documenting the insecure behavior).
- `test_ai_provider.py` (new, 18 tests) — factory selection, fallback,
  provider_status, LocalProvider streaming, OpenAIProvider SSE parsing,
  JSON helpers, protocol compliance.
- `test_models.py` (new, 11 tests) — TaskStatus values/aliases,
  normalize_status, EventType.thinking/cancelled, ConfigOut.
- `test_cancellation.py` (new, 5 tests) — cancel flag, subprocess kill,
  runtime cleanup.
- `test_v2_api.py` (new, 9 tests) — /api/config, /run endpoint, WebSocket
  streaming + cancel, SSE history replay.

**Config / docs**
- `.env.example` — documented `AI_PROVIDER` / `AI_API_KEY` / `AI_MODEL` /
  `AI_BASE_URL` with examples for OpenAI, Gemini, DeepSeek, OpenRouter, Ollama.

---

## Tests run & results

```
$ python -m pytest -q
94 passed, 1 warning in ~11s
```

Breakdown: 44 original (2 fixed for the new `/health` contract, 1 updated for
the hardened sandbox) + 50 new (18 ai_provider, 11 models, 5 cancellation,
 9 v2_api, 7 path-containment).

### Live verification (backend running on 127.0.0.1:8765)
- `GET /health` → `{"status":"ok","version":"0.2.0"}` ✅
- `GET /api/config` → `{"provider":"local","model":"local","configured":false,
  "streaming_supported":false,"repository_configured":false}` ✅
- `POST /api/tasks` → creates a task, returns `task_id` + `status:"running"` ✅
- SSE stream (`/api/tasks/{id}/stream`) → real events flow
  (`session_started → info → analyzing → searching → planning → editing →
  completed`) ✅
- WebSocket (`/api/tasks/{id}/ws`) → same real event stream flows ✅
- WebSocket cancel → sending `{"type":"cancel"}` yields a real
  `cancelled` event ✅
- `POST /api/tasks/{id}/run` with `ls -la` / `echo` / `python --version` →
  real stdout ✅
- Security: `cat /etc/passwd`, `ls /`, `cat ../../secret.txt`, `rm -rf /`,
  `curl http://evil.com` → all **blocked** (400) ✅
- `cat README.md` (workspace-relative) → allowed to run ✅

---

## How to start the agent

```bash
cd pk-ninja-agent/backend

# (optional) configure an external AI provider for real streaming
export AI_PROVIDER=openai          # or gemini, deepseek, openrouter, ollama
export AI_API_KEY=sk-...
export AI_MODEL=gpt-4o-mini        # provider-appropriate model
# export AI_BASE_URL=...           # only for non-default OpenAI-compatible hosts

# without any of the above, the agent falls back to the offline LocalProvider

python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`, type a task, press **Start Agent**, and watch
real activity stream in. Press **Cancel** to stop a running task. Type a
command in the terminal input row to run a real sandboxed command.

All configurable env vars are documented in `.env.example`.

---

## What remains for Jules integration

The agent is a self-contained FastAPI service. To wire it into Jules:

1. **AI provider** — set `AI_PROVIDER` / `AI_API_KEY` / `AI_MODEL` (and
   `AI_BASE_URL` if needed) to point at the Jules-compatible OpenAI-style
   endpoint. The `OpenAIProvider` already parses standard SSE `data:` lines,
   so any OpenAI-compatible endpoint works without code changes.
2. **Repository** — set `GITHUB_OWNER` / `GITHUB_REPO` (and `GITHUB_TOKEN`)
   so the agent clones the target repo into a per-task workspace. Without
   these it runs in a local-only workspace.
3. **Events** — Jules can consume events from the WebSocket
   (`/api/tasks/{id}/ws`) or SSE (`/api/tasks/{id}/stream`) and send
   `{"type":"cancel"}` over the WebSocket to cancel.
4. **Cancellation** — the REST `POST /api/tasks/{id}/cancel` endpoint is
   available as a transport-independent backstop.
5. **Manual commands** — `POST /api/tasks/{id}/run` lets Jules run a
   sandboxed command and inspect real output.

No code changes are required for Jules integration — only environment
configuration. The provider architecture is fully modular, so additional
non-OpenAI-compatible providers can be added by implementing the
`AIProvider` Protocol's `stream_chat()` method.
