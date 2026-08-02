"""Gemini adapter — configuration-only wrapper around GeminiProvider.

The existing ``GeminiProvider`` in ``backend/ai_provider.py`` is a thin subclass
of ``OpenAIProvider`` that routes through Google's OpenAI-compatible endpoint
(``generativelanguage.googleapis.com/v1beta/openai/v1``). This adapter exposes
it through the plugin interface.

We deliberately do **not** implement any Gemini-native (generateContent /
streamGenerateContent) API here. The user's constraint is explicit: do not use
unsupported APIs. We rely solely on the OpenAI-compatible shim that Google
already publishes, which is the same approach the existing codebase takes.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from .interface import ProviderCapability

from ai_provider import (
    GeminiProvider,
    Plan,
    ChatMessage,
    ChatResult,
    AIError,
)


class GeminiAdapter:
    """Configuration-only adapter for Gemini via Google's OpenAI-compatible endpoint.

    No Gemini-native API is used. Streaming and tool calling capabilities are
    inherited from the OpenAI-compatible shim; we declare them conservatively
    (tool_calling=True because the shim supports it, but the manager only
    advertises it — actual usage is opt-in by callers).
    """

    name = "gemini"
    display_name = "Gemini (OpenAI-compatible endpoint)"
    description = (
        "Routes to Google Gemini through its OpenAI-compatible endpoint. "
        "Requires GEMINI_API_KEY (or AI_API_KEY). No Gemini-native API is "
        "used — only the published OpenAI shim. Configuration only."
    )
    capability = ProviderCapability(
        streaming=True,
        tool_calling=True,
        code_editing=True,
        context_window=0,
        max_output=0,
    )
    requires_api_key = True

    def __init__(self, settings: Optional[Any] = None) -> None:
        self.settings = settings
        self._inner: Optional[GeminiProvider] = None
        self._init_error: Optional[str] = None
        try:
            self._inner = GeminiProvider(settings)
        except AIError as exc:
            self._init_error = str(exc)

    @property
    def _provider(self) -> GeminiProvider:
        if self._inner is None:
            raise AIError(self._init_error or "Gemini provider not initialised.")
        return self._inner

    # ── Original protocol methods ───────────────────────────────────────
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

    # ── New enhanced methods ────────────────────────────────────────────
    def chat(self, messages: List[ChatMessage], *, stream: bool = False) -> ChatResult:
        if stream:
            return self._provider.stream_chat(messages)
        text = self._provider.generate(messages)
        return ChatResult(text=text, model=getattr(self._provider, "model", "gemini"))

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
