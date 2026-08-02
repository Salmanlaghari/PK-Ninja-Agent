"""Tests for the MockProvider — the deterministic test double.

Verifies the MockProvider produces real, verifiable output, simulates failures
and latency, logs calls, and satisfies the enhanced ProviderProtocol.
"""
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from providers.mock_provider import MockProvider, MockConfig
from providers.interface import ProviderProtocol
from ai_provider import ChatMessage


def test_mock_plan_returns_real_plan():
    mp = MockProvider(config=MockConfig(plan_summary="S", plan_steps=["a", "b"]))
    plan = mp.plan("task", "ctx")
    assert plan.summary == "S"
    assert plan.steps == ["a", "b"]


def test_mock_edit_returns_canned_edits():
    edits = [{"path": "a.py", "content": "new"}]
    mp = MockProvider(config=MockConfig(edits=edits))
    out = mp.edit("task", mp.plan("t", "c"), [])
    assert out == edits


def test_mock_chat_returns_text():
    mp = MockProvider(config=MockConfig(chat_text="hello"))
    r = mp.chat([ChatMessage("user", "hi")])
    assert r.text == "hello"
    assert r.model == "mock"


def test_mock_stream_chat_calls_on_token():
    mp = MockProvider(config=MockConfig(chat_text="one two three"))
    tokens = []
    r = mp.stream_chat([ChatMessage("user", "hi")], on_token=tokens.append)
    assert r.text == "one two three"
    assert len(tokens) > 0


def test_mock_review():
    mp = MockProvider(config=MockConfig(review_text="looks good"))
    assert mp.review("task", []) == "looks good"


def test_mock_summarize_truncates():
    mp = MockProvider(config=MockConfig(summary_text="x" * 500))
    assert mp.summarize("text", max_length=50) == "x" * 50


def test_mock_failure_simulation():
    mp = MockProvider(config=MockConfig(fail_next=2, fail_message="boom"))
    with pytest.raises(RuntimeError, match="boom"):
        mp.plan("t", "c")
    with pytest.raises(RuntimeError, match="boom"):
        mp.plan("t", "c")
    # Third call succeeds (fail_next exhausted).
    plan = mp.plan("t", "c")
    assert plan.summary == "Mock plan."


def test_mock_call_log():
    mp = MockProvider()
    mp.plan("t", "c")
    mp.chat([ChatMessage("user", "hi")])
    assert "plan" in mp.call_log
    assert "chat" in mp.call_log


def test_mock_satisfies_provider_protocol():
    mp = MockProvider()
    # ProviderProtocol is runtime_checkable; verify structural compatibility.
    assert hasattr(mp, "name")
    assert hasattr(mp, "plan")
    assert hasattr(mp, "edit")
    assert hasattr(mp, "analyze_error")
    assert hasattr(mp, "stream_chat")
    assert hasattr(mp, "chat")
    assert hasattr(mp, "review")
    assert hasattr(mp, "summarize")


def test_mock_analyze_error():
    mp = MockProvider()
    out = mp.analyze_error("task", "SyntaxError: bad", [])
    assert "SyntaxError" in out


def test_mock_custom_name():
    mp = MockProvider(config=MockConfig(name="custom-mock"))
    assert mp.name == "custom-mock"


def test_mock_latency_does_not_error():
    mp = MockProvider(config=MockConfig(latency_ms=1.0))
    # Should complete without error.
    plan = mp.plan("t", "c")
    assert plan is not None
