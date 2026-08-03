"""Tests for provider capability detection per built-in provider.

Verifies that each built-in adapter declares its capabilities honestly and
that the manager exposes them correctly (including context_window / max_output
reported as None when 0/unknown).
"""
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from providers import (
    ProviderManager,
    ProviderCapability,
    LocalAdapter,
    OpenAIAdapter,
    GeminiAdapter,
    XiaomiAdapter,
    MockProvider,
    reset_manager,
)


@pytest.fixture
def mgr():
    reset_manager()
    m = ProviderManager()
    yield m
    reset_manager()


def test_local_adapter_capabilities():
    cap = LocalAdapter.capability
    assert cap.streaming is True
    assert cap.tool_calling is False
    assert cap.code_editing is True


def test_openai_adapter_capabilities():
    cap = OpenAIAdapter.capability
    assert cap.streaming is True
    assert cap.tool_calling is True
    assert cap.code_editing is True


def test_gemini_adapter_capabilities():
    cap = GeminiAdapter.capability
    assert cap.streaming is True
    assert cap.tool_calling is True
    assert cap.code_editing is True


def test_xiaomi_adapter_capabilities():
    cap = XiaomiAdapter.capability
    assert cap.streaming is True
    assert cap.tool_calling is True
    assert cap.code_editing is True
    assert XiaomiAdapter.requires_api_key is True
    assert XiaomiAdapter.name == "xiaomi"


def test_mock_provider_capabilities_default():
    from providers.mock_provider import MockConfig
    mp = MockProvider(config=MockConfig())
    # default capability from MockConfig -> ProviderCapability() defaults
    assert mp.capability.code_editing is True


def test_capability_to_dict_none_for_unknown(mgr):
    cap = mgr.capability("local")
    d = cap.to_dict()
    # context_window 0 -> None in to_dict
    assert d["context_window"] is None
    assert d["max_output"] is None
    assert d["streaming"] is True


def test_capability_to_dict_with_values():
    cap = ProviderCapability(streaming=True, tool_calling=True, context_window=128000, max_output=4096)
    d = cap.to_dict()
    assert d["context_window"] == 128000
    assert d["max_output"] == 4096


def test_manager_capability_matches_adapter(mgr):
    assert mgr.capability("openai").tool_calling is True
    assert mgr.capability("local").tool_calling is False
    assert mgr.capability("xiaomi").streaming is True
    assert mgr.capability("xiaomi").tool_calling is True


def test_providers_with_code_editing(mgr):
    ce = mgr.providers_with_capability("code_editing")
    assert "local" in ce
    assert "openai" in ce
    assert "mock" in ce
    assert "xiaomi" in ce
    assert "jules" in ce


def test_providers_with_capability_respects_disabled(mgr):
    mgr.disable("mock")
    streaming = mgr.providers_with_capability("streaming")
    assert "mock" not in streaming


def test_xiaomi_capability_to_dict(mgr):
    cap = mgr.capability("xiaomi")
    d = cap.to_dict()
    assert d["streaming"] is True
    assert d["tool_calling"] is True
    assert d["code_editing"] is True
    # context_window and max_output are 0 → None in to_dict
    assert d["context_window"] is None
    assert d["max_output"] is None
