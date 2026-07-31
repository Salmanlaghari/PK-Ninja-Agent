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
