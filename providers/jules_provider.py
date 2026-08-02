"""Jules adapter — official Google Jules async coding-agent provider (v1.1.0).

This adapter exposes the rewritten :class:`JulesProvider` (in
``backend/ai_provider.py``) through the PK-Ninja-Agent provider plugin
interface. ``JulesProvider`` now talks to the *official* Jules REST API at
``https://jules.googleapis.com/v1alpha`` using the ``x-goog-api-key`` header
and the async session model (create session → poll state → list activities
→ collect artifacts), rather than the previous incorrect OpenAI-compatible
stub.

The adapter is a thin wrapper (same pattern as ``GeminiAdapter`` /
``OpenAIAdapter``): it constructs the inner provider lazily, captures
initialisation errors so a missing key never breaks startup, and delegates
the full :class:`ProviderProtocol` surface (plan, edit, analyze_error,
stream_chat, chat, review, summarize) to it.

Security: the API key is never exposed through this adapter's public
attributes used by the manager's ``to_dict`` / ``status`` endpoints.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from .interface import ProviderCapability

from ai_provider import (
    JulesProvider,
    Plan,
    ChatMessage,
    ChatResult,
    AIError,
)


class JulesAdapter:
    """Plugin adapter for the official Google Jules async coding agent.

    Capabilities are declared conservatively: Jules supports code editing
    (it produces real git patches / change sets) and we expose streaming via
    an *emulated* chunked delivery (the official API has no SSE endpoint).
    Tool calling is reported as ``True`` because Jules autonomously runs
    tools (file edits, shell commands) within a session.
    """

    name = "jules"
    display_name = "Jules (official async coding agent)"
    description = (
        "Google Jules via the official REST API "
        "(https://jules.googleapis.com/v1alpha, x-goog-api-key auth). "
        "Async session-based coding agent: creates a session, polls to "
        "completion, collects activities & artifacts (git patches, bash "
        "output). Requires JULES_API_KEY (or AI_API_KEY / GEMINI_API_KEY). "
        "Streaming is emulated (chunked delivery) — Jules has no SSE endpoint."
    )
    capability = ProviderCapability(
        streaming=True,        # emulated chunked delivery
        tool_calling=True,     # Jules runs tools autonomously in-session
        code_editing=True,     # produces real git patches / change sets
        context_window=0,      # Jules manages context internally
        max_output=0,
    )
    requires_api_key = True

    def __init__(self, settings: Optional[Any] = None) -> None:
        self.settings = settings
        self._inner: Optional[JulesProvider] = None
        self._init_error: Optional[str] = None
        try:
            self._inner = JulesProvider(settings)
        except AIError as exc:
            self._init_error = str(exc)

    @property
    def _provider(self) -> JulesProvider:
        if self._inner is None:
            raise AIError(self._init_error or "Jules provider not initialised.")
        return self._inner

    # ── Original protocol methods ─────────────────────────────────────────
    def plan(self, task: str, context: str) -> Plan:
        return self._provider.plan(task, context)

    def edit(self, task: str, plan: Plan, files: List[dict]) -> List[dict]:
        return self._provider.edit(task, plan, files)

    def analyze_error(self, task: str, error: str, files: List[dict]) -> str:
        return self._provider.analyze_error(task, error, files)

    def stream_chat(
        self,
        messages: List[ChatMessage],
        on_token: Optional[Callable[[str], None]] = None,
    ) -> ChatResult:
        return self._provider.stream_chat(messages, on_token)

    # ── New enhanced methods ──────────────────────────────────────────────
    def chat(self, messages: List[ChatMessage], *, stream: bool = False) -> ChatResult:
        if stream:
            return self._provider.stream_chat(messages)
        text = self._provider.generate(messages)
        return ChatResult(text=text, model=getattr(self._provider, "model", "jules"))

    def review(self, task: str, files: List[dict]) -> str:
        files_brief = "\n".join(f["path"] for f in files[:30])
        messages = [
            ChatMessage(
                "system",
                "You are a code reviewer. Provide a concise review of the "
                "changes (risks, suggestions). Be specific and short.",
            ),
            ChatMessage("user", f"Task:\n{task}\n\nFiles:\n{files_brief}"),
        ]
        return self._provider.generate(messages).strip()

    def summarize(self, text: str, max_length: int = 200) -> str:
        messages = [
            ChatMessage(
                "system",
                f"Summarize the following in at most {max_length} characters. "
                "Return only the summary.",
            ),
            ChatMessage("user", text[:6000]),
        ]
        return self._provider.generate(messages).strip()

    # ── Diagnostics (non-secret) ──────────────────────────────────────────
    def diagnostics(self) -> dict:
        """Return a non-secret diagnostics/metrics snapshot from the inner provider."""
        inner = self._inner
        if inner is None:
            return {"initialised": False, "error": self._init_error}
        return {"initialised": True, **inner.diagnostics_summary()}
