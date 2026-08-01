"""Pydantic models and enums shared across the agent, API, and event system."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Event types ─────────────────────────────────────────────────────────
# Each event type maps to a real agent action. The agent never emits an event
# it didn't actually perform — there are no faked activity messages.
class EventType(str, Enum):
    session_started = "session_started"
    analyzing = "analyzing"            # understanding the task
    searching = "searching"            # searching the repository
    file_read = "file_read"            # reading a file (real read happened)
    planning = "planning"              # a plan was produced
    thinking = "thinking"              # streaming AI token (real model output)
    editing = "editing"                # a file was modified
    command_started = "command_started"
    command_output = "command_output"  # real stdout/stderr
    command_finished = "command_finished"
    test_started = "test_started"
    test_finished = "test_finished"
    error = "error"
    fixing = "fixing"                  # analyzing a failure
    completed = "completed"
    cancelled = "cancelled"
    info = "info"


# ── Task status ─────────────────────────────────────────────────────────
# The five canonical states required by the interactive agent:
#   idle       — no task running (default before a task starts)
#   running    — agent loop is active
#   success    — agent finished without errors
#   failed     — agent encountered an unrecoverable error
#   cancelled  — user cancelled the running task
# Legacy values (pending/completed) are kept as aliases for backward compat
# with existing persisted rows and tests.
class TaskStatus(str, Enum):
    idle = "idle"
    running = "running"
    success = "success"
    failed = "failed"
    cancelled = "cancelled"
    # Legacy aliases (kept for backward compatibility with v1 data).
    pending = "pending"
    completed = "completed"


# Map legacy status strings to the canonical set so old DB rows render right.
_STATUS_NORMALIZE = {
    "pending": "idle",
    "completed": "success",
}


def normalize_status(status: str) -> str:
    """Return the canonical status string for a (possibly legacy) value."""
    return _STATUS_NORMALIZE.get(status, status)


# ── API request / response models ───────────────────────────────────────
class TaskCreate(BaseModel):
    description: str = Field(..., min_length=1, max_length=4000)
    repository: Optional[str] = None  # "owner/repo" override; else uses config


class TaskSummary(BaseModel):
    task_id: str
    description: str
    status: str
    created_at: datetime
    updated_at: datetime
    branch: Optional[str] = None


class EventOut(BaseModel):
    task_id: str
    type: EventType
    message: str
    data: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime


class DiffOut(BaseModel):
    task_id: str
    branch: Optional[str]
    staged: str
    unstaged: str
    files: List[str] = Field(default_factory=list)


class GitBranchRequest(BaseModel):
    task_id: str
    branch: str = Field(..., min_length=1, max_length=120)


class GitCommitRequest(BaseModel):
    task_id: str
    message: str = Field(..., min_length=1, max_length=500)


class GitPushRequest(BaseModel):
    task_id: str


class PRPrepareRequest(BaseModel):
    task_id: str
    title: Optional[str] = None
    body: Optional[str] = None


class ToolResult(BaseModel):
    tool: str
    success: bool
    output: str = ""
    data: Dict[str, Any] = Field(default_factory=dict)


class ConfigOut(BaseModel):
    """Non-secret configuration summary exposed to the frontend."""
    provider: str
    model: str
    configured: bool
    streaming_supported: bool
    repository_configured: bool
    # v0.6.0 — provider plugin system summary (optional, backward compatible).
    provider_manager_enabled: bool = False
    providers: Optional[Dict[str, Any]] = None


class ProviderCapabilityOut(BaseModel):
    """Capability flags for a provider (non-secret)."""
    streaming: bool = False
    tool_calling: bool = False
    code_editing: bool = True
    context_window: Optional[int] = None
    max_output: Optional[int] = None


class ProviderHealthOut(BaseModel):
    """Health metrics for a provider (non-secret)."""
    status: str = "unknown"
    last_success: Optional[str] = None
    last_error: Optional[str] = None
    last_error_message: Optional[str] = None
    error_count: int = 0
    success_count: int = 0
    request_count: int = 0
    avg_response_time_ms: Optional[float] = None


class ProviderInfoOut(BaseModel):
    """Public info for a single registered provider (no secrets)."""
    name: str
    display_name: str
    description: str
    capability: ProviderCapabilityOut
    requires_api_key: bool = False
    enabled: bool = True
    configurable: bool = True
    is_available: bool = True
    health: ProviderHealthOut
    fallback_for: List[str] = []


class ProviderManagerStatusOut(BaseModel):
    """Public snapshot of the provider manager (no secrets)."""
    active: Optional[str] = None
    available: List[str] = []
    fallback_chain: List[str] = []
    active_capability: Optional[ProviderCapabilityOut] = None
    active_health: Optional[ProviderHealthOut] = None
    providers: Dict[str, ProviderInfoOut] = {}


class ProviderActionRequest(BaseModel):
    """Request body for enable/disable/set-active actions."""
    name: str


# ── Authentication (v0.7.0) ────────────────────────────────────────────────
class UserOut(BaseModel):
    """Non-secret public user identity."""
    user_id: str
    username: str
    display_name: str
    is_guest: bool = True
    github_login: Optional[str] = None
    avatar_url: Optional[str] = None
    scopes: List[str] = Field(default_factory=list)


class LoginResponse(BaseModel):
    """Successful login response (includes the session token)."""
    session: str
    user: UserOut
    expires_in: int


class GuestLoginRequest(BaseModel):
    display_name: str = Field(default="Guest", max_length=80)


class GitHubLoginRequest(BaseModel):
    """Token-based GitHub login for the beta."""
    github_token: str = Field(..., min_length=1, max_length=400)


class SessionOut(BaseModel):
    """Current session info (no token — that's only in LoginResponse)."""
    authenticated: bool
    auth_enabled: bool
    user: Optional[UserOut] = None


# ── User settings (v0.7.0) ────────────────────────────────────────────────
class SettingsOut(BaseModel):
    """Non-secret user preferences exposed to the frontend."""
    theme: str = "shinobi"
    ai_provider: str = "local"
    default_workspace: str = ""
    terminal_preferences: Dict[str, Any] = Field(default_factory=dict)
    git_preferences: Dict[str, Any] = Field(default_factory=dict)
    auto_save: bool = True
    auto_commit: bool = False
    notifications: bool = True


class SettingsUpdate(BaseModel):
    """Partial settings update — all fields optional."""
    theme: Optional[str] = None
    ai_provider: Optional[str] = None
    default_workspace: Optional[str] = None
    terminal_preferences: Optional[Dict[str, Any]] = None
    git_preferences: Optional[Dict[str, Any]] = None
    auto_save: Optional[bool] = None
    auto_commit: Optional[bool] = None
    notifications: Optional[bool] = None


# ── Workspace Manager (v0.7.0) ────────────────────────────────────────────
class WorkspaceOut(BaseModel):
    name: str
    path: str
    is_default: bool = False
    is_git_repo: bool = False
    branch: Optional[str] = None
    file_count: int = 0
    size_bytes: int = 0
    last_modified: Optional[str] = None


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    repo: Optional[str] = None  # "owner/repo" to clone, else empty workspace


class WorkspaceRenameRequest(BaseModel):
    old_name: str = Field(..., min_length=1, max_length=120)
    new_name: str = Field(..., min_length=1, max_length=120)


class WorkspaceActionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


# ── Dashboard (v0.7.0) ────────────────────────────────────────────────────
class DashboardTaskItem(BaseModel):
    task_id: str
    description: str
    status: str
    created_at: str
    branch: Optional[str] = None


class SystemHealthComponent(BaseModel):
    name: str
    status: str  # ok | degraded | down | unknown
    detail: str = ""


class DashboardOut(BaseModel):
    recent_tasks: List[DashboardTaskItem] = Field(default_factory=list)
    active_tasks: List[DashboardTaskItem] = Field(default_factory=list)
    agent_status: str = "idle"
    workspace_status: Dict[str, Any] = Field(default_factory=dict)
    git_status: Dict[str, Any] = Field(default_factory=dict)
    provider_status: Dict[str, Any] = Field(default_factory=dict)
    system_health: List[SystemHealthComponent] = Field(default_factory=list)
    multi_agent_enabled: bool = False


# ── System health (v0.7.0) ────────────────────────────────────────────────
class SystemHealthOut(BaseModel):
    status: str  # ok | degraded | down
    version: str
    environment: str
    components: List[SystemHealthComponent] = Field(default_factory=list)
    startup_checks: List[Dict[str, Any]] = Field(default_factory=list)


# ── Autonomous Execution Engine (v0.8.0) ───────────────────────────────────

class TaskQueueItem(BaseModel):
    """Public representation of a task in the scheduler queue."""

    task_id: str
    description: str
    repo_full: str
    priority: int
    status: str
    retries: int
    max_retries: int
    enqueued_at: float
    started_at: Optional[float] = None
    error: Optional[str] = None


class QueueListOut(BaseModel):
    enabled: bool
    queue: List[TaskQueueItem] = Field(default_factory=list)
    queue_length: int = 0
    running_count: int = 0
    max_concurrency: int = 1


class QueueActionRequest(BaseModel):
    """Operator action on a queued task (pause/resume/cancel)."""

    task_id: str


class RetryRequest(BaseModel):
    """Manual retry of a task."""

    task_id: str
    priority: Optional[int] = None


class ReorderRequest(BaseModel):
    """Change a queued task's priority."""

    task_id: str
    priority: int


class WorkspaceSessionOut(BaseModel):
    """A persistent workspace session."""

    session_id: str
    repo_full: str
    branch: Optional[str] = None
    workspace: str
    state: str
    task_id: Optional[str] = None
    description: Optional[str] = None
    created_at: str
    last_active: str


class WorkspaceSessionListOut(BaseModel):
    sessions: List[WorkspaceSessionOut] = Field(default_factory=list)
    count: int = 0


class SessionCreateRequest(BaseModel):
    """Create or reuse a session for a repo."""

    repo_full: str
    workspace: str
    branch: Optional[str] = None
    task_id: Optional[str] = None
    description: Optional[str] = None


class SessionActionRequest(BaseModel):
    """Operate on a session by id."""

    session_id: str


class RecoverySummaryOut(BaseModel):
    interrupted_count: int
    interrupted_task_ids: List[str] = Field(default_factory=list)
    auto_resume: bool = False


class RecoveryActionRequest(BaseModel):
    """Act on an interrupted task."""

    task_id: str
    reason: Optional[str] = None


# ── v0.8.0 Job History models ──────────────────────────────────────────────
class HistoryEventPreview(BaseModel):
    type: str
    message: str
    timestamp: str


class HistoryItemOut(BaseModel):
    task_id: str
    description: str
    status: str
    repo: Optional[str] = None
    branch: Optional[str] = None
    created_at: str
    updated_at: str
    event_count: Optional[int] = None
    first_event_at: Optional[str] = None
    last_event_at: Optional[str] = None
    events: Optional[List[HistoryEventPreview]] = Field(default=None)


class HistoryListOut(BaseModel):
    items: List[HistoryItemOut] = Field(default_factory=list)
    count: int = 0
    limit: int = 100
    offset: int = 0
    filters: Dict[str, Any] = Field(default_factory=dict)


class HistoryDetailOut(BaseModel):
    task_id: str
    description: str
    status: str
    repo: Optional[str] = None
    branch: Optional[str] = None
    created_at: str
    updated_at: str
    events: List[EventOut] = Field(default_factory=list)
    event_count: int = 0


class HistoryStatsOut(BaseModel):
    total_tasks: int = 0
    by_status: Dict[str, int] = Field(default_factory=dict)
    by_repo: Dict[str, int] = Field(default_factory=dict)
    total_events: int = 0
