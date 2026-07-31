"""AI Provider Plugin System (v0.6.0).

This package implements a modular, pluggable provider architecture that sits
*on top* of the existing ``backend/ai_provider.py`` module. It adds:

* a :class:`ProviderCapability` descriptor (streaming, tool calling, code
  editing, context window, max output),
* a :class:`ProviderInfo` registry record,
* an enhanced :class:`ProviderProtocol` that extends the original
  ``AIProvider`` protocol with ``chat()``, ``review()`` and ``summarize()``
  while remaining structurally compatible with it,
* a :class:`ProviderManager` central registry with dynamic loading,
  enable/disable, capability detection, health monitoring and a fallback
  system,
* built-in adapters (Local, OpenAI-compatible, Gemini config-only, Mock) that
  wrap the existing provider classes so no current functionality is removed,
* server-side configuration via environment variables.

The original ``get_provider(settings)`` factory in ``ai_provider.py`` continues
to work unchanged. ``ProviderManager`` is an opt-in layer: when the manager is
not explicitly used, the agent loop falls back to ``get_provider`` exactly as
before.
"""

from .interface import (
    ProviderCapability,
    ProviderInfo,
    ProviderProtocol,
    ProviderStatus,
    ProviderHealth,
)
from .manager import (
    ProviderManager,
    get_manager,
    reset_manager,
    provider_manager_status,
)
from .local_provider import LocalAdapter
from .openai_provider import OpenAIAdapter
from .gemini_provider import GeminiAdapter
from .mock_provider import MockProvider, MockAdapter

__all__ = [
    "ProviderCapability",
    "ProviderInfo",
    "ProviderProtocol",
    "ProviderStatus",
    "ProviderHealth",
    "ProviderManager",
    "get_manager",
    "reset_manager",
    "provider_manager_status",
    "LocalAdapter",
    "OpenAIAdapter",
    "GeminiAdapter",
    "MockProvider",
    "MockAdapter",
]

__version__ = "0.6.0"
