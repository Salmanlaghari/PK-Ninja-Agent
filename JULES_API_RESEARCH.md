# Jules API Research Notes (Official)

## Source
Official Jules REST API documentation at https://jules.google/docs/api/reference/
and https://developers.google.com/jules/api/reference/rest

## Base URL
`https://jules.googleapis.com/v1alpha`

## Authentication
- API key passed via HTTP header: `x-goog-api-key: <KEY>`
- Recommended env var: `JULES_API_KEY`
- Keys obtained from https://jules.google.com/settings
- IMPORTANT: NOT a Bearer token. Uses `x-goog-api-key` header.
- NOT OpenAI-compatible. This is an async coding-agent API, not a chat completions API.

## Core Model: Asynchronous Coding Sessions
Jules is an ASYNC coding agent. The flow is:
1. Create a session (POST /sessions) with a `prompt` + optional `sourceContext`.
2. Poll the session (GET /sessions/{id}) for state transitions.
3. List activities (GET /sessions/{id}/activities) to get plans, progress, messages, artifacts.
4. Optionally approve a plan (POST /sessions/{id}:approvePlan) if requirePlanApproval=true.
5. Optionally send messages (POST /sessions/{id}:sendMessage).
6. Session ends in COMPLETED (with outputs like PR) or FAILED.

## Endpoints

### Sessions (v1alpha.sessions)
- POST   /v1alpha/sessions                         -> create session
- GET    /v1alpha/sessions                          -> list sessions (pageSize, pageToken)
- GET    /v1alpha/sessions/{sessionId}              -> get session
- DELETE /v1alpha/sessions/{sessionId}              -> delete session
- POST   /v1alpha/sessions/{sessionId}:sendMessage  -> send message {prompt}
- POST   /v1alpha/sessions/{sessionId}:approvePlan  -> approve plan ({})

### Activities (v1alpha.sessions.activities)
- GET /v1alpha/sessions/{sessionId}/activities         -> list activities (pageSize, pageToken, createTime)
- GET /v1alpha/sessions/{sessionId}/activities/{actId} -> get activity

### Sources (v1alpha.sources)
- GET /v1alpha/sources             -> list sources (pageSize, pageToken, filter)
- GET /v1alpha/sources/{sourceId}  -> get source

## Create Session Request Body
{
  "prompt": "<task description>",            # required
  "title": "<optional title>",
  "sourceContext": {                         # optional (repoless sessions allowed)
    "source": "sources/<sourceId>",
    "githubRepoContext": { "startingBranch": "main" }
  },
  "requirePlanApproval": true|false,
  "automationMode": "AUTO_CREATE_PR"         # optional
}

## Session States (SessionState enum)
STATE_UNSPECIFIED, QUEUED, PLANNING, AWAITING_PLAN_APPROVAL,
AWAITING_USER_FEEDBACK, IN_PROGRESS, PAUSED, FAILED, COMPLETED

## Terminal states: COMPLETED, FAILED

## Session Response
{
  "name": "sessions/1234567",
  "id": "abc123",
  "prompt": "...",
  "title": "...",
  "state": "QUEUED",
  "url": "https://jules.google.com/session/abc123",
  "createTime": "...", "updateTime": "...",
  "outputs": [ {"pullRequest": {"url":..., "title":..., "description":...}} ]
}

## Activity Event Types (exactly one populated)
- planGenerated: { plan: { id, steps: [{id,index,title,description}], createTime } }
- planApproved: { planId }
- userMessaged: { userMessage }
- agentMessaged: { agentMessage }
- progressUpdated: { title, description }
- sessionCompleted: {}
- sessionFailed: { reason }

## Artifacts (in activity.artifacts[])
- changeSet: { source, gitPatch: { baseCommitId, unidiffPatch, suggestedCommitMessage } }
- bashOutput: { command, output, exitCode }
- media: { mimeType, data(base64) }

## Error Handling
Standard HTTP codes. Error body:
{ "error": { "code": 400, "message": "...", "status": "INVALID_ARGUMENT" } }
401 unauthorized, 403 forbidden, 404 not found, 429 rate limited, 500 server error.

## Pagination
pageSize + pageToken; nextPageToken in list responses.

## Streaming
The Jules API does NOT provide a streaming (SSE) endpoint for chat tokens.
It is an async polling model. "Streaming" in our provider will be simulated
by polling progress activities and emitting progress updates as tokens.

## Integration Strategy for PK-Ninja-Agent
Since Jules is async + repo-based (operates on GitHub repos via "sources"),
the JulesProvider must adapt the synchronous AIProvider protocol (plan/edit/
stream_chat/analyze_error/chat/review/summarize) to Jules' async session model:

- plan(task, context): create a session with requirePlanApproval=true, poll until
  AWAITING_PLAN_APPROVAL or PLANNING done, fetch the planGenerated activity,
  convert Plan(steps[].title/description) -> backend.Plan(summary, steps).
- edit(task, plan, files): create a session (repoless if no source configured,
  or with sourceContext) with automationMode=AUTO_CREATE_PR (or none), poll to
  COMPLETED, collect changeSet gitPatch artifacts. Since Jules returns a git
  unidiff patch (not full file contents), we apply it to the provided `files`
  to produce the {path, content} edit list expected by the agent loop. If no
  source is configured, Jules operates repoless and returns a patch/diff that
  we parse into edits.
- stream_chat(messages, on_token): create a repoless session with the combined
  prompt, poll activities, emit agentMessaged/progressUpdated text via on_token,
  return final ChatResult. (Simulated streaming since Jules has no SSE.)
- chat(messages): same as stream_chat without token callback, return ChatResult.
- analyze_error(task, error, files): repoless session with a prompt asking to
  diagnose the error; return agentMessaged text.
- review(task, files): repoless session asking for a code review; return text.
- summarize(text): repoless session asking to summarize; return text.

Authentication: read JULES_API_KEY env (or fall back to AI_API_KEY via settings
effective_api_key). Use x-goog-api-key header.

Health check: a lightweight GET /v1alpha/sources (or list sessions) probe.

Timeouts & retries: per-request timeout (ai_timeout_seconds) + configurable
retry with exponential backoff for 429/5xx. Polling uses a max wait + interval.

Diagnostics & metrics: track session counts, poll durations, failure reasons.

Manual steps for user:
- Set JULES_API_KEY env var (use your own Jules API key from jules.google.com;
  do NOT commit real keys to the repository).
- (Optional) Connect a GitHub repo as a "source" via jules.google.com for
  repo-based sessions; otherwise sessions run repoless.
