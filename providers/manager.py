"""Provider Manager — central registry, health monitoring and fallback.

The :class:`ProviderManager` is the heart of the plugin system. It:

* maintains a registry of provider *classes* (adapters),
* dynamically instantiates providers (deferred until first use, so a missing
  API key does not break startup),
* tracks per-provider health (status, last success, error count, avg response
  time),
* detects capabilities and routes calls to a compatible provider,
* implements a fallback chain: if the active provider fails, the manager
  transparently retries on the next compatible, enabled, healthy provider.

The manager is **opt-in** and **non-breaking**: when not used, the existing
``get_provider(settings)`` factory continues to work unchanged. The manager
simply wraps that factory's output for backward compatibility.

Security: provider configuration is read from server-side settings only.
API keys are never exposed via the manager's public ``to_dict`` / status
endpoints.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional, Type

from .interface import (
    ProviderCapability,
    ProviderInfo,
    ProviderStatus,
)

logger = logging.getLogger(__name__)

# Lazy import of ai_provider symbols (avoid circular import at package import).
def _ai_symbols():
    import ai_provider
    return ai_provider


# ── Adapter factory registry ────────────────────────────────────────────────
# Maps provider name -> adapter class + capability metadata.
# Built-ins are registered here; external plugins call register_adapter().
_ADAPTERS: Dict[str, Dict[str, Any]] = {}


def register_adapter(
    name: str,
    adapter_cls: Type,
    *,
    display_name: str = "",
    description: str = "",
    capability: Optional[ProviderCapability] = None,
    requires_api_key: bool = False,
    configurable: bool = True,
) -> None:
    """Register a provider adapter class under ``name``.

    This is the public extension point: third-party packages call
    ``register_adapter("myprovider", MyAdapter, ...)`` to plug into the
    manager without modifying this file.
    """
    cap = capability or getattr(adapter_cls, "capability", ProviderCapability())
    _ADAPTERS[name] = {
        "cls": adapter_cls,
        "display_name": display_name or getattr(adapter_cls, "display_name", name),
        "description": description or getattr(adapter_cls, "description", ""),
        "capability": cap,
        "requires_api_key": requires_api_key or getattr(adapter_cls, "requires_api_key", False),
        "configurable": configurable,
    }


def _register_builtins() -> None:
    """Register the four built-in adapters."""
    if _ADAPTERS:
        return  # already registered
    from .local_provider import LocalAdapter
    from .openai_provider import OpenAIAdapter
    from .gemini_provider import GeminiAdapter
    from .jules_provider import JulesAdapter
    from .mock_provider import MockProvider

    register_adapter(
        "local", LocalAdapter,
        display_name="Local (offline, deterministic)",
        description=LocalAdapter.description,
        capability=LocalAdapter.capability,
        requires_api_key=False,
        configurable=False,
    )
    register_adapter(
        "openai", OpenAIAdapter,
        display_name="OpenAI-compatible",
        description=OpenAIAdapter.description,
        capability=OpenAIAdapter.capability,
        requires_api_key=True,
    )
    register_adapter(
        "gemini", GeminiAdapter,
        display_name="Gemini (OpenAI-compatible endpoint)",
        description=GeminiAdapter.description,
        capability=GeminiAdapter.capability,
        requires_api_key=True,
    )
    register_adapter(
        "jules", JulesAdapter,
        display_name="Jules (official async coding agent)",
        description=JulesAdapter.description,
        capability=JulesAdapter.capability,
        requires_api_key=True,
    )
    register_adapter(
        "mock", MockProvider,
        display_name="Mock (in-memory, testing)",
        description=MockProvider.__doc__ or "Deterministic in-memory provider for tests.",
        capability=ProviderCapability(streaming=True, code_editing=True),
        requires_api_key=False,
        configurable=True,
    )


# ── Manager ─────────────────────────────────────────────────────────────────
class ProviderManager:
    """Central provider registry with health monitoring and fallback."""

    def __init__(self, settings: Optional[Any] = None) -> None:
        _register_builtins()
        self.settings = settings
        # name -> ProviderInfo
        self._providers: Dict[str, ProviderInfo] = {}
        # name -> instantiated adapter instance (lazy)
        self._instances: Dict[str, Any] = {}
        self._active: Optional[str] = None
        # ordered fallback chain (list of names)
        self._fallback_chain: List[str] = []
        self._auto_register_builtins()
        # Select active from settings if provided.
        if settings is not None:
            preferred = getattr(settings, "ai_provider", None) or "local"
            self.set_active(preferred)

    # ── registration ────────────────────────────────────────────────────
    def _auto_register_builtins(self) -> None:
        """Register ProviderInfo records for every registered adapter."""
        for name, meta in _ADAPTERS.items():
            if name in self._providers:
                continue
            info = ProviderInfo(
                name=name,
                display_name=meta["display_name"],
                description=meta["description"],
                capability=meta["capability"],
                requires_api_key=meta["requires_api_key"],
                enabled=True,
                configurable=meta["configurable"],
            )
            self._providers[name] = info

    def register(
        self,
        name: str,
        adapter_cls: Type,
        *,
        display_name: str = "",
        description: str = "",
        capability: Optional[ProviderCapability] = None,
        requires_api_key: bool = False,
        configurable: bool = True,
    ) -> ProviderInfo:
        """Register a new provider adapter at runtime (plugin extension point)."""
        register_adapter(
            name, adapter_cls,
            display_name=display_name,
            description=description,
            capability=capability,
            requires_api_key=requires_api_key,
            configurable=configurable,
        )
        info = ProviderInfo(
            name=name,
            display_name=display_name or getattr(adapter_cls, "display_name", name),
            description=description or getattr(adapter_cls, "description", ""),
            capability=capability or getattr(adapter_cls, "capability", ProviderCapability()),
            requires_api_key=requires_api_key or getattr(adapter_cls, "requires_api_key", False),
            configurable=configurable,
        )
        self._providers[name] = info
        return info

    # ── enable / disable ────────────────────────────────────────────────
    def enable(self, name: str) -> bool:
        info = self._providers.get(name)
        if not info:
            return False
        info.enabled = True
        if info.health.status == ProviderStatus.DISABLED:
            info.health.status = ProviderStatus.UNKNOWN
        return True

    def disable(self, name: str) -> bool:
        info = self._providers.get(name)
        if not info:
            return False
        info.enabled = False
        info.health.status = ProviderStatus.DISABLED
        # Clear any cached instance so a re-enable re-initialises.
        self._instances.pop(name, None)
        if self._active == name:
            # Active provider disabled -> fall back to local.
            self._active = "local" if "local" in self._providers else None
        return True

    # ── active selection ────────────────────────────────────────────────
    def set_active(self, name: str) -> bool:
        """Select the active provider by name."""
        info = self._providers.get(name)
        if not info:
            logger.warning("set_active: unknown provider %r; falling back to local.", name)
            self._active = "local" if "local" in self._providers else None
            return False
        if not info.enabled:
            logger.warning("set_active: provider %r is disabled; using local.", name)
            self._active = "local" if "local" in self._providers else None
            return False
        self._active = name
        self._rebuild_fallback_chain()
        return True

    @property
    def active_name(self) -> Optional[str]:
        return self._active

    def available_providers(self) -> List[str]:
        """Names of enabled providers (excluding disabled)."""
        return [n for n, i in self._providers.items() if i.enabled]

    def get_info(self, name: str) -> Optional[ProviderInfo]:
        return self._providers.get(name)

    def all_info(self) -> Dict[str, ProviderInfo]:
        return dict(self._providers)

    # ── capability detection ────────────────────────────────────────────
    def capability(self, name: str) -> Optional[ProviderCapability]:
        info = self._providers.get(name)
        return info.capability if info else None

    def providers_with_capability(self, cap: str) -> List[str]:
        """Return enabled provider names that have ``cap`` set True.

        ``cap`` is one of: streaming, tool_calling, code_editing.
        """
        result = []
        for name, info in self._providers.items():
            if not info.enabled:
                continue
            if getattr(info.capability, cap, False):
                result.append(name)
        return result

    # ── instantiation (lazy) ─────────────────────────────────────────────
    def get_instance(self, name: str) -> Optional[Any]:
        """Return the (lazily constructed) adapter instance, or None if unusable."""
        info = self._providers.get(name)
        if not info or not info.enabled:
            return None
        if name in self._instances:
            return self._instances[name]
        meta = _ADAPTERS.get(name)
        if not meta:
            return None
        cls = meta["cls"]
        try:
            inst = cls(self.settings)
            # Some adapters (e.g. OpenAIAdapter) catch construction errors and
            # store them in ``_init_error`` with ``_inner`` left as None, so the
            # adapter object exists but is not usable. Treat that as a failure.
            init_error = getattr(inst, "_init_error", None)
            if init_error is not None and getattr(inst, "_inner", "missing") is None:
                info.health.record_failure(init_error)
                return None
            self._instances[name] = inst
            return inst
        except Exception as exc:  # noqa: BLE001
            logger.debug("provider %s failed to init: %s", name, exc)
            info.health.record_failure(str(exc))
            return None

    def get_active(self) -> Optional[Any]:
        """Return the active provider instance (constructing it lazily)."""
        if self._active is None:
            return None
        return self.get_instance(self._active)

    # ── fallback chain ──────────────────────────────────────────────────
    def _rebuild_fallback_chain(self) -> None:
        """Build an ordered fallback list: active first, then compatible others."""
        chain: List[str] = []
        if self._active and self._active not in chain:
            chain.append(self._active)
        active_cap = self.capability(self._active) if self._active else None
        for name, info in self._providers.items():
            if name == self._active or not info.enabled:
                continue
            # Prefer providers with matching code_editing capability (the
            # primary capability the agent loop relies on).
            if active_cap and info.capability.code_editing != active_cap.code_editing:
                continue
            chain.append(name)
        # Always keep local as the final safety net if not already present.
        if "local" in self._providers and "local" not in chain:
            chain.append("local")
        self._fallback_chain = chain

    @property
    def fallback_chain(self) -> List[str]:
        if not self._fallback_chain:
            self._rebuild_fallback_chain()
        return list(self._fallback_chain)

    def set_fallback_chain(self, names: List[str]) -> None:
        """Explicitly set the fallback order (overrides auto-built chain)."""
        self._fallback_chain = [n for n in names if n in self._providers]

    # ── call wrapper with health + fallback ──────────────────────────────
    def call(self, method: str, *args, **kwargs) -> Any:
        """Invoke ``method`` on the active provider with automatic fallback.

        Tries each provider in the fallback chain until one succeeds. Records
        health metrics (success/failure, response time) for every attempt.
        Raises the last exception if all providers fail.
        """
        chain = self.fallback_chain or ["local"]
        last_exc: Optional[Exception] = None
        for name in chain:
            info = self._providers.get(name)
            if not info or not info.enabled:
                continue
            inst = self.get_instance(name)
            if inst is None:
                continue
            fn = getattr(inst, method, None)
            if fn is None:
                continue
            start = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                info.health.record_success(elapsed_ms)
                # If a fallback provider succeeded, promote it to active if
                # the original active one is now unhealthy.
                if name != self._active and info.health.status != ProviderStatus.UNHEALTHY:
                    active_info = self._providers.get(self._active) if self._active else None
                    if active_info and active_info.health.status in (
                        ProviderStatus.UNHEALTHY, ProviderStatus.DISABLED,
                    ):
                        logger.info(
                            "Fallback: active provider %s unhealthy; promoting %s.",
                            self._active, name,
                        )
                        self._active = name
                return result
            except Exception as exc:  # noqa: BLE001
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                info.health.record_failure(str(exc))
                last_exc = exc
                logger.warning(
                    "provider %s.%s failed: %s — trying fallback.", name, method, exc
                )
                continue
        # All providers failed.
        if last_exc:
            raise last_exc
        raise RuntimeError("No usable provider available in fallback chain.")

    # Convenience high-level methods (use the call() wrapper):
    def plan(self, task: str, context: str):
        return self.call("plan", task, context)

    def edit(self, task: str, plan, files: List[dict]) -> List[dict]:
        return self.call("edit", task, plan, files)

    def analyze_error(self, task: str, error: str, files: List[dict]) -> str:
        return self.call("analyze_error", task, error, files)

    def stream_chat(self, messages, on_token: Optional[Callable[[str], None]] = None):
        return self.call("stream_chat", messages, on_token)

    def chat(self, messages, *, stream: bool = False):
        return self.call("chat", messages, stream=stream)

    def review(self, task: str, files: List[dict]) -> str:
        return self.call("review", task, files)

    def summarize(self, text: str, max_length: int = 200) -> str:
        return self.call("summarize", text, max_length)

    # ── health check ─────────────────────────────────────────────────────
    def health_check(self, name: Optional[str] = None) -> Dict[str, Any]:
        """Run a lightweight probe (``plan`` with a trivial task).

        If ``name`` is given, probe only that provider; otherwise probe the
        active one. Updates health records. Returns the health dict.
        """
        target = name or self._active
        if not target:
            return {}
        info = self._providers.get(target)
        if not info:
            return {}
        inst = self.get_instance(target)
        if inst is None:
            info.health.record_failure("could not initialise provider")
            return info.health.to_dict()
        start = time.perf_counter()
        try:
            inst.plan("health check", "")
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            info.health.record_success(elapsed_ms)
        except Exception as exc:  # noqa: BLE001
            info.health.record_failure(str(exc))
        return info.health.to_dict()

    # ── status / serialisation (no secrets) ──────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        return {
            "active": self._active,
            "available": self.available_providers(),
            "fallback_chain": self.fallback_chain,
            "providers": {
                name: info.to_dict() for name, info in self._providers.items()
            },
        }

    def status(self) -> Dict[str, Any]:
        """Public, non-secret status snapshot for the API/UI."""
        d = self.to_dict()
        active_info = self._providers.get(self._active) if self._active else None
        d["active_capability"] = active_info.capability.to_dict() if active_info else None
        d["active_health"] = active_info.health.to_dict() if active_info else None
        return d

    # ── reset (for tests) ────────────────────────────────────────────────
    def reset(self) -> None:
        self._providers.clear()
        self._instances.clear()
        self._active = None
        self._fallback_chain = []
        self._auto_register_builtins()


# ── Module-level singleton (lazy) ───────────────────────────────────────────
_manager: Optional[ProviderManager] = None


def get_manager(settings: Optional[Any] = None) -> ProviderManager:
    """Return the process-wide ProviderManager singleton."""
    global _manager
    if _manager is None:
        _manager = ProviderManager(settings)
    elif settings is not None:
        _manager.settings = settings
        # Re-select active if settings changed the preferred provider.
        preferred = getattr(settings, "ai_provider", None)
        if preferred and preferred != _manager.active_name:
            _manager.set_active(preferred)
    return _manager


def reset_manager() -> None:
    """Reset the singleton (primarily for tests)."""
    global _manager
    _manager = None
    _ADAPTERS.clear()


def provider_manager_status(settings: Optional[Any] = None) -> Dict[str, Any]:
    """Public status snapshot suitable for an API endpoint. No secrets."""
    return get_manager(settings).status()
