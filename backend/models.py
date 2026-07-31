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
