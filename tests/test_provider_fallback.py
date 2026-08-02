"""Tests for the provider fallback system in isolation.

Exercises the ProviderManager.call() fallback chain with controlled mock
providers to verify transparent switching, health promotion, and error
propagation.
"""
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from providers import ProviderManager, ProviderStatus, reset_manager
from providers.mock_provider import MockProvider, MockConfig


@pytest.fixture
def mgr():
    reset_manager()
    m = ProviderManager()
    # Register two controlled mocks.
    m.register("primary", MockProvider, capability=__import__(
        "providers").ProviderCapability(code_editing=True))
    m.register("secondary", MockProvider, capability=__import__(
        "providers").ProviderCapability(code_editing=True))
    yield m
    reset_manager()


def _install(mgr, name, config):
    """Install a configured MockProvider instance under ``name``."""
    mgr._instances[name] = MockProvider(config=config)


def test_fallback_to_secondary_on_primary_failure(mgr):
    _install(mgr, "primary", MockConfig(fail_next=5, fail_message="primary down"))
    _install(mgr, "secondary", MockConfig(chat_text="secondary ok"))
    mgr.set_active("primary")
    mgr.set_fallback_chain(["primary", "secondary"])
    result = mgr.plan("task", "ctx")
    assert result is not None
    assert mgr.get_info("primary").health.error_count >= 1
    assert mgr.get_info("secondary").health.success_count >= 1


def test_fallback_promotes_secondary_when_primary_unhealthy(mgr):
    # Make primary fail enough times to become unhealthy.
    _install(mgr, "primary", MockConfig(fail_next=20, fail_message="primary down"))
    _install(mgr, "secondary", MockConfig(chat_text="secondary ok"))
    mgr.set_active("primary")
    mgr.set_fallback_chain(["primary", "secondary"])
    # Drive primary to UNHEALTHY by recording failures directly.
    pinfo = mgr.get_info("primary")
    for _ in range(5):
        pinfo.health.record_failure("boom")
    assert pinfo.health.status == ProviderStatus.UNHEALTHY
    # Now a successful call should promote secondary to active.
    result = mgr.plan("task", "ctx")
    assert result is not None
    assert mgr.active_name == "secondary"


def test_fallback_skips_disabled_providers(mgr):
    _install(mgr, "primary", MockConfig(fail_next=5, fail_message="down"))
    _install(mgr, "secondary", MockConfig(chat_text="ok"))
    mgr.set_active("primary")
    mgr.disable("secondary")
    mgr.set_fallback_chain(["primary", "secondary", "local"])
    # secondary is disabled -> should skip to local.
    result = mgr.plan("add docstrings", "ctx")
    assert result is not None
    # secondary should NOT have been called.
    assert mgr.get_info("secondary").health.request_count == 0


def test_fallback_records_response_time(mgr):
    _install(mgr, "primary", MockConfig(fail_next=5))
    _install(mgr, "secondary", MockConfig(chat_text="ok", latency_ms=2.0))
    mgr.set_active("primary")
    mgr.set_fallback_chain(["primary", "secondary"])
    mgr.plan("task", "ctx")
    sinfo = mgr.get_info("secondary")
    assert sinfo.health.avg_response_time_ms is not None
    assert sinfo.health.avg_response_time_ms >= 0.0


def test_no_fallback_when_chain_empty_raises(mgr):
    _install(mgr, "primary", MockConfig(fail_next=5))
    mgr.set_active("primary")
    # Disable everything except primary, empty-ish chain.
    for name in list(mgr.all_info().keys()):
        if name != "primary":
            mgr.disable(name)
    mgr.set_fallback_chain(["primary"])
    with pytest.raises(Exception):
        mgr.plan("x", "y")


def test_chat_method_fallback(mgr):
    from ai_provider import ChatMessage
    _install(mgr, "primary", MockConfig(fail_next=5))
    _install(mgr, "secondary", MockConfig(chat_text="hello from secondary"))
    mgr.set_active("primary")
    mgr.set_fallback_chain(["primary", "secondary"])
    result = mgr.chat([ChatMessage("user", "hi")])
    assert "secondary" in result.text


def test_review_method_fallback(mgr):
    _install(mgr, "primary", MockConfig(fail_next=5))
    _install(mgr, "secondary", MockConfig(review_text="review ok"))
    mgr.set_active("primary")
    mgr.set_fallback_chain(["primary", "secondary"])
    result = mgr.review("task", [{"path": "a.py", "content": "x"}])
    assert result == "review ok"
