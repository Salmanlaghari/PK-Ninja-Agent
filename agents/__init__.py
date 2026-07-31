"""PK-Ninja-Agent Multi-Agent Architecture.

A provider-independent multi-agent layer that orchestrates specialized agents
(Planner, Repository, Coding, Terminal, Testing, Git, Review) coordinated by
an AgentCoordinator.

The agents communicate exclusively through structured :class:`AgentMessage`
objects and return :class:`AgentResult` objects. None of the agents import a
specific AI provider; they accept a provider that conforms to the
``AIProvider`` protocol defined in ``ai_provider.py``.

This package is *additive*: the existing ``agent.Agent`` loop keeps working
unchanged. The coordinator can be used as an opt-in orchestration path.
"""

from agents.base import (  # noqa: F401
    AgentContext,
    AgentMessage,
    AgentResult,
    AgentRole,
    BaseAgent,
    MessagePriority,
    MessageRole,
)
from agents.coordinator import (  # noqa: F401
    AgentCoordinator,
    CoordinatorState,
)
from agents.registry import (  # noqa: F401
    AgentRegistry,
    get_registry,
    register_agent,
)

# Importing the specialized agent modules triggers their @register_agent
# decorators, so the registry is fully populated whenever the package is
# imported. Each module is lightweight (no heavy work at import time) and
# provider-independent, so this is safe and cheap.
from agents import planner_agent  # noqa: F401
from agents import repository_agent  # noqa: F401
from agents import coding_agent  # noqa: F401
from agents import terminal_agent  # noqa: F401
from agents import testing_agent  # noqa: F401
from agents import git_agent  # noqa: F401
from agents import review_agent  # noqa: F401

__all__ = [
    "AgentContext",
    "AgentCoordinator",
    "AgentMessage",
    "AgentRegistry",
    "AgentResult",
    "AgentRole",
    "BaseAgent",
    "CoordinatorState",
    "MessagePriority",
    "MessageRole",
    "get_registry",
    "register_agent",
]
