"""Tests for provider validation and test-connection endpoints."""
import sys
from pathlib import Path
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_BACKEND = _REPO_ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from providers import ProviderManager, MockProvider, reset_manager
from providers.mock_provider import MockConfig


@pytest.fixture
def mgr():
    reset_manager()
    m = ProviderManager()
    yield m
    reset_manager()


def test_validate_local_provider(mgr):
    """Local provider should validate successfully (no key needed)."""
    health = mgr.health_check("local")
    assert health["status"] in ("healthy", "unknown", "degraded")


def test_validate_mock_provider(mgr):
    """Mock provider should validate successfully."""
    health = mgr.health_check("mock")
    assert health["status"] in ("healthy", "unknown", "degraded")


def test_validate_unknown_provider(mgr):
    """Unknown provider should return empty health."""
    health = mgr.health_check("nonexistent")
    assert health == {}


def test_test_connection_with_mock(mgr):
    """Mock provider should respond to a test chat message."""
    mgr.set_active("mock")
    inst = mgr.get_active()
    assert inst is not None
    from ai_provider import ChatMessage
    messages = [ChatMessage("user", "Say ok")]
    result = inst.chat(messages, stream=False)
    assert result.text  # MockProvider returns canned text
