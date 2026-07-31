"""OpenAI-compatible adapter — wraps the existing OpenAIProvider.

Exposes any OpenAI-compatible Chat Completions endpoint through the plugin
interface. Capability detection is declared honestly: streaming is supported
(SSE), tool calling is supported by the OpenAI API surface (declared but only
used if the caller opts in), code editing is supported, and the context window
is reported as unknown (0) because it varies per model.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from .interface import ProviderCapability

from ai_provider import (
    OpenAIProvider,
    Plan,
    ChatMessage,
    ChatResult,
    AIError,
)


class OpenAIAdapter:
    """Adapter for any OpenAI-compatible endpoint."""

    name = "openai"
    display_name = "OpenAI-compatible"
    description = (
        "Adapter for any OpenAI-compatible Chat Completions REST endpoint "
        "(OpenAI, DeepSeek, Together, OpenRouter, Ollama, etc.). Requires an "
        "API key. Supports SSE streaming and tool calling."
    )
    capability = ProviderCapability(
        streaming=True,
        tool_calling=True,
        code_editing=True,
        context_window=0,   # model-dependent
        max_output=0,
    )
    requires_api_key = True

    def __init__(self, settings: Optional[Any] = None) -> None:
        # Defer construction error to the manager's health check; but if a key
        # is present, construct now so plan/edit/stream work immediately.
        self.settings = settings
        self._inner: Optional[OpenAIProvider] = None
        self._init_error: Optional[str] = None
        try:
            self._inner = OpenAIProvider(settings)
        except AIError as exc:
            self._init_error = str(exc)

    @property
    def _provider(self) -> OpenAIProvider:
        if self._inner is None:
            raise AIError(self._init_error or "OpenAI provider not initialised.")
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
        return ChatResult(text=text, model=getattr(self._provider, "model", "openai"))

    def review(self, task: str, files: List[dict]) -> str:
        files_brief = "\n".join(f["path"] for f in files[:30])
        messages = [
            ChatMessage(
                "system",
                "You are a code reviewer. Given a task and a list of changed "
                "file paths, provide a concise review (risks, suggestions). "
                "Be specific and short.",
            ),
            ChatMessage("user", f"Task:\n{task}\n\nFiles:\n{files_brief}"),
        ]
        return self._provider.generate(messages).strip()

    def summarize(self, text: str, max_length: int = 200) -> str:
        messages = [
            ChatMessage(
                "system",
                f"Summarize the following text in at most {max_length} "
                "characters. Return only the summary.",
            ),
            ChatMessage("user", text[:6000]),
        ]
        return self._provider.generate(messages).strip()
