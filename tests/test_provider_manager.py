"""Tests for the Provider Manager: registry, enable/disable, capability
detection, health monitoring, dynamic loading and fallback.

These tests use the built-in MockProvider and LocalAdapter so no network or
API key is required.
"""
import os
import sys
from pathlib import Path

import pytest

# Ensure the repo root (where the providers/ package lives) is importable.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from providers import (
    ProviderManager,
    ProviderCapability,
    ProviderStatus,
    MockProvider,
    reset_manager,
)
from providers.mock_provider import MockConfig


@pytest.fixture
def fresh_manager():
    """A brand-new ProviderManager with built-ins registered."""
    reset_manager()
    m = ProviderManager()
    yield m
    reset_manager()


# ── Registry / built-ins ────────────────────────────────────────────────────
def test_builtins_registered(fresh_manager):
    names = set(fresh_manager.all_info().keys())
    assert {"local", "openai", "gemini", "mock"} <= names


def test_default_active_is_local_when_settings_local(fresh_manager):
    from config import get_settings
    get_settings.cache_clear()
    s = get_settings()
    fresh_manager.set_active(s.ai_provider or "local")
    assert fresh_manager.active_name == "local"


def test_unknown_active_falls_back_to_local(fresh_manager):
    assert fresh_manager.set_active("does-not-exist") is False
    assert fresh_manager.active_name == "local"


# ── Enable / disable ────────────────────────────────────────────────────────
def test_disable_provider(fresh_manager):
    assert fresh_manager.disable("mock") is True
    info = fresh_manager.get_info("mock")
    assert info.enabled is False
    assert info.health.status == ProviderStatus.DISABLED
    assert "mock" not in fresh_manager.available_providers()


def test_enable_provider(fresh_manager):
    fresh_manager.disable("mock")
    assert fresh_manager.enable("mock") is True
    info = fresh_manager.get_info("mock")
    assert info.enabled is True
    assert "mock" in fresh_manager.available_providers()


def test_disable_unknown_returns_false(fresh_manager):
    assert fresh_manager.disable("nope") is False
    assert fresh_manager.enable("nope") is False


def test_disabling_active_falls_back_to_local(fresh_manager):
    fresh_manager.set_active("mock")
    fresh_manager.disable("mock")
    assert fresh_manager.active_name == "local"


# ── Capability detection ────────────────────────────────────────────────────
def test_capability_lookup(fresh_manager):
    cap = fresh_manager.capability("local")
    assert isinstance(cap, ProviderCapability)
    assert cap.code_editing is True
    assert cap.streaming is True


def test_capability_unknown_provider(fresh_manager):
    assert fresh_manager.capability("nope") is None


def test_providers_with_capability_streaming(fresh_manager):
    streaming = fresh_manager.providers_with_capability("streaming")
    assert "local" in streaming
    assert "mock" in streaming


def test_providers_with_capability_tool_calling_excludes_local(fresh_manager):
    tc = fresh_manager.providers_with_capability("tool_calling")
    assert "local" not in tc
    assert "openai" in tc


# ── Dynamic loading / instantiation ─────────────────────────────────────────
def test_get_instance_local(fresh_manager):
    inst = fresh_manager.get_instance("local")
    assert inst is not None
    assert inst.name == "local"


def test_get_instance_openai_without_key_returns_none(fresh_manager):
    # No API key in test env -> OpenAIAdapter construction fails -> None.
    inst = fresh_manager.get_instance("openai")
    assert inst is None
    info = fresh_manager.get_info("openai")
    assert info.health.error_count >= 1


def test_get_instance_disabled_returns_none(fresh_manager):
    fresh_manager.disable("local")
    assert fresh_manager.get_instance("local") is None


# ── Health monitoring ───────────────────────────────────────────────────────
def test_health_records_success(fresh_manager):
    fresh_manager.set_active("mock")
    fresh_manager.plan("task", "ctx")
    info = fresh_manager.get_info("mock")
    assert info.health.success_count == 1
    assert info.health.request_count == 1
    assert info.health.avg_response_time_ms is not None
    assert info.health.avg_response_time_ms >= 0.0


def test_health_records_failure_and_degrades(fresh_manager):
    m = fresh_manager
    # Register a mock that always fails.
    m.register(
        "failmock", MockProvider,
        capability=ProviderCapability(code_editing=True),
    )
    # Replace its instance with a configured failing mock.
    failing = MockProvider(config=MockConfig(fail_next=10, fail_message="boom"))
    m._instances["failmock"] = failing
    m.set_active("failmock")
    # Disable the built-in fallbacks so the failure must propagate (otherwise
    # ``call()`` falls back to ``local``/``mock`` and succeeds overall).
    m.disable("local")
    m.disable("mock")
    m.disable("openai")
    m.disable("gemini")
    with pytest.raises(Exception):
        m.plan("x", "y")
    info = m.get_info("failmock")
    assert info.health.error_count >= 1
    assert info.health.last_error_message == "boom"


def test_health_unhealthy_after_five_errors(fresh_manager):
    m = fresh_manager
    m.register("failmock", MockProvider)
    failing = MockProvider(config=MockConfig(fail_next=10))
    m._instances["failmock"] = failing
    m.set_active("failmock")
    # The fallback chain will try failmock then fall back to local, so plan()
    # succeeds overall — but failmock's health should degrade. We call the
    # failing instance directly to push its error count.
    for _ in range(5):
        with pytest.raises(Exception):
            failing.plan("x", "y")
        m.get_info("failmock").health.record_failure("boom")
    assert m.get_info("failmock").health.status == ProviderStatus.UNHEALTHY


def test_health_check_probe(fresh_manager):
    h = fresh_manager.health_check("local")
    assert h["status"] in ("healthy", "unknown", "degraded")
    assert fresh_manager.get_info("local").health.request_count >= 1


# ── Fallback system ─────────────────────────────────────────────────────────
def test_fallback_chain_built(fresh_manager):
    fresh_manager.set_active("local")
    chain = fresh_manager.fallback_chain
    assert chain[0] == "local"
    assert "local" in chain


def test_fallback_switches_on_failure(fresh_manager):
    m = fresh_manager
    # Active = failing mock; fallback chain includes local which succeeds.
    m.register("failmock", MockProvider)
    m._instances["failmock"] = MockProvider(config=MockConfig(fail_next=5, fail_message="boom"))
    m.set_active("failmock")
    # Rebuild chain so failmock is first, then local.
    m.set_fallback_chain(["failmock", "local"])
    # plan() should fall back to local and succeed.
    result = m.plan("add docstrings", "repo")
    assert result is not None
    # failmock recorded failures.
    assert m.get_info("failmock").health.error_count >= 1
    # local recorded a success.
    assert m.get_info("local").health.success_count >= 1


def test_fallback_all_fail_raises(fresh_manager):
    m = fresh_manager
    m.register("failmock", MockProvider)
    m._instances["failmock"] = MockProvider(config=MockConfig(fail_next=5))
    m.set_active("failmock")
    # Disable all other providers so only failmock is in the chain.
    for name in list(m.all_info().keys()):
        if name != "failmock":
            m.disable(name)
    m.set_fallback_chain(["failmock"])
    with pytest.raises(Exception):
        m.plan("x", "y")


def test_set_fallback_chain_explicit(fresh_manager):
    fresh_manager.set_fallback_chain(["local", "mock"])
    assert fresh_manager.fallback_chain == ["local", "mock"]


def test_set_fallback_chain_ignores_unknown(fresh_manager):
    fresh_manager.set_fallback_chain(["local", "nope", "mock"])
    assert fresh_manager.fallback_chain == ["local", "mock"]


# ── Status / serialisation (no secrets) ─────────────────────────────────────
def test_status_has_no_secrets(fresh_manager):
    import json
    fresh_manager.set_active("local")
    text = json.dumps(fresh_manager.status())
    # ``requires_api_key`` is legitimate boolean metadata (a *field name*), not
    # a secret value. We only guard against actual secret *values*: long
    # credential-like strings (hex tokens 20+ chars) that would indicate a
    # leaked API key / token.
    import re
    # Long hex/base64-ish token patterns that indicate real secrets.
    secret_value_patterns = [
        r"sk-[A-Za-z0-9]{16,}",
        r"[A-Za-z0-9_-]{40,}",
        r"AIza[0-9A-Za-z_-]{20,}",
    ]
    for pattern in secret_value_patterns:
        assert not re.search(pattern, text), f"possible secret value leaked: {pattern}"
    # Sanity: no password/secret *values* set anywhere.
    assert "password" not in text.lower()
    assert "secret" not in text.lower()


def test_to_dict_structure(fresh_manager):
    d = fresh_manager.to_dict()
    assert "active" in d
    assert "available" in d
    assert "fallback_chain" in d
    assert "providers" in d
    assert isinstance(d["providers"], dict)


# ── Dynamic plugin registration ─────────────────────────────────────────────
def test_register_custom_adapter(fresh_manager):
    class CustomAdapter(MockProvider):
        name = "custom"

    info = fresh_manager.register(
        "custom", CustomAdapter,
        display_name="Custom",
        description="A custom test adapter",
        capability=ProviderCapability(streaming=True, code_editing=True),
        requires_api_key=False,
    )
    assert info.name == "custom"
    assert "custom" in fresh_manager.all_info()
    inst = fresh_manager.get_instance("custom")
    assert inst is not None


# ── Convenience methods via call() ──────────────────────────────────────────
def test_plan_via_manager_uses_active(fresh_manager):
    fresh_manager.set_active("local")
    plan = fresh_manager.plan("add docstrings", "some context")
    assert hasattr(plan, "summary")
    assert hasattr(plan, "steps")


def test_summarize_via_local(fresh_manager):
    fresh_manager.set_active("local")
    summary = fresh_manager.summarize("This is a long sentence. Another one here.", max_length=100)
    assert isinstance(summary, str)
    assert len(summary) <= 100 or summary == "This is a long sentence. Another one here."
