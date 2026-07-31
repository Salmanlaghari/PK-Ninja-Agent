"""AI provider architecture: factory, LocalProvider streaming, OpenAIProvider
SSE parsing (mocked), and the graceful fallback contract."""
import json
from typing import List
from unittest.mock import patch

import pytest

from ai_provider import (
    AIError,
    ChatMessage,
    ChatResult,
    LocalProvider,
    OpenAIProvider,
    GeminiProvider,
    get_provider,
    provider_status,
    _parse_plan_json,
    _parse_edits_json,
)


# ── Factory selection ────────────────────────────────────────────────────────
def test_factory_default_is_local(monkeypatch):
    """With AI_PROVIDER=local (or unset) the factory returns LocalProvider."""
    from config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("AI_PROVIDER", "local")
    monkeypatch.delenv("AI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    get_settings.cache_clear()
    p = get_provider()
    assert isinstance(p, LocalProvider)
    assert p.name == "local"


def test_factory_falls_back_to_local_when_no_key(monkeypatch):
    """Asking for openai without a key must NOT crash — it falls back to local."""
    from config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("AI_PROVIDER", "openai")
    monkeypatch.delenv("AI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    get_settings.cache_clear()
    p = get_provider()
    assert isinstance(p, LocalProvider)


def test_factory_returns_openai_provider_with_key(monkeypatch):
    """With AI_PROVIDER=openai and AI_API_KEY set, we get an OpenAIProvider."""
    from config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("AI_PROVIDER", "openai")
    monkeypatch.setenv("AI_API_KEY", "sk-test-key-123")
    monkeypatch.setenv("AI_MODEL", "gpt-4o-mini")
    get_settings.cache_clear()
    p = get_provider()
    assert isinstance(p, OpenAIProvider)
    assert p.name == "openai"
    assert p.api_key == "sk-test-key-123"
    assert p.model == "gpt-4o-mini"


def test_factory_returns_gemini_provider(monkeypatch):
    from config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    get_settings.cache_clear()
    p = get_provider()
    assert isinstance(p, GeminiProvider)
    assert p.name == "gemini"
    # Gemini routes through Google's OpenAI-compatible endpoint.
    assert "googleapis.com" in p.base_url


def test_factory_custom_provider_uses_default_base(monkeypatch):
    """A custom provider name with an explicit base URL works."""
    from config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("AI_PROVIDER", "deepseek")
    monkeypatch.setenv("AI_API_KEY", "ds-key")
    monkeypatch.setenv("AI_BASE_URL", "https://api.deepseek.com/v1")
    get_settings.cache_clear()
    p = get_provider()
    assert isinstance(p, OpenAIProvider)
    assert p.base_url == "https://api.deepseek.com/v1"


# ── provider_status (non-secret summary) ─────────────────────────────────────
def test_provider_status_local(monkeypatch):
    from config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("AI_PROVIDER", "local")
    monkeypatch.delenv("AI_API_KEY", raising=False)
    get_settings.cache_clear()
    s = provider_status()
    assert s["provider"] == "local"
    assert s["configured"] is False
    assert s["streaming_supported"] is False


def test_provider_status_openai_configured(monkeypatch):
    from config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("AI_PROVIDER", "openai")
    monkeypatch.setenv("AI_API_KEY", "sk-x")
    get_settings.cache_clear()
    s = provider_status()
    assert s["provider"] == "openai"
    assert s["configured"] is True
    assert s["streaming_supported"] is True
    # Must never leak the key.
    assert "sk-x" not in json.dumps(s)


# ── LocalProvider streaming ──────────────────────────────────────────────────
def test_local_provider_stream_chat_yields_tokens():
    """LocalProvider.stream_chat calls on_token with real (deterministic) text."""
    p = LocalProvider()
    tokens: List[str] = []
    msgs = [ChatMessage("user", "Add module docstrings to my project")]
    result = p.stream_chat(msgs, on_token=lambda t: tokens.append(t))
    assert isinstance(result, ChatResult)
    assert result.model == "local"
    # Tokens must reconstruct the full text.
    assert "".join(tokens) == result.text
    assert len(tokens) > 1  # word-by-word streaming
    assert "docstring" in result.text.lower()


def test_local_provider_plan_for_docstrings():
    p = LocalProvider()
    plan = p.plan("Add module docstrings", "no context")
    assert "docstring" in plan.summary.lower()
    assert len(plan.steps) >= 1


def test_local_provider_edit_adds_docstring():
    p = LocalProvider()
    files = [{"path": "main.py", "content": "import os\nprint('hi')\n"}]
    edits = p.edit("Add module docstrings", p.plan("Add module docstrings", ""), files)
    assert len(edits) == 1
    assert edits[0]["path"] == "main.py"
    assert '"""' in edits[0]["content"]


def test_local_provider_edit_skips_already_documented():
    p = LocalProvider()
    files = [{"path": "main.py", "content": '"""Already documented."""\nimport os\n'}]
    edits = p.edit("Add module docstrings", p.plan("Add module docstrings", ""), files)
    assert edits == []


# ── OpenAIProvider SSE parsing (mocked httpx) ────────────────────────────────
def test_openai_provider_stream_chat_parses_sse_deltas(monkeypatch):
    """Verify stream_chat parses real SSE data: lines and accumulates deltas."""
    from config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("AI_PROVIDER", "openai")
    monkeypatch.setenv("AI_API_KEY", "sk-test")
    monkeypatch.setenv("AI_MODEL", "gpt-4o-mini")
    get_settings.cache_clear()
    provider = OpenAIProvider(get_settings())

    # Build a fake SSE stream: three deltas then [DONE].
    sse_lines = [
        'data: {"choices":[{"delta":{"content":"Hello"}}]}',
        'data: {"choices":[{"delta":{"content":" world"}}]}',
        'data: {"choices":[{"delta":{"content":"!"},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]

    class FakeResponse:
        def __init__(self):
            self.headers = {"content-type": "text/event-stream"}
        def read(self): pass
        def iter_lines(self):
            for line in sse_lines:
                yield line

    class FakeStreamCtx:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return FakeResponse()
        def __exit__(self, *a): return False

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def stream(self, *a, **kw): return FakeStreamCtx()

    with patch("ai_provider.httpx.Client", FakeClient):
        tokens: List[str] = []
        result = provider.stream_chat(
            [ChatMessage("user", "hi")], on_token=lambda t: tokens.append(t))
    assert result.text == "Hello world!"
    assert result.finish_reason == "stop"
    assert tokens == ["Hello", " world", "!"]


def test_openai_provider_stream_chat_falls_back_on_non_sse(monkeypatch):
    """If the endpoint returns JSON (not SSE), stream_chat still returns text."""
    from config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("AI_PROVIDER", "openai")
    monkeypatch.setenv("AI_API_KEY", "sk-test")
    get_settings.cache_clear()
    provider = OpenAIProvider(get_settings())

    class FakeResponse:
        def __init__(self):
            self.headers = {"content-type": "application/json"}
        def read(self): pass
        def json(self):
            return {"choices": [{"message": {"content": "flat reply"},
                                  "finish_reason": "stop"}]}
        def iter_lines(self):
            return iter([])

    class FakeStreamCtx:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return FakeResponse()
        def __exit__(self, *a): return False

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def stream(self, *a, **kw): return FakeStreamCtx()

    with patch("ai_provider.httpx.Client", FakeClient):
        tokens: List[str] = []
        result = provider.stream_chat(
            [ChatMessage("user", "hi")], on_token=lambda t: tokens.append(t))
    assert result.text == "flat reply"
    assert tokens == ["flat reply"]


# ── JSON parsing helpers ─────────────────────────────────────────────────────
def test_parse_plan_json_valid():
    text = 'Here is the plan:\n{"summary":"Do X","steps":["a","b"]}\nDone.'
    plan = _parse_plan_json(text, fallback_task="t")
    assert plan.summary == "Do X"
    assert plan.steps == ["a", "b"]


def test_parse_plan_json_invalid_fallback():
    plan = _parse_plan_json("no json here", fallback_task="t")
    assert plan.summary  # has some fallback text
    assert isinstance(plan.steps, list)


def test_parse_edits_json_valid():
    text = '```json\n[{"path":"a.py","content":"x=1"},{"path":"b.py","content":"y=2"}]\n```'
    edits = _parse_edits_json(text)
    assert len(edits) == 2
    assert edits[0]["path"] == "a.py"


def test_parse_edits_json_filters_bad_entries():
    text = '[{"path":"a.py","content":"x=1"},{"nope":true}]'
    edits = _parse_edits_json(text)
    assert len(edits) == 1
    assert edits[0]["path"] == "a.py"


# ── Protocol compliance ──────────────────────────────────────────────────────
def test_local_provider_satisfies_protocol():
    """LocalProvider must expose stream_chat, plan, edit, analyze_error."""
    from ai_provider import AIProvider
    p = LocalProvider()
    assert isinstance(p, AIProvider)


def test_factory_returns_anthropic_provider(monkeypatch):
    from config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("AI_PROVIDER", "anthropic")
    monkeypatch.setenv("AI_API_KEY", "ant-key")
    get_settings.cache_clear()
    p = get_provider()
    from ai_provider import AnthropicProvider
    assert isinstance(p, AnthropicProvider)
    assert p.name == "anthropic"


def test_factory_returns_jules_provider(monkeypatch):
    from config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("AI_PROVIDER", "jules")
    monkeypatch.setenv("AI_API_KEY", "jules-key")
    get_settings.cache_clear()
    p = get_provider()
    from ai_provider import JulesProvider
    assert isinstance(p, JulesProvider)
    assert p.name == "jules"


def test_anthropic_provider_stream_chat_parses_messages(monkeypatch):
    from config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("AI_PROVIDER", "anthropic")
    monkeypatch.setenv("AI_API_KEY", "ant-key")
    get_settings.cache_clear()
    from ai_provider import AnthropicProvider
    provider = AnthropicProvider(get_settings())

    sse_lines = [
        'data: {"type": "content_block_delta", "delta": {"text": "Hello"}}',
        'data: {"type": "content_block_delta", "delta": {"text": " Claude"}}',
        'data: {"type": "message_stop"}',
    ]

    class FakeResponse:
        def __init__(self):
            self.headers = {"content-type": "text/event-stream"}
        def read(self): pass
        def iter_lines(self):
            for line in sse_lines:
                yield line

    class FakeStreamCtx:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return FakeResponse()
        def __exit__(self, *a): return False

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def stream(self, *a, **kw): return FakeStreamCtx()

    with patch("ai_provider.httpx.Client", FakeClient):
        tokens: List[str] = []
        result = provider.stream_chat(
            [ChatMessage("user", "hi")], on_token=lambda t: tokens.append(t))
    assert result.text == "Hello Claude"
    assert tokens == ["Hello", " Claude"]
