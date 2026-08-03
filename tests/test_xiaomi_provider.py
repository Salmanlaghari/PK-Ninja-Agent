"""Comprehensive tests for the Xiaomi MiMo provider integration.

Covers:
* XiaomiAdapter initialisation (key resolution, no-key → AIError).
* Provider registration in manager.
* Capability detection.
* Secret leak prevention.
* Factory fallback without key.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_BACKEND = _REPO_ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from ai_provider import AIError, get_provider, LocalProvider
from config import get_settings


def _settings_with_xiaomi_key(monkeypatch, key="test-mimo-key-12345"):
    get_settings.cache_clear()
    monkeypatch.setenv("AI_PROVIDER", "xiaomi")
    monkeypatch.setenv("MIMO_API_KEY", key)
    get_settings.cache_clear()
    return get_settings()


class TestXiaomiAdapterInit:
    def test_no_key_captures_init_error(self, monkeypatch):
        from providers import XiaomiAdapter, reset_manager
        get_settings.cache_clear()
        monkeypatch.delenv("MIMO_API_KEY", raising=False)
        monkeypatch.delenv("AI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setenv("BUILTIN_AI_API_KEY", "")
        get_settings.cache_clear()
        reset_manager()
        adapter = XiaomiAdapter(get_settings())
        assert adapter._inner is None
        assert adapter._init_error is not None
        with pytest.raises(AIError):
            _ = adapter._provider
        reset_manager()

    def test_with_key_initialises(self, monkeypatch):
        from providers import XiaomiAdapter, reset_manager
        s = _settings_with_xiaomi_key(monkeypatch)
        adapter = XiaomiAdapter(s)
        assert adapter._inner is not None
        reset_manager()

    def test_name_and_capabilities(self):
        from providers import XiaomiAdapter
        assert XiaomiAdapter.name == "xiaomi"
        assert XiaomiAdapter.requires_api_key is True
        cap = XiaomiAdapter.capability
        assert cap.streaming is True
        assert cap.tool_calling is True
        assert cap.code_editing is True


class TestXiaomiManagerIntegration:
    def test_xiaomi_is_registered_builtin(self):
        from providers import ProviderManager, reset_manager
        reset_manager()
        m = ProviderManager()
        names = set(m.all_info().keys())
        assert "xiaomi" in names
        reset_manager()

    def test_xiaomi_capability_advertised(self):
        from providers import ProviderManager, reset_manager
        reset_manager()
        m = ProviderManager()
        info = m.all_info()["xiaomi"]
        assert info.capability.streaming is True
        assert info.requires_api_key is True
        assert "xiaomi" in info.display_name.lower()
        reset_manager()

    def test_get_instance_returns_none_without_key(self, monkeypatch):
        from providers import ProviderManager, reset_manager
        get_settings.cache_clear()
        monkeypatch.delenv("MIMO_API_KEY", raising=False)
        monkeypatch.delenv("AI_API_KEY", raising=False)
        monkeypatch.setenv("BUILTIN_AI_API_KEY", "")
        get_settings.cache_clear()
        reset_manager()
        m = ProviderManager(get_settings())
        assert m.get_instance("xiaomi") is None
        reset_manager()

    def test_no_secrets_in_manager_status(self, monkeypatch):
        from providers import ProviderManager, reset_manager, provider_manager_status
        s = _settings_with_xiaomi_key(monkeypatch, key="MIMO_SUPERSECRET_1234567890ABCDEF")
        reset_manager()
        m = ProviderManager(s)
        m.get_instance("xiaomi")
        status = provider_manager_status()
        blob = json.dumps(status)
        assert "SUPERSECRET" not in blob
        reset_manager()


class TestXiaomiFactory:
    def test_factory_xiaomi_without_key_falls_back(self, monkeypatch):
        get_settings.cache_clear()
        monkeypatch.setenv("AI_PROVIDER", "xiaomi")
        monkeypatch.delenv("MIMO_API_KEY", raising=False)
        monkeypatch.delenv("AI_API_KEY", raising=False)
        monkeypatch.setenv("BUILTIN_AI_API_KEY", "")
        get_settings.cache_clear()
        p = get_provider()
        assert isinstance(p, LocalProvider)

    def test_factory_xiaomi_with_key(self, monkeypatch):
        from ai_provider import OpenAIProvider
        get_settings.cache_clear()
        monkeypatch.setenv("AI_PROVIDER", "xiaomi")
        monkeypatch.setenv("MIMO_API_KEY", "test-key-123")
        get_settings.cache_clear()
        p = get_provider()
        # Factory returns OpenAIProvider with provider_hint="xiaomi";
        # name stays "openai" (the underlying provider class name).
        assert isinstance(p, OpenAIProvider)
        assert not isinstance(p, LocalProvider)
