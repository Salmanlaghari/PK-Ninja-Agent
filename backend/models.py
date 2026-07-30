"""Pydantic models and enums shared across the agent, API, and event system."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────────
class EventType(str, Enum):
    session_started = "session_started"
    analyzing = "analyzing"
    searching = "searching"
    file_read = "file_read"
    planning = "planning"
    editing = "editing"
    command_started = "command_started"
    command_output = "command_output"
    command_finished = "command_finished"
    test_started = "test_started"
    test_finished = "test_finished"
    error = "error"
    fixing = "fixing"
    completed = "completed"
    info = "info"


class TaskStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


# ── API request / response models ─────────────────────────────────────────
class TaskCreate(BaseModel):
    description: str = Field(..., min_length=1, max_length=4000)
    repository: Optional[str] = None  # "owner/repo" override; else uses config


class TaskSummary(BaseModel):
    task_id: str
    description: str
    status: TaskStatus
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
