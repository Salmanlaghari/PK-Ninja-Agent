"""Tests for the AgentCoordinator state machine and routing logic.

These use a fake registry of stub agents so the coordinator's routing, feedback
loops, iteration cap, cancellation, and message construction can be exercised
deterministically without any real workspace, provider, or network.
"""
import threading

import pytest

from agents.base import (
    AgentContext,
    AgentMessage,
    AgentResult,
    AgentRole,
    BaseAgent,
    MessageRole,
)
from agents.coordinator import (
    AgentCoordinator,
    CoordinatorState,
    _NEXT_ON_SUCCESS,
    _STATE_AGENT,
)
from agents.registry import AgentRegistry


# ── Stub agent toolkit ────────────────────────────────────────────────────────
class _StubAgent(BaseAgent):
    """Configurable stub class. The factory + call log live on the class so the
    registry (which instantiates with no args) can build it."""

    # Class-level config: set per subclass.
    _factory = None
    _calls = None

    def handle(self, ctx, message):
        if self._calls is not None:
            self._calls.append(message)
        return self._factory(ctx, message)


def _make_stub_class(role, factory, calls):
    """Create a _StubAgent subclass carrying a factory + shared call log."""
    cls = type(
        f"Stub{role.value.capitalize()}",
        (_StubAgent,),
        {"role": role, "name": f"Stub{role.value.capitalize()}",
         "_factory": staticmethod(factory), "_calls": calls},
    )
    return cls


def _result(success, agent, *, next_agent=None, summary="", data=None):
    return AgentResult(success=success, agent=agent, next_agent=next_agent,
                       summary=summary, data=data or {})


def _make_ctx(**overrides):
    base = dict(task_id="c1", description="do the thing", workspace=None,
                settings=None, cancel=threading.Event())
    base.update(overrides)
    return AgentContext(**base)


def _make_registry(role_to_factory):
    """Build a registry mapping each role -> a stub agent class built from a
    factory. Returns (registry, {role: call_log})."""
    reg = AgentRegistry()
    calls = {}
    for role, factory in role_to_factory.items():
        calls[role] = []
        reg.register(role, _make_stub_class(role, factory, calls[role]))
    return reg, calls


def _all_success_factories():
    """Every agent succeeds and follows the linear happy path."""
    factories = {}
    for state, role in _STATE_AGENT.items():
        def _f(ctx, message, role=role):
            return _result(True, role, summary=f"{role.value} ok")
        factories[role] = _f
    return factories


# ── Happy path ────────────────────────────────────────────────────────────────
def test_coordinator_happy_path_reaches_done():
    reg, calls = _make_registry(_all_success_factories())
    coord = AgentCoordinator(registry=reg)
    ctx = _make_ctx()
    res = coord.execute(ctx)

    assert res.success is True
    assert res.data["state"] == CoordinatorState.done.value
    # 7 agent dispatches (one per state) + the init->planning transition.
    assert res.data["iterations"] == 8
    assert res.data["fix_rounds"] == 0
    # Every agent was dispatched exactly once.
    for role, msgs in calls.items():
        assert len(msgs) == 1, f"{role.value} called {len(msgs)} times"
    # A conversation log of structured messages was kept.
    assert len(coord.messages) == 7
    assert all(isinstance(m, AgentMessage) for m in coord.messages)


def test_coordinator_emits_real_events_into_callback():
    reg, _ = _make_registry(_all_success_factories())
    coord = AgentCoordinator(registry=reg)
    events = []
    ctx = _make_ctx(emit=lambda etype, msg, **d: events.append((etype, msg)))
    coord.execute(ctx)
    # The coordinator must announce start, dispatches, results, and completion.
    assert events, "coordinator emitted no events"
    assert any("started" in m for _, m in events)
    assert any("completed" in m for _, m in events)
    # Dispatch announcements reference each agent role.
    for role in _STATE_AGENT.values():
        assert any(role.value in m for _, m in events), f"no event for {role.value}"


# ── Feedback loops ────────────────────────────────────────────────────────────
def test_testing_failure_routes_back_to_coding_then_completes():
    """Testing fails once (requests coding fix), then succeeds -> done."""
    call_count = {"testing": 0, "coding": 0}
    factories = _all_success_factories()

    def coding_factory(ctx, msg):
        call_count["coding"] += 1
        return _result(True, AgentRole.coding, next_agent=AgentRole.terminal,
                       summary="coded")

    def testing_factory(ctx, msg):
        call_count["testing"] += 1
        if call_count["testing"] == 1:
            # First run fails and asks for a fix.
            return _result(False, AgentRole.testing, next_agent=AgentRole.coding,
                           summary="tests failed", data={"failing_files": ["a.py"]})
        return _result(True, AgentRole.testing, next_agent=AgentRole.review,
                       summary="tests pass")

    factories[AgentRole.coding] = coding_factory
    factories[AgentRole.testing] = testing_factory
    reg, _ = _make_registry(factories)

    coord = AgentCoordinator(registry=reg)
    res = coord.execute(_make_ctx())

    assert res.success is True
    assert res.data["state"] == "done"
    assert res.data["fix_rounds"] == 1
    # Coding ran twice (once before terminal, once after the fix request).
    assert call_count["coding"] == 2
    assert call_count["testing"] == 2


def test_review_failure_routes_back_to_coding():
    call_count = {"review": 0, "coding": 0}
    factories = _all_success_factories()

    def coding_factory(ctx, msg):
        call_count["coding"] += 1
        return _result(True, AgentRole.coding, next_agent=AgentRole.terminal)

    def review_factory(ctx, msg):
        call_count["review"] += 1
        if call_count["review"] == 1:
            return _result(False, AgentRole.review, next_agent=AgentRole.coding,
                           summary="review found errors",
                           data={"errors": ["syntax"]})
        return _result(True, AgentRole.review, next_agent=AgentRole.git,
                       summary="review passed")

    factories[AgentRole.coding] = coding_factory
    factories[AgentRole.review] = review_factory
    reg, _ = _make_registry(factories)

    coord = AgentCoordinator(registry=reg)
    res = coord.execute(_make_ctx())

    assert res.success is True
    assert res.data["fix_rounds"] == 1
    assert call_count["review"] == 2


def test_unrecoverable_failure_stops_orchestration():
    """A planner failure is NOT recoverable -> orchestration fails fast."""
    factories = _all_success_factories()
    factories[AgentRole.planner] = lambda ctx, msg: _result(
        False, AgentRole.planner, summary="planner exploded")
    reg, _ = _make_registry(factories)

    coord = AgentCoordinator(registry=reg)
    res = coord.execute(_make_ctx())

    assert res.success is False
    assert res.data["state"] == "failed"


def test_exceeding_max_fix_rounds_fails():
    """Testing keeps failing forever -> stops after MAX_FIX_ROUNDS."""
    factories = _all_success_factories()
    factories[AgentRole.testing] = lambda ctx, msg: _result(
        False, AgentRole.testing, next_agent=AgentRole.coding,
        summary="still failing")
    factories[AgentRole.coding] = lambda ctx, msg: _result(
        True, AgentRole.coding, next_agent=AgentRole.terminal)
    reg, _ = _make_registry(factories)

    coord = AgentCoordinator(registry=reg)
    res = coord.execute(_make_ctx())

    assert res.success is False
    assert res.data["fix_rounds"] == AgentCoordinator.MAX_FIX_ROUNDS


# ── Safety budgets ────────────────────────────────────────────────────────────
def test_iteration_cap_prevents_infinite_loop():
    """An agent that never advances state must hit MAX_ITERATIONS, not loop."""
    # coding always hints back to itself -> would loop forever without the cap.
    factories = _all_success_factories()
    factories[AgentRole.coding] = lambda ctx, msg: _result(
        True, AgentRole.coding, next_agent=AgentRole.coding, summary="loop")
    reg, _ = _make_registry(factories)

    coord = AgentCoordinator(registry=reg)
    coord.MAX_ITERATIONS = 20  # keep the test fast
    res = coord.execute(_make_ctx())

    assert res.success is False
    assert "max iterations" in res.summary.lower()


def test_cancellation_stops_orchestration():
    reg, _ = _make_registry(_all_success_factories())
    coord = AgentCoordinator(registry=reg)
    ctx = _make_ctx()
    ctx.cancel.set()
    res = coord.execute(ctx)
    assert res.success is False
    assert res.summary == "cancelled"


def test_missing_agent_for_state_fails_honestly():
    """If no agent is registered for a needed state, stop honestly."""
    # Register only the planner; the very next state (repository) has no agent.
    reg = AgentRegistry()
    reg.register(AgentRole.planner, _make_stub_class(
        AgentRole.planner,
        lambda ctx, msg: _result(True, AgentRole.planner,
                                 next_agent=AgentRole.repository),
        []))
    coord = AgentCoordinator(registry=reg)
    res = coord.execute(_make_ctx())
    assert res.success is False
    assert "no agent" in res.summary.lower()


# ── Routing helpers ───────────────────────────────────────────────────────────
def test_next_state_honors_next_agent_hint():
    coord = AgentCoordinator(registry=AgentRegistry())
    coord.state = CoordinatorState.testing
    hinted = AgentResult(success=True, agent=AgentRole.testing,
                         next_agent=AgentRole.coding)
    assert coord._next_state(hinted) == CoordinatorState.coding


def test_next_state_linear_progression_without_hint():
    coord = AgentCoordinator(registry=AgentRegistry())
    coord.state = CoordinatorState.planning
    assert coord._next_state(None) == CoordinatorState.repository


def test_is_recoverable_classification():
    assert AgentCoordinator._is_recoverable(AgentRole.testing) is True
    assert AgentCoordinator._is_recoverable(AgentRole.review) is True
    assert AgentCoordinator._is_recoverable(AgentRole.coding) is True
    assert AgentCoordinator._is_recoverable(AgentRole.terminal) is True
    assert AgentCoordinator._is_recoverable(AgentRole.planner) is False
    assert AgentCoordinator._is_recoverable(AgentRole.git) is False


def test_role_to_state_mapping():
    assert AgentCoordinator._role_to_state(AgentRole.planner) == CoordinatorState.planning
    assert AgentCoordinator._role_to_state(AgentRole.git) == CoordinatorState.git
    assert AgentCoordinator._role_to_state(AgentRole.coordinator) is None


# ── Message construction ──────────────────────────────────────────────────────
def test_build_message_for_includes_previous_result():
    coord = AgentCoordinator(registry=AgentRegistry())
    prev = AgentResult(success=False, agent=AgentRole.testing,
                       summary="tests failed", data={"failing_files": ["x.py"]})
    msg = coord._build_message_for(AgentRole.coding, prev)
    assert msg.sender == AgentRole.coordinator
    assert msg.recipient == AgentRole.coding
    assert msg.role == MessageRole.coordinator
    assert msg.payload["previous"]["agent"] == "testing"
    assert msg.payload["previous"]["summary"] == "tests failed"


def test_build_message_for_without_previous():
    coord = AgentCoordinator(registry=AgentRegistry())
    msg = coord._build_message_for(AgentRole.planner, None)
    assert msg.recipient == AgentRole.planner
    assert "previous" not in msg.payload


def test_state_agent_map_covers_seven_roles():
    assert len(_STATE_AGENT) == 7
    expected_roles = {AgentRole.planner, AgentRole.repository, AgentRole.coding,
                      AgentRole.terminal, AgentRole.testing, AgentRole.review,
                      AgentRole.git}
    assert set(_STATE_AGENT.values()) == expected_roles


def test_next_on_success_terminates_at_done():
    assert _NEXT_ON_SUCCESS[CoordinatorState.git] == CoordinatorState.done
    assert _NEXT_ON_SUCCESS[CoordinatorState.init] == CoordinatorState.planning
