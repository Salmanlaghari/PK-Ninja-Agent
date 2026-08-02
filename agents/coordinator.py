"""Agent Coordinator.

The coordinator is the orchestrator of the multi-agent architecture. It:

  * Decides which agent should work next based on the current state and the
    most recent agent result / message.
  * Routes structured :class:`AgentMessage` objects between agents.
  * Keeps a conversation log (the ordered list of messages) for transparency.
  * Enforces a maximum iteration budget so a broken agent loop can't run
    forever.
  * Streams real events into the existing UI via ``ctx.emit``.

Routing is state-machine based (see :class:`CoordinatorState`) plus an optional
``next_agent`` hint that an agent may return to short-circuit the default
transition (e.g. Testing can say "go back to Coding to fix this failure").

The coordinator never fakes activity: if no agent is available it reports an
honest error and stops.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Dict, List, Optional

from agents.base import (
    AgentContext,
    AgentMessage,
    AgentResult,
    AgentRole,
    BaseAgent,
    MessageRole,
)
from agents.registry import get_registry

log = logging.getLogger("pk_ninja.agents.coordinator")


class CoordinatorState(str, Enum):
    """Linear-with-feedback states the coordinator moves through.

    The happy path is:
      planning -> repository -> coding -> terminal -> testing -> review -> git -> done

    Feedback loops:
      testing  --(failure)--> coding   (fix the code)
      review   --(issues)----> coding   (address review feedback)
      any      --(cancelled)--> done
    """

    init = "init"
    planning = "planning"
    repository = "repository"
    coding = "coding"
    terminal = "terminal"
    testing = "testing"
    review = "review"
    git = "git"
    done = "done"
    failed = "failed"


# Map state -> agent role that should run in that state.
_STATE_AGENT: Dict[CoordinatorState, AgentRole] = {
    CoordinatorState.planning: AgentRole.planner,
    CoordinatorState.repository: AgentRole.repository,
    CoordinatorState.coding: AgentRole.coding,
    CoordinatorState.terminal: AgentRole.terminal,
    CoordinatorState.testing: AgentRole.testing,
    CoordinatorState.review: AgentRole.review,
    CoordinatorState.git: AgentRole.git,
}

# Default linear transitions on success.
_NEXT_ON_SUCCESS: Dict[CoordinatorState, CoordinatorState] = {
    CoordinatorState.init: CoordinatorState.planning,
    CoordinatorState.planning: CoordinatorState.repository,
    CoordinatorState.repository: CoordinatorState.coding,
    CoordinatorState.coding: CoordinatorState.terminal,
    CoordinatorState.terminal: CoordinatorState.testing,
    CoordinatorState.testing: CoordinatorState.review,
    CoordinatorState.review: CoordinatorState.git,
    CoordinatorState.git: CoordinatorState.done,
}


class AgentCoordinator(BaseAgent):
    """Orchestrates the specialized agents.

    Usage::

        coord = AgentCoordinator()
        result = coord.execute(ctx)
    """

    role = AgentRole.coordinator
    name = "AgentCoordinator"

    # Safety budget: never spin forever even if agents keep handing off.
    MAX_ITERATIONS = 30
    # How many times we allow a testing/review -> coding feedback loop.
    MAX_FIX_ROUNDS = 2

    def __init__(self, registry=None) -> None:
        super().__init__()
        self._registry = registry or get_registry()
        self.state: CoordinatorState = CoordinatorState.init
        self.messages: List[AgentMessage] = []
        self.results: List[AgentResult] = []
        self.fix_rounds: int = 0

    # ── Public API ───────────────────────────────────────────────────────────
    def execute(self, ctx: AgentContext) -> AgentResult:
        """Run the full orchestration loop and return a final result."""
        self.state = CoordinatorState.init
        self.messages = []
        self.results = []
        self.fix_rounds = 0

        self._announce(ctx, "Multi-agent orchestration started.", agents=self._agent_names())

        iterations = 0
        last_result: Optional[AgentResult] = None

        while self.state not in (CoordinatorState.done, CoordinatorState.failed):
            if ctx.is_cancelled():
                self._announce(ctx, "Orchestration cancelled by user.")
                return AgentResult(
                    success=False, agent=self.role, summary="cancelled",
                    data={"state": self.state.value, "messages": [m.to_dict() for m in self.messages]},
                )
            if iterations >= self.MAX_ITERATIONS:
                self._announce(ctx, f"Reached max iterations ({self.MAX_ITERATIONS}); stopping.")
                return AgentResult(
                    success=False, agent=self.role,
                    summary="max iterations reached",
                    data={"state": self.state.value},
                )

            iterations += 1

            # Decide next state + agent.
            self.state = self._next_state(last_result)
            if self.state in (CoordinatorState.done, CoordinatorState.failed):
                break

            agent_role = _STATE_AGENT.get(self.state)
            agent = self._registry.get(agent_role) if agent_role else None
            if agent is None:
                self._announce(ctx, f"No agent registered for state {self.state.value}; stopping.")
                return AgentResult(
                    success=False, agent=self.role,
                    summary=f"no agent for state {self.state.value}",
                    data={"state": self.state.value},
                )

            # Build the message to send to this agent.
            msg = self._build_message_for(agent_role, last_result)
            self.messages.append(msg)
            self._announce(ctx, f"Dispatching to {agent.name} ({self.state.value}).",
                           agent=agent_role.value, state=self.state.value)

            result = agent.run(ctx, msg)
            self.results.append(result)
            self._record_result(ctx, agent_role, result)

            if not result.success:
                # Decide whether it's recoverable (fix loop) or fatal.
                if self._is_recoverable(agent_role) and self.fix_rounds < self.MAX_FIX_ROUNDS:
                    self.fix_rounds += 1
                    self._announce(ctx,
                                   f"{agent.name} reported a failure; routing back to Coding for a fix "
                                   f"(round {self.fix_rounds}/{self.MAX_FIX_ROUNDS}).",
                                   agent=agent_role.value)
                    # Force next state to coding and carry the failure result.
                    last_result = AgentResult(
                        success=False, agent=agent_role,
                        summary=result.summary or f"{agent.name} failed",
                        data=result.data,
                        next_agent=AgentRole.coding,
                    )
                    self.state = CoordinatorState.coding
                    continue
                else:
                    self._announce(ctx, f"{agent.name} failed and cannot recover; stopping.",
                                   agent=agent_role.value)
                    self.state = CoordinatorState.failed
                    break

            last_result = result

        success = self.state == CoordinatorState.done
        summary = "Multi-agent orchestration completed." if success else "Orchestration ended without completion."
        self._announce(ctx, summary, state=self.state.value, success=success)

        return AgentResult(
            success=success,
            agent=self.role,
            summary=summary,
            data={
                "state": self.state.value,
                "iterations": iterations,
                "fix_rounds": self.fix_rounds,
                "messages": [m.to_dict() for m in self.messages],
                "results": [r.to_dict() for r in self.results],
            },
        )

    # ── Routing logic ────────────────────────────────────────────────────────
    def _next_state(self, last_result: Optional[AgentResult]) -> CoordinatorState:
        """Decide the next coordinator state from the last result."""
        # Honor an explicit next_agent hint from the previous agent.
        if last_result and last_result.next_agent:
            hinted = self._role_to_state(last_result.next_agent)
            if hinted is not None:
                return hinted

        # Default linear progression.
        return _NEXT_ON_SUCCESS.get(self.state, CoordinatorState.done)

    @staticmethod
    def _role_to_state(role: AgentRole) -> Optional[CoordinatorState]:
        for state, r in _STATE_AGENT.items():
            if r == role:
                return state
        return None

    @staticmethod
    def _is_recoverable(role: AgentRole) -> bool:
        # Testing and Review failures are recoverable (route back to Coding).
        # Terminal/Coding failures may also be recoverable if not unsafe.
        return role in (AgentRole.testing, AgentRole.review, AgentRole.coding, AgentRole.terminal)

    # ── Message construction ─────────────────────────────────────────────────
    def _build_message_for(self, role: AgentRole, last_result: Optional[AgentResult]) -> AgentMessage:
        """Build the structured message that the coordinator sends to ``role``."""
        content_map = {
            AgentRole.planner: "Understand the user's goal and produce an executable plan.",
            AgentRole.repository: "Search the repository, build project context, and find relevant files and symbols.",
            AgentRole.coding: "Apply the planned edits: modify files, generate code, refactor, and apply edits safely.",
            AgentRole.terminal: "Execute any sandboxed commands needed and stream real output.",
            AgentRole.testing: "Run tests, analyze failures, and request fixes if necessary.",
            AgentRole.review: "Review the generated code, detect potential issues, and suggest improvements before commit.",
            AgentRole.git: "Handle git status, diff, branch management, commit, push, and PR preparation.",
        }
        payload: Dict[str, Any] = {}
        if last_result:
            payload["previous"] = {
                "agent": last_result.agent.value,
                "summary": last_result.summary,
                "data": last_result.data,
            }
        return AgentMessage(
            sender=AgentRole.coordinator,
            recipient=role,
            role=MessageRole.coordinator,
            content=content_map.get(role, ""),
            payload=payload,
        )

    # ── UI streaming helpers ─────────────────────────────────────────────────
    def _announce(self, ctx: AgentContext, message: str, **data: Any) -> None:
        log.info("[coordinator] %s", message)
        ctx.emit_event("info", message, coordinator=True, **data)

    def _record_result(self, ctx: AgentContext, role: AgentRole, result: AgentResult) -> None:
        ctx.emit_event(
            "info",
            f"{role.value} agent {'succeeded' if result.success else 'failed'}: {result.summary}",
            agent=role.value, success=result.success, agent_data=result.data,
        )

    def _agent_names(self) -> List[str]:
        return [r.value for r in self._registry.roles()]

    # BaseAgent handle — used if the coordinator is ever invoked as an agent.
    def handle(self, ctx: AgentContext, message: AgentMessage) -> AgentResult:
        return self.execute(ctx)
