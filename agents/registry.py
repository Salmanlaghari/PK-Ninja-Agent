"""Agent registry: maps :class:`AgentRole` to agent classes and instances.

The registry is how the coordinator discovers which agents exist without hard
imports of every specialized agent. Agents self-register on import via
:func:`register_agent`, so the coordinator stays decoupled.

This keeps the architecture open for extension: add a new agent file, decorate
it with ``@register_agent``, and the coordinator will route to it
automatically.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Type

from agents.base import AgentRole, BaseAgent

log = logging.getLogger("pk_ninja.agents.registry")


class AgentRegistry:
    """Central registry of agent classes keyed by :class:`AgentRole`."""

    def __init__(self) -> None:
        self._classes: Dict[AgentRole, Type[BaseAgent]] = {}
        self._instances: Dict[AgentRole, BaseAgent] = {}

    def register(self, role: AgentRole, cls: Type[BaseAgent]) -> None:
        if not issubclass(cls, BaseAgent):
            raise TypeError(f"{cls} must subclass BaseAgent")
        self._classes[role] = cls
        # Invalidate any cached instance so the next get() builds fresh.
        self._instances.pop(role, None)
        log.debug("registered agent %s -> %s", role.value, cls.__name__)

    def get_class(self, role: AgentRole) -> Optional[Type[BaseAgent]]:
        return self._classes.get(role)

    def get(self, role: AgentRole) -> Optional[BaseAgent]:
        """Return a singleton instance of the agent for ``role``."""
        if role not in self._classes:
            return None
        if role not in self._instances:
            self._instances[role] = self._classes[role]()
        return self._instances[role]

    def roles(self) -> List[AgentRole]:
        return list(self._classes.keys())

    def clear(self) -> None:
        self._classes.clear()
        self._instances.clear()


# Module-level singleton.
_REGISTRY = AgentRegistry()


def get_registry() -> AgentRegistry:
    return _REGISTRY


def register_agent(role: AgentRole):
    """Class decorator: register an agent class under ``role``."""

    def deco(cls: Type[BaseAgent]) -> Type[BaseAgent]:
        get_registry().register(role, cls)
        return cls

    return deco
