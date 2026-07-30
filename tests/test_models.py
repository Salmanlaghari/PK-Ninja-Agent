"""Models: TaskStatus canonical values, legacy normalization, ConfigOut."""
from models import (
    ConfigOut,
    EventType,
    TaskStatus,
    normalize_status,
)


# ── TaskStatus canonical set ─────────────────────────────────────────────────
def test_task_status_has_five_canonical_values():
    values = {s.value for s in TaskStatus}
    for needed in ("idle", "running", "success", "failed", "cancelled"):
        assert needed in values, f"missing canonical status: {needed}"


def test_task_status_keeps_legacy_aliases():
    values = {s.value for s in TaskStatus}
    assert "pending" in values
    assert "completed" in values


def test_task_status_is_string_enum():
    assert TaskStatus.idle == "idle"
    assert TaskStatus.running == "running"
    assert TaskStatus.cancelled == "cancelled"


# ── normalize_status ─────────────────────────────────────────────────────────
def test_normalize_legacy_pending_to_idle():
    assert normalize_status("pending") == "idle"


def test_normalize_legacy_completed_to_success():
    assert normalize_status("completed") == "success"


def test_normalize_canonical_passes_through():
    for s in ("idle", "running", "success", "failed", "cancelled"):
        assert normalize_status(s) == s


def test_normalize_unknown_passes_through():
    assert normalize_status("weird") == "weird"


# ── EventType new members ────────────────────────────────────────────────────
def test_event_type_has_thinking():
    assert EventType.thinking.value == "thinking"


def test_event_type_has_cancelled():
    assert EventType.cancelled.value == "cancelled"


def test_event_type_values_unique():
    values = [e.value for e in EventType]
    assert len(values) == len(set(values))


# ── ConfigOut ────────────────────────────────────────────────────────────────
def test_config_out_serializes_without_secrets():
    c = ConfigOut(provider="openai", model="gpt-4o-mini",
                  configured=True, streaming_supported=True,
                  repository_configured=True)
    d = c.model_dump()
    assert d["provider"] == "openai"
    assert d["model"] == "gpt-4o-mini"
    assert d["configured"] is True
    assert d["streaming_supported"] is True
    assert d["repository_configured"] is True
    # No secret fields exist on the model at all.
    for secret in ("api_key", "token", "key", "password"):
        assert secret not in d
