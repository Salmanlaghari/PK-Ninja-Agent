"""Mistral AI adapter — configuration-only wrapper.

Mistral provides an OpenAI-compatible API at https://api.mistral.ai.
This adapter wraps the MistralProvider for use with the ProviderManager.
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


class MistralAdapter:
    """Adapter for Mistral AI via OpenAI-compatible API."""

    name = "mistral"
    display_name = "Mistral AI"
    description = (
        "Mistral AI via OpenAI-compatible API "
        "(https://api.mistral.ai/v1). "
        "Requires AI_API_KEY. Supports streaming."
    )
    capability = ProviderCapability(
        streaming=True,
        tool_calling=False,
        code_editing=True,
        context_window=32768,
        max_output=4096,
    )
    requires_api_key = True

    def __init__(self, settings: Optional[Any] = None) -> None:
        self._settings = settings
        self._inner: Optional[OpenAIProvider] = None
        self._init_error: Optional[str] = None
        try:
            if settings is not None:
                api_key = getattr(settings, "ai_api_key", "") or ""
                model = getattr(settings, "ai_model", "") or "mistral-small-latest"
                if api_key:
                    self._inner = OpenAIProvider(
                        settings,
                        provider_hint="mistral",
                    )
                    # Override the base URL for Mistral
                    self._inner._base_url = "https://api.mistral.ai/v1"
        except Exception as exc:
            self._init_error = str(exc)

    def plan(self, task: str, context: str = "", **kw: Any) -> Plan:
        if self._inner is None:
            raise AIError(f"Mistral not configured: {self._init_error or 'no API key'}")
        return self._inner.plan(task, context, **kw)

    def edit(self, task: str, plan: Plan, files: List[dict], **kw: Any) -> List[dict]:
        if self._inner is None:
            raise AIError(f"Mistral not configured: {self._init_error or 'no API key'}")
        return self._inner.edit(task, plan, files, **kw)

    def analyze_error(self, task: str, error: str, files: List[dict], **kw: Any) -> str:
        if self._inner is None:
            raise AIError(f"Mistral not configured: {self._init_error or 'no API key'}")
        return self._inner.analyze_error(task, error, files, **kw)

    def stream_chat(self, messages: List[ChatMessage],
                    on_token: Optional[Callable[[str], None]] = None, **kw: Any) -> ChatResult:
        if self._inner is None:
            raise AIError(f"Mistral not configured: {self._init_error or 'no API key'}")
        return self._inner.stream_chat(messages, on_token, **kw)

    def chat(self, messages: List[ChatMessage], *, stream: bool = False, **kw: Any) -> ChatResult:
        if self._inner is None:
            raise AIError(f"Mistral not configured: {self._init_error or 'no API key'}")
        return self._inner.chat(messages, stream=stream, **kw)

    def review(self, task: str, files: List[dict], **kw: Any) -> str:
        if self._inner is None:
            raise AIError(f"Mistral not configured: {self._init_error or 'no API key'}")
        return self._inner.review(task, files, **kw)

    def summarize(self, text: str, max_length: int = 200, **kw: Any) -> str:
        if self._inner is None:
            raise AIError(f"Mistral not configured: {self._init_error or 'no API key'}")
        return self._inner.summarize(text, max_length, **kw)


__all__ = ["MistralAdapter"]
