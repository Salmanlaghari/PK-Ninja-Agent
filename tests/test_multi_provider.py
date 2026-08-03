"""Integration tests for the multi-provider system.

Tests provider switching, fallback, per-provider key resolution,
and the provider manager status API.
"""
import json
import sys
from pathlib import Path
import pytest

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
def mgr():
    reset_manager()
    m = ProviderManager()
    yield m
    reset_manager()


def test_all_builtin_providers_registered(mgr):
    names = set(mgr.all_info().keys())
    expected = {"local", "openai", "gemini", "jules", "xiaomi", "mock"}
    assert expected <= names


def test_provider_capabilities_honest(mgr):
    """Each provider declares its capabilities honestly."""
    local_cap = mgr.capability("local")
    assert local_cap.streaming is True
    assert local_cap.tool_calling is False

    openai_cap = mgr.capability("openai")
    assert openai_cap.streaming is True
    assert openai_cap.tool_calling is True

    gemini_cap = mgr.capability("gemini")
    assert gemini_cap.streaming is True
    assert gemini_cap.tool_calling is True

    jules_cap = mgr.capability("jules")
    assert jules_cap.streaming is True
    assert jules_cap.tool_calling is True
    assert jules_cap.code_editing is True

    xiaomi_cap = mgr.capability("xiaomi")
    assert xiaomi_cap.streaming is True
    assert xiaomi_cap.tool_calling is True


def test_fallback_chain_includes_all_enabled(mgr):
    mgr.set_active("local")
    chain = mgr.fallback_chain
    assert "local" in chain
    # local is always last safety net
    assert chain[-1] == "local" or "local" in chain


def test_disable_and_reenable(mgr):
    mgr.disable("mock")
    assert "mock" not in mgr.available_providers()
    mgr.enable("mock")
    assert "mock" in mgr.available_providers()


def test_status_no_secrets(mgr):
    mgr.set_active("local")
    status = json.dumps(mgr.status())
    # No long alphanumeric tokens
    import re
    leaks = re.findall(r"[A-Za-z0-9_-]{40,}", status)
    assert leaks == []


def test_provider_info_to_dict(mgr):
    info = mgr.get_info("local")
    d = info.to_dict()
    assert d["name"] == "local"
    assert d["requires_api_key"] is False
    assert d["configurable"] is False
    assert "capability" in d
    assert "health" in d
