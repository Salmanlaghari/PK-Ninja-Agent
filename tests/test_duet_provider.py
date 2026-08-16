"""Comprehensive unit tests for Duet AI provider integration and PK Agent rebranding.
"""
import os
import pytest
from unittest.mock import MagicMock, patch
from config import get_settings, Settings
from ai_provider import DuetProvider, AIError, get_provider, ChatMessage
from providers.duet_provider import DuetAdapter
from providers.manager import ProviderManager, reset_manager


def _settings_with_duet_key(monkeypatch, key="duet_sk_test_key_123456"):
    monkeypatch.setenv("DUET_API_KEY", key)
    # Clear cached settings so new env var takes effect
    get_settings.cache_clear()
    return get_settings()


class TestDuetConfig:
    def test_missing_duet_key_raises_error(self, monkeypatch):
        monkeypatch.delenv("DUET_API_KEY", raising=False)
        monkeypatch.delenv("AI_API_KEY", raising=False)
        get_settings.cache_clear()
        s = get_settings()
        with pytest.raises(AIError, match="DUET_API_KEY is not set"):
            DuetProvider(s)

    def test_duet_key_loaded_from_env(self, monkeypatch):
        s = _settings_with_duet_key(monkeypatch, key="duet_sk_abc123")
        p = DuetProvider(s)
        assert p.api_key == "duet_sk_abc123"
        assert p.base_url == "https://ctl.duet.so"

    def test_local_properties_loading(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DUET_API_KEY", raising=False)
        get_settings.cache_clear()
        props_file = tmp_path / "local.properties"
        props_file.write_text("DUET_API_KEY=duet_sk_from_local_properties\n")

        from config import load_dotenv_into_environ
        with patch("config.Path") as mock_path:
            # Let Path("local.properties") point to props_file
            def path_side_effect(p):
                if p == "local.properties":
                    return props_file
                return tmp_path / p
            mock_path.side_effect = path_side_effect
            load_dotenv_into_environ()

        assert os.environ.get("DUET_API_KEY") == "duet_sk_from_local_properties"


class TestDuetProviderApi:
    @patch("httpx.Client.get")
    def test_whoami_connectivity_check(self, mock_get, monkeypatch):
        s = _settings_with_duet_key(monkeypatch)
        p = DuetProvider(s)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "user": "test_user",
            "workspace": "test_workspace",
            "scopes": ["session:read", "session:write"]
        }
        mock_get.return_value = mock_resp

        res = p.whoami()
        assert res["user"] == "test_user"
        assert res["workspace"] == "test_workspace"

        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        assert args[0] == "https://ctl.duet.so/v1/whoami"
        assert kwargs["headers"]["Authorization"] == f"Bearer {p.api_key}"

    @patch("httpx.Client.post")
    def test_generate_chat_completions(self, mock_post, monkeypatch):
        s = _settings_with_duet_key(monkeypatch)
        p = DuetProvider(s)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Hello from PK Agent!"}}]
        }
        mock_post.return_value = mock_resp

        messages = [ChatMessage("user", "Hello")]
        text = p.generate(messages)
        assert text == "Hello from PK Agent!"

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert kwargs["headers"]["Authorization"] == f"Bearer {p.api_key}"

    @patch("httpx.Client.post")
    def test_generate_fallback_session_endpoint(self, mock_post, monkeypatch):
        s = _settings_with_duet_key(monkeypatch)
        p = DuetProvider(s)

        mock_404 = MagicMock()
        mock_404.status_code = 404

        mock_200 = MagicMock()
        mock_200.status_code = 200
        mock_200.json.return_value = {"response": "Session response from PK Agent"}

        mock_post.side_effect = [mock_404, mock_200]

        messages = [ChatMessage("user", "Hello")]
        text = p.generate(messages)
        assert text == "Session response from PK Agent"
        assert mock_post.call_count == 2


class TestDuetManagerIntegration:
    def setup_method(self):
        reset_manager()

    def teardown_method(self):
        reset_manager()

    def test_duet_registered_as_pk_agent(self):
        m = ProviderManager()
        info = m.get_info("duet")
        assert info is not None
        assert info.name == "duet"
        assert info.display_name == "PK Agent"
        assert "ctl.duet.so" in info.description

    def test_adapter_whoami(self, monkeypatch):
        s = _settings_with_duet_key(monkeypatch)
        adapter = DuetAdapter(s)

        with patch.object(adapter._provider, "whoami") as mock_whoami:
            mock_whoami.return_value = {"user": "pk_user"}
            assert adapter.whoami() == {"user": "pk_user"}

    def test_factory_duet_provider(self, monkeypatch):
        s = _settings_with_duet_key(monkeypatch)
        monkeypatch.setenv("AI_PROVIDER", "duet")
        get_settings.cache_clear()

        p = get_provider(get_settings())
        assert isinstance(p, DuetProvider)
        assert p.name == "duet"
