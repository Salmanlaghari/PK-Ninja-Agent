"""Mock provider — deterministic, in-memory, for tests and dry runs.

The MockProvider is a *real* test double: it produces genuine, verifiable
output (plans, edits, chat results) without any network or API key. It is
configurable to simulate success, failure, latency, and capability flags so
the manager's health monitoring and fallback logic can be exercised
deterministically.

It is distinct from ``LocalProvider``: Local produces real edits against real
file contents; Mock returns canned responses useful for asserting manager
behaviour (e.g. forcing failures to trigger fallback).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from .interface import ProviderCapability

from ai_provider import Plan, ChatMessage, ChatResult


@dataclass
class MockConfig:
    """Behaviour knobs for the MockProvider."""

    fail_next: int = 0  # number of subsequent calls to fail
    fail_message: str = "mock failure"
    latency_ms: float = 0.0
    plan_summary: str = "Mock plan."
    plan_steps: List[str] = field(
        default_factory=lambda: ["Mock step one.", "Mock step two."]
    )
    chat_text: str = "Mock chat response."
    review_text: str = "Mock review: looks fine."
    summary_text: str = "Mock summary."
    edits: List[dict] = field(default_factory=list)
    capability: ProviderCapability = field(default_factory=ProviderCapability)
    name: str = "mock"


class MockProvider:
    """In-memory mock provider implementing the full :class:`ProviderProtocol`.

    ``MockAdapter`` is provided as an alias for registry symmetry with the
    other adapters; both point to this class.
    """

    def __init__(self, settings: Optional[Any] = None, config: Optional[MockConfig] = None) -> None:
        self.settings = settings
        self.config = config or MockConfig()
        self.name = self.config.name
        self.capability = self.config.capability
        self.display_name = f"Mock ({self.name})"
        self.description = "Deterministic in-memory provider for tests and dry runs."
        self.requires_api_key = False
        self._call_log: List[str] = []

    # ── internal helpers ────────────────────────────────────────────────
    def _maybe_sleep(self) -> None:
        if self.config.latency_ms > 0:
            time.sleep(self.config.latency_ms / 1000.0)

    def _should_fail(self) -> bool:
        if self.config.fail_next > 0:
            self.config.fail_next -= 1
            return True
        return False

    def _log(self, method: str) -> None:
        self._call_log.append(method)

    @property
    def call_log(self) -> List[str]:
        return list(self._call_log)

    # ── protocol methods ────────────────────────────────────────────────
    def plan(self, task: str, context: str) -> Plan:
        self._log("plan")
        self._maybe_sleep()
        if self._should_fail():
            raise RuntimeError(self.config.fail_message)
        return Plan(summary=self.config.plan_summary, steps=list(self.config.plan_steps))

    def edit(self, task: str, plan: Plan, files: List[dict]) -> List[dict]:
        self._log("edit")
        self._maybe_sleep()
        if self._should_fail():
            raise RuntimeError(self.config.fail_message)
        # Default: return canned edits, or echo file contents unchanged.
        if self.config.edits:
            return [dict(e) for e in self.config.edits]
        return []

    def analyze_error(self, task: str, error: str, files: List[dict]) -> str:
        self._log("analyze_error")
        if self._should_fail():
            raise RuntimeError(self.config.fail_message)
        return f"Mock analysis of: {error[:120]}"

    def stream_chat(
        self,
        messages: List[ChatMessage],
        on_token: Optional[Callable[[str], None]] = None,
    ) -> ChatResult:
        self._log("stream_chat")
        self._maybe_sleep()
        if self._should_fail():
            raise RuntimeError(self.config.fail_message)
        text = self.config.chat_text
        if on_token:
            for word in text.split(" "):
                on_token(word + " ")
        return ChatResult(text=text, model=self.name)

    # ── new enhanced methods ────────────────────────────────────────────
    def chat(self, messages: List[ChatMessage], *, stream: bool = False) -> ChatResult:
        self._log("chat")
        self._maybe_sleep()
        if self._should_fail():
            raise RuntimeError(self.config.fail_message)
        return ChatResult(text=self.config.chat_text, model=self.name)

    def review(self, task: str, files: List[dict]) -> str:
        self._log("review")
        if self._should_fail():
            raise RuntimeError(self.config.fail_message)
        return self.config.review_text

    def summarize(self, text: str, max_length: int = 200) -> str:
        self._log("summarize")
        if self._should_fail():
            raise RuntimeError(self.config.fail_message)
        return self.config.summary_text[:max_length]


# Registry alias for symmetry with the other adapter modules.
MockAdapter = MockProvider
