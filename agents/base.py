"""Core abstractions for the multi-agent architecture.

Everything here is provider-independent and intentionally minimal so the
specialized agents can be unit-tested with a stub provider or with no provider
at all (deterministic agents like Terminal/Git don't need an LLM).

Design rules enforced by this module:
  * Agents communicate through ``AgentMessage`` (structured, typed) only.
  * Agents return ``AgentResult`` (success flag + structured payload).
  * No agent imports a concrete provider; they take an object that conforms to
    the ``AIProvider`` protocol from ``ai_provider.py``.
  * Security is inherited: every agent that touches the filesystem or runs
    commands goes through the existing ``Workspace`` / ``terminal.run_command``
    layer, which keeps path-traversal and command-injection protections.
"""
from __future__ import annotations

import datetime as _dt
import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Protocol

log = logging.getLogger("pk_ninja.agents")


# ── Agent roles ──────────────────────────────────────────────────────────────
class AgentRole(str, Enum):
    """The seven specialized agents in the architecture."""

    planner = "planner"
    repository = "repository"
    coding = "coding"
    terminal = "terminal"
    testing = "testing"
    git = "git"
    review = "review"
    coordinator = "coordinator"  # the orchestrator itself


# ── Message roles (who authored a message) ───────────────────────────────────
class MessageRole(str, Enum):
    user = "user"
    agent = "agent"
    coordinator = "coordinator"
    system = "system"


class MessagePriority(str, Enum):
    """Routing priority so the coordinator can prioritize fixes over new work."""

    normal = "normal"
    high = "high"  # e.g. a test failure that needs a fix
    urgent = "urgent"  # e.g. an unsafe command was rejected


# ── Structured inter-agent message ───────────────────────────────────────────
@dataclass
class AgentMessage:
    """The only way agents talk to each other.

    Every message is structured and typed. There is no free-form agent chatter;
    a message always carries a sender, recipient, a textual content, and an
    optional structured payload (e.g. a list of files, a diff, a test report).
    """

    sender: AgentRole
    recipient: AgentRole
    content: str
    role: MessageRole = MessageRole.agent
    priority: MessagePriority = MessagePriority.normal
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: _dt.datetime = field(default_factory=_dt.datetime.utcnow)
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "sender": self.sender.value,
            "recipient": self.recipient.value,
            "content": self.content,
            "role": self.role.value,
            "priority": self.priority.value,
            "payload": self.payload,
            "timestamp": self.timestamp.isoformat() + "Z",
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AgentMessage":
        return cls(
            sender=AgentRole(d["sender"]),
            recipient=AgentRole(d["recipient"]),
            content=d["content"],
            role=MessageRole(d.get("role", "agent")),
            priority=MessagePriority(d.get("priority", "normal")),
            payload=d.get("payload", {}),
        )


# ── Agent result ─────────────────────────────────────────────────────────────
@dataclass
class AgentResult:
    """Structured return value from every agent action.

    ``success`` is the only field an agent MUST set. ``data`` carries the
    structured payload the coordinator/next agent reads (e.g. a plan, a list
    of files, a diff, a test report). ``next_agent`` is an optional hint the
    coordinator MAY honor to short-circuit routing.
    """

    success: bool
    agent: AgentRole
    data: Dict[str, Any] = field(default_factory=dict)
    messages: List[AgentMessage] = field(default_factory=list)
    next_agent: Optional[AgentRole] = None
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "agent": self.agent.value,
            "data": self.data,
            "messages": [m.to_dict() for m in self.messages],
            "next_agent": self.next_agent.value if self.next_agent else None,
            "summary": self.summary,
        }


# ── Shared execution context ─────────────────────────────────────────────────
@dataclass
class AgentContext:
    """Shared mutable context handed to every agent by the coordinator.

    It bundles the pieces an agent needs: the task description, the workspace,
    the settings, an optional AI provider, and a thread-safe ``emit`` callback
    so agents stream real events into the existing EventBus/UI.

    Keeping this in one object means agents never reach into globals and the
    coordinator can inject test doubles trivially.
    """

    task_id: str
    description: str
    workspace: Any  # workspace.Workspace, kept as Any to avoid hard import
    settings: Any  # config.Settings
    provider: Any = None  # conforms to ai_provider.AIProvider protocol
    emit: Optional[Callable[..., None]] = None
    # The coordinator populates these as agents produce output, so later
    # agents (e.g. Review) can read what earlier agents (e.g. Coding) did.
    plan: List[Dict[str, Any]] = field(default_factory=list)
    relevant_files: List[Dict[str, Any]] = field(default_factory=list)
    edits: List[Dict[str, Any]] = field(default_factory=list)
    test_report: Dict[str, Any] = field(default_factory=dict)
    review: Dict[str, Any] = field(default_factory=dict)
    # Per-agent scratch pad (free-form, keyed by AgentRole.value).
    scratch: Dict[str, Any] = field(default_factory=dict)
    # A cancel flag the coordinator checks between agents.
    cancel: Any = None  # threading.Event-like

    def is_cancelled(self) -> bool:
        return bool(self.cancel is not None and self.cancel.is_set())

    def emit_event(self, etype: Any, message: str, **data: Any) -> None:
        """Stream a real event into the existing UI if an emit callback exists."""
        if self.emit:
            try:
                self.emit(etype, message, **data)
            except Exception:  # pragma: no cover - never let UI break an agent
                log.exception("emit callback failed")


# ── The agent protocol/base class ─────────────────────────────────────────────
class BaseAgent:
    """Abstract base for all specialized agents.

    Subclasses implement :meth:`handle` which receives an :class:`AgentContext`
    plus an :class:`AgentMessage` and returns an :class:`AgentResult`.

    The base class standardizes:
      * ``role`` — which :class:`AgentRole` this agent plays.
      * ``name`` — human-readable name for UI/logs.
      * logging + cancellation guard around ``handle``.
      * a helper to build an :class:`AgentMessage` reply.
    """

    role: AgentRole = AgentRole.coordinator
    name: str = "base"

    def __init__(self) -> None:
        self.log = logging.getLogger(f"pk_ninja.agents.{self.role.value}")

    # Public entry point — subclasses should NOT override this.
    def run(self, ctx: AgentContext, message: AgentMessage) -> AgentResult:
        if ctx.is_cancelled():
            return AgentResult(
                success=False,
                agent=self.role,
                summary="cancelled before start",
            )
        self.log.debug("agent %s handling message from %s", self.role.value, message.sender.value)
        try:
            result = self.handle(ctx, message)
        except Exception as exc:  # surface honestly, never fake success
            self.log.exception("agent %s failed", self.role.value)
            return AgentResult(
                success=False,
                agent=self.role,
                summary=f"{self.name} failed: {exc}",
                data={"error": str(exc)},
            )
        if not isinstance(result, AgentResult):
            raise TypeError(f"{self.name}.handle must return AgentResult, got {type(result)}")
        return result

    # Subclasses implement this.
    def handle(self, ctx: AgentContext, message: AgentMessage) -> AgentResult:
        raise NotImplementedError

    # ── helpers ──────────────────────────────────────────────────────────────
    def reply(self, recipient: AgentRole, content: str, **payload: Any) -> AgentMessage:
        return AgentMessage(
            sender=self.role,
            recipient=recipient,
            content=content,
            payload=payload,
        )


class AgentProtocol(Protocol):
    """Structural protocol every agent satisfies (for type-checkers)."""

    role: AgentRole
    name: str

    def run(self, ctx: AgentContext, message: AgentMessage) -> AgentResult: ...


def get_runtime_for_ctx(ctx: AgentContext):
    """Return the TaskRuntime for the given context, or None.

    Shared utility so terminal_agent and testing_agent don't duplicate this.
    """
    try:
        from agent import get_runtime
        rt = get_runtime(ctx.task_id)
        if rt is not None:
            return rt
    except Exception:
        pass
    return None
