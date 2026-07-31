"""Local adapter — wraps the existing offline LocalProvider.

The LocalProvider in ``backend/ai_provider.py`` is deterministic, offline and
needs no API key. This adapter exposes it through the enhanced
:class:`ProviderProtocol` and declares its capabilities honestly: it supports
code editing and a form of streaming (word-by-word), but does *not* support
tool calling and has no real context window (we report 0 / unknown).
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from .interface import ProviderCapability

# Import the existing provider — never reimplement its logic.
from ai_provider import LocalProvider, Plan, ChatMessage, ChatResult


class LocalAdapter:
    """Adapter that exposes ``LocalProvider`` through the plugin interface.

    Adds ``chat()``, ``review()`` and ``summarize()`` with deterministic
    implementations derived from the existing ``plan``/``analyze_error`` logic
    so the local provider remains fully functional offline.
    """

    name = "local"
    display_name = "Local (offline, deterministic)"
    description = (
        "Deterministic, fully offline provider. Inspects real repository "
        "contents and returns concrete plans/edits for automatable tasks. "
        "No API key required. This is the safe default fallback."
    )
    capability = ProviderCapability(
        streaming=True,       # word-by-word simulated streaming
        tool_calling=False,
        code_editing=True,
        context_window=0,     # unknown / unbounded
        max_output=0,
    )
    requires_api_key = False

    def __init__(self, settings: Optional[Any] = None) -> None:
        # LocalProvider takes no settings; we keep settings for interface parity.
        self.settings = settings
        self._inner = LocalProvider()

    # ── Original protocol methods (delegated) ───────────────────────────
    def plan(self, task: str, context: str) -> Plan:
        return self._inner.plan(task, context)

    def edit(self, task: str, plan: Plan, files: List[dict]) -> List[dict]:
        return self._inner.edit(task, plan, files)

    def analyze_error(self, task: str, error: str, files: List[dict]) -> str:
        return self._inner.analyze_error(task, error, files)

    def stream_chat(
        self,
        messages: List[ChatMessage],
        on_token: Optional[Callable[[str], None]] = None,
    ) -> ChatResult:
        return self._inner.stream_chat(messages, on_token)

    # ── New enhanced methods ────────────────────────────────────────────
    def chat(self, messages: List[ChatMessage], *, stream: bool = False) -> ChatResult:
        """Chat via the local provider's stream_chat (deterministic)."""
        return self._inner.stream_chat(messages)

    def review(self, task: str, files: List[dict]) -> str:
        """Deterministic review: check for obvious issues in changed files."""
        findings: List[str] = []
        for f in files:
            path = f.get("path", "")
            content = f.get("content", "")
            if not content.strip():
                findings.append(f"`{path}` is empty.")
                continue
            if content.startswith(" ") or content.startswith("\t"):
                findings.append(f"`{path}` starts with unexpected indentation.")
            if "TODO" in content or "FIXME" in content:
                findings.append(f"`{path}` still contains TODO/FIXME markers.")
        if not findings:
            return "No obvious issues detected by the local reviewer."
        return "Local review findings:\n- " + "\n- ".join(findings)

    def summarize(self, text: str, max_length: int = 200) -> str:
        """Trivial extractive summary: first sentence(s) up to max_length."""
        if not text:
            return ""
        # Take up to two sentences, capped at max_length.
        import re
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        summary = ""
        for s in sentences:
            if len(summary) + len(s) > max_length:
                break
            summary = (summary + " " + s).strip()
        return summary or text[:max_length]
