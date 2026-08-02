"""Provider interface, capabilities and health data structures.

This module defines the contract for the plugin system. It deliberately keeps
backward compatibility with ``backend.ai_provider.AIProvider``: any object
satisfying the original protocol (``name``, ``plan``, ``edit``,
``analyze_error``, ``stream_chat``) also satisfies :class:`ProviderProtocol`
because the new methods (``chat``, ``review``, ``summarize``) are optional and
implemented as default no-ops on the manager's adapter wrappers.

Nothing here imports network libraries or performs I/O — it is pure data and
protocol definitions, safe to import in any environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable


# ── Capability descriptor ───────────────────────────────────────────────────
@dataclass
class ProviderCapability:
    """Static capability flags and limits for a provider.

    These are *declared* by the adapter (not measured at runtime) so that the
    manager can make routing and fallback decisions without making a network
    request. ``context_window`` / ``max_output`` of ``0`` mean "unknown" (the
    manager treats unknown as unbounded for routing purposes but reports it
    as ``None`` to consumers).
    """

    streaming: bool = False
    tool_calling: bool = False
    code_editing: bool = True
    context_window: int = 0
    max_output: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "streaming": self.streaming,
            "tool_calling": self.tool_calling,
            "code_editing": self.code_editing,
            "context_window": self.context_window or None,
            "max_output": self.max_output or None,
        }


# ── Provider status ─────────────────────────────────────────────────────────
class ProviderStatus(str, Enum):
    """Lifecycle / health status of a registered provider."""

    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    DISABLED = "disabled"


# ── Health record ───────────────────────────────────────────────────────────
@dataclass
class ProviderHealth:
    """Runtime health metrics tracked by the manager."""

    status: ProviderStatus = ProviderStatus.UNKNOWN
    last_success: Optional[datetime] = None
    last_error: Optional[datetime] = None
    last_error_message: str = ""
    error_count: int = 0
    success_count: int = 0
    total_response_time_ms: float = 0.0
    request_count: int = 0

    @property
    def avg_response_time_ms(self) -> Optional[float]:
        if self.request_count == 0:
            return None
        return round(self.total_response_time_ms / self.request_count, 2)

    def record_success(self, response_time_ms: float) -> None:
        self.success_count += 1
        self.request_count += 1
        self.total_response_time_ms += response_time_ms
        self.last_success = datetime.now(timezone.utc)
        # Recover from degraded if recent success.
        if self.status == ProviderStatus.DEGRADED:
            self.status = ProviderStatus.HEALTHY
        elif self.status == ProviderStatus.UNKNOWN:
            self.status = ProviderStatus.HEALTHY

    def record_failure(self, message: str = "") -> None:
        self.error_count += 1
        self.request_count += 1
        self.last_error = datetime.now(timezone.utc)
        self.last_error_message = (message or "")[:500]
        # Degrade after 3 consecutive-style errors, unhealthy after 5.
        if self.error_count >= 5:
            self.status = ProviderStatus.UNHEALTHY
        elif self.error_count >= 3:
            self.status = ProviderStatus.DEGRADED

    def reset(self) -> None:
        self.status = ProviderStatus.UNKNOWN
        self.last_success = None
        self.last_error = None
        self.last_error_message = ""
        self.error_count = 0
        self.success_count = 0
        self.total_response_time_ms = 0.0
        self.request_count = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "last_error": self.last_error.isoformat() if self.last_error else None,
            "last_error_message": self.last_error_message or None,
            "error_count": self.error_count,
            "success_count": self.success_count,
            "request_count": self.request_count,
            "avg_response_time_ms": self.avg_response_time_ms,
        }


# ── Provider info (registry record) ─────────────────────────────────────────
@dataclass
class ProviderInfo:
    """Metadata + live state for a registered provider."""

    name: str
    display_name: str
    description: str
    capability: ProviderCapability
    requires_api_key: bool = False
    enabled: bool = True
    configurable: bool = True  # False for built-ins that can't be removed
    health: ProviderHealth = field(default_factory=ProviderHealth)
    # Optional fallback target name (set by manager based on capability match).
    fallback_for: List[str] = field(default_factory=list)

    @property
    def is_available(self) -> bool:
        """A provider is available if enabled and not disabled/unhealthy-beyond-repair."""
        return self.enabled and self.health.status != ProviderStatus.DISABLED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "capability": self.capability.to_dict(),
            "requires_api_key": self.requires_api_key,
            "enabled": self.enabled,
            "configurable": self.configurable,
            "health": self.health.to_dict(),
            "is_available": self.is_available,
            "fallback_for": list(self.fallback_for),
        }


# ── Enhanced protocol ───────────────────────────────────────────────────────
# We import lazily to avoid a hard dependency cycle with ai_provider.
try:
    from ai_provider import Plan, ChatMessage, ChatResult  # type: ignore
except Exception:  # pragma: no cover - fallback for isolated imports
    Plan = Any  # type: ignore
    ChatMessage = Any  # type: ignore
    ChatResult = Any  # type: ignore


@runtime_checkable
class ProviderProtocol(Protocol):
    """Enhanced provider contract.

    Extends the original ``AIProvider`` protocol with optional ``chat``,
    ``review`` and ``summarize`` methods. Existing providers that only
    implement ``plan``/``edit``/``analyze_error``/``stream_chat`` remain
    compatible because the manager's adapter wrappers provide sensible
    defaults for the new methods.
    """

    name: str

    def plan(self, task: str, context: str) -> Any: ...
    def edit(self, task: str, plan: Any, files: List[dict]) -> List[dict]: ...
    def analyze_error(self, task: str, error: str, files: List[dict]) -> str: ...
    def stream_chat(
        self,
        messages: List[Any],
        on_token: Optional[Callable[[str], None]] = None,
    ) -> Any: ...

    # New optional methods (defaults provided by adapters):
    def chat(self, messages: List[Any], *, stream: bool = False) -> Any: ...
    def review(self, task: str, files: List[dict]) -> str: ...
    def summarize(self, text: str, max_length: int = 200) -> str: ...
