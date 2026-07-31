"""Tests for the multi-agent core abstractions (agents.base + agents.registry).

These cover the structured messaging protocol, the result/context dataclasses,
the BaseAgent cancellation + error handling contract, and the registry
self-registration behavior. Everything here is provider-independent and runs
fully offline (no AI key, no network).
"""
import threading

import pytest

from agents.base import (
    AgentContext,
    AgentMessage,
    AgentResult,
    AgentRole,
    BaseAgent,
    MessagePriority,
    MessageRole,
)
from agents.registry import AgentRegistry, get_registry, register_agent


# ── AgentMessage ──────────────────────────────────────────────────────────────
def test_agent_message_roundtrip():
    msg = AgentMessage(
        sender=AgentRole.coordinator,
        recipient=AgentRole.coding,
        content="apply the planned edits",
        role=MessageRole.coordinator,
        priority=MessagePriority.high,
        payload={"files": ["a.py", "b.py"]},
    )
    d = msg.to_dict()
    assert d["sender"] == "coordinator"
    assert d["recipient"] == "coding"
    assert d["priority"] == "high"
    assert d["payload"]["files"] == ["a.py", "b.py"]
    assert d["message_id"] == msg.message_id

    rebuilt = AgentMessage.from_dict(d)
    assert rebuilt.sender == AgentRole.coordinator
    assert rebuilt.recipient == AgentRole.coding
    assert rebuilt.content == msg.content
    assert rebuilt.priority == MessagePriority.high


def test_agent_message_defaults():
    msg = AgentMessage(sender=AgentRole.planner, recipient=AgentRole.repository,
                       content="hi")
    assert msg.role == MessageRole.agent
    assert msg.priority == MessagePriority.normal
    assert msg.payload == {}
    assert len(msg.message_id) == 16


# ── AgentResult ───────────────────────────────────────────────────────────────
def test_agent_result_to_dict_handles_none_next_agent():
    r = AgentResult(success=True, agent=AgentRole.planner, summary="ok",
                    data={"steps": 3})
    d = r.to_dict()
    assert d["success"] is True
    assert d["agent"] == "planner"
    assert d["next_agent"] is None
    assert d["data"]["steps"] == 3
    assert d["messages"] == []


def test_agent_result_with_next_agent_hint():
    r = AgentResult(success=False, agent=AgentRole.testing,
                    next_agent=AgentRole.coding, summary="tests failed")
    d = r.to_dict()
    assert d["next_agent"] == "coding"


# ── AgentContext ──────────────────────────────────────────────────────────────
def test_agent_context_emit_event_calls_callback():
    received = []

    def emit(etype, message, **data):
        received.append((etype, message, data))

    ctx = AgentContext(task_id="t1", description="d", workspace=None,
                       settings=None, emit=emit)
    ctx.emit_event("info", "hello", agent="planner")
    assert received == [("info", "hello", {"agent": "planner"})]


def test_agent_context_emit_event_swallows_callback_errors():
    def bad_emit(etype, message, **data):
        raise RuntimeError("boom")

    ctx = AgentContext(task_id="t1", description="d", workspace=None,
                       settings=None, emit=bad_emit)
    # Must NOT raise — a broken UI bridge must never crash an agent.
    ctx.emit_event("info", "hello")


def test_agent_context_is_cancelled_with_event():
    ctx = AgentContext(task_id="t1", description="d", workspace=None,
                       settings=None, cancel=threading.Event())
    assert ctx.is_cancelled() is False
    ctx.cancel.set()
    assert ctx.is_cancelled() is True


def test_agent_context_is_cancelled_without_event():
    ctx = AgentContext(task_id="t1", description="d", workspace=None, settings=None)
    assert ctx.is_cancelled() is False


# ── BaseAgent contract ────────────────────────────────────────────────────────
class _OkAgent(BaseAgent):
    role = AgentRole.planner
    name = "OkAgent"

    def handle(self, ctx, message):
        return AgentResult(success=True, agent=self.role, summary="done")


class _BoomAgent(BaseAgent):
    role = AgentRole.coding
    name = "BoomAgent"

    def handle(self, ctx, message):
        raise ValueError("kaboom")


def test_base_agent_run_success():
    agent = _OkAgent()
    ctx = AgentContext(task_id="t", description="d", workspace=None, settings=None)
    msg = AgentMessage(sender=AgentRole.coordinator, recipient=AgentRole.planner,
                       content="go")
    res = agent.run(ctx, msg)
    assert res.success is True
    assert res.agent == AgentRole.planner


def test_base_agent_run_swallows_handle_exception():
    agent = _BoomAgent()
    ctx = AgentContext(task_id="t", description="d", workspace=None, settings=None)
    msg = AgentMessage(sender=AgentRole.coordinator, recipient=AgentRole.coding,
                       content="go")
    res = agent.run(ctx, msg)
    # Errors surface honestly — never faked as success.
    assert res.success is False
    assert "kaboom" in res.summary
    assert res.data["error"] == "kaboom"


def test_base_agent_run_respects_cancellation():
    agent = _OkAgent()
    cancel = threading.Event()
    cancel.set()
    ctx = AgentContext(task_id="t", description="d", workspace=None,
                       settings=None, cancel=cancel)
    msg = AgentMessage(sender=AgentRole.coordinator, recipient=AgentRole.planner,
                       content="go")
    res = agent.run(ctx, msg)
    assert res.success is False
    assert "cancel" in res.summary.lower()


def test_base_agent_reply_builds_message():
    agent = _OkAgent()
    reply = agent.reply(AgentRole.coordinator, "done", files=["a.py"])
    assert reply.sender == AgentRole.planner
    assert reply.recipient == AgentRole.coordinator
    assert reply.payload == {"files": ["a.py"]}


# ── Registry ──────────────────────────────────────────────────────────────────
def test_registry_register_and_get():
    reg = AgentRegistry()
    reg.register(AgentRole.review, _OkAgent)
    assert reg.get_class(AgentRole.review) is _OkAgent
    # Singleton instantiation.
    inst = reg.get(AgentRole.review)
    assert isinstance(inst, _OkAgent)
    # Second get returns a fresh singleton-stable instance.
    assert reg.get(AgentRole.review) is inst


def test_registry_roles_lists_registered():
    reg = AgentRegistry()
    reg.register(AgentRole.planner, _OkAgent)
    reg.register(AgentRole.coding, _BoomAgent)
    roles = reg.roles()
    assert AgentRole.planner in roles
    assert AgentRole.coding in roles


def test_registry_clear():
    reg = AgentRegistry()
    reg.register(AgentRole.planner, _OkAgent)
    reg.clear()
    assert reg.get(AgentRole.planner) is None


def test_register_agent_decorator_registers_class():
    # The decorator always targets the global registry; use the coordinator
    # role (unused by the 7 specialized agents) so we don't disturb them.
    reg = get_registry()
    prior = reg.get_class(AgentRole.coordinator)

    @register_agent(AgentRole.coordinator)
    class _DecTest(BaseAgent):
        role = AgentRole.coordinator
        name = "DecTest"

        def handle(self, ctx, message):
            return AgentResult(success=True, agent=self.role)

    assert reg.get_class(AgentRole.coordinator) is _DecTest

    # Restore prior state so we don't leak a test agent into the global registry.
    if prior is not None:
        reg.register(AgentRole.coordinator, prior)
    else:
        # No prior coordinator class: drop our test registration cleanly by
        # re-registering the real AgentCoordinator if it was imported.
        try:
            from agents.coordinator import AgentCoordinator
            reg.register(AgentRole.coordinator, AgentCoordinator)
        except Exception:
            pass


def test_global_registry_has_all_seven_specialized_agents():
    """Importing the agents package must register all 7 specialized agents."""
    import agents  # noqa: F401  (triggers registration via __init__)
    reg = get_registry()
    expected = {AgentRole.planner, AgentRole.repository, AgentRole.coding,
                AgentRole.terminal, AgentRole.testing, AgentRole.git,
                AgentRole.review}
    registered = set(reg.roles())
    assert expected.issubset(registered), (
        f"missing agents: {expected - registered}")
