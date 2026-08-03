"""Xiaomi MiMo adapter — configuration-only wrapper around OpenAIProvider.

Xiaomi MiMo provides an OpenAI-compatible endpoint at
https://api.xiaomimimo.com/v1. This adapter routes through that endpoint
using the existing OpenAIProvider infrastructure.
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


class XiaomiAdapter:
    """Configuration-only adapter for Xiaomi MiMo via its OpenAI-compatible endpoint."""

    name = "xiaomi"
    display_name = "Xiaomi MiMo"
    description = (
        "Xiaomi MiMo via its OpenAI-compatible endpoint "
        "(https://api.xiaomimimo.com/v1). "
        "Requires MIMO_API_KEY (or AI_API_KEY). Supports streaming and tool calling."
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
        self._inner: Optional[OpenAIProvider] = None
        self._init_error: Optional[str] = None
        try:
            self._inner = OpenAIProvider(settings, provider_hint="xiaomi")
        except AIError as exc:
            self._init_error = str(exc)

    @property
    def _provider(self) -> OpenAIProvider:
        if self._inner is None:
            raise AIError(self._init_error or "Xiaomi provider not initialised.")
        return self._inner

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

    def chat(self, messages: List[ChatMessage], *, stream: bool = False) -> ChatResult:
        if stream:
            return self._provider.stream_chat(messages)
        text = self._provider.generate(messages)
        return ChatResult(text=text, model=getattr(self._provider, "model", "xiaomi"))

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
