"""Planner Agent.

Understands the user's goal and breaks the work into executable steps.

Provider-independent: it uses whatever ``ctx.provider`` is set (conforms to the
``AIProvider`` protocol). When no provider or the local provider is present, it
produces a deterministic, keyword-driven plan so the architecture always makes
progress without an API key.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from agents.base import (
    AgentContext,
    AgentMessage,
    AgentResult,
    AgentRole,
    BaseAgent,
)
from agents.registry import register_agent

log = logging.getLogger("pk_ninja.agents.planner")


@register_agent(AgentRole.planner)
class PlannerAgent(BaseAgent):
    role = AgentRole.planner
    name = "PlannerAgent"

    def handle(self, ctx: AgentContext, message: AgentMessage) -> AgentResult:
        ctx.emit_event("analyzing", "Planner agent: understanding the user's goal.")

        steps = self._produce_plan(ctx)

        # Store the plan in the shared context for later agents.
        ctx.plan = steps

        ctx.emit_event(
            "planning",
            f"Plan produced with {len(steps)} steps.",
            steps=[s["description"] for s in steps],
            plan_steps=steps,
            agent=AgentRole.planner.value,
        )

        return AgentResult(
            success=True,
            agent=self.role,
            summary=f"Produced a {len(steps)}-step plan.",
            data={"plan": steps},
            messages=[
                self.reply(
                    AgentRole.repository,
                    "Here is the plan; gather repository context for these steps.",
                    plan=steps,
                )
            ],
            next_agent=AgentRole.repository,
        )

    # ── Plan generation ──────────────────────────────────────────────────────
    def _produce_plan(self, ctx: AgentContext) -> List[Dict[str, Any]]:
        """Return a list of plan steps: [{'id', 'description', 'status', 'retries'}]."""
        provider = ctx.provider
        # Try a real provider that can stream/generate.
        if provider is not None and not _is_local(provider):
            try:
                steps = self._plan_with_provider(ctx, provider)
                if steps:
                    return _normalize_steps(steps)
            except Exception as exc:  # honest fallback, never fake
                ctx.emit_event("error", f"Planner provider error: {exc}; using deterministic plan.")
                log.warning("planner provider failed: %s", exc)

        # Deterministic keyword-driven plan (no API key needed).
        return self._deterministic_plan(ctx)

    def _plan_with_provider(self, ctx: AgentContext, provider: Any) -> List[str]:
        """Ask the provider for a plan as a JSON {'summary','steps'} object."""
        from ai_provider import ChatMessage  # local import keeps base clean

        messages = [
            ChatMessage(
                "system",
                "You are a planning agent. Break the task into small executable "
                "steps. Output JSON with keys 'summary' (string) and 'steps' "
                "(array of short strings). Return ONLY JSON.",
            ),
            ChatMessage("user", f"Task:\n{ctx.description}"),
        ]
        text = ""
        if hasattr(provider, "generate"):
            text = provider.generate(messages)
        elif hasattr(provider, "stream_chat"):
            text = provider.stream_chat(messages).text
        import re

        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return []
        obj = json.loads(m.group(0))
        return obj.get("steps", []) if isinstance(obj, dict) else []

    def _deterministic_plan(self, ctx: AgentContext) -> List[Dict[str, Any]]:
        """A keyword-driven plan that always makes sense for a coding task."""
        desc = (ctx.description or "").lower()
        steps: List[str] = []

        steps.append("Search the repository and identify relevant files and symbols.")
        if any(k in desc for k in ("readme", "doc", "document")):
            steps.append("Create or update documentation (README/docs) describing the project.")
        if any(k in desc for k in ("docstring", "doc string", "comment")):
            steps.append("Add module/function docstrings to the relevant source files.")
        if any(k in desc for k in ("test", "spec", "coverage")):
            steps.append("Add or update unit tests for the changed behavior.")
        if any(k in desc for k in ("refactor", "restructure", "rename")):
            steps.append("Refactor the identified code and update all references.")
        if any(k in desc for k in ("fix", "bug", "error", "resolve")):
            steps.append("Apply the fix to the affected files.")
        if any(k in desc for k in ("add", "implement", "create", "build", "feature")):
            steps.append("Implement the new functionality in the appropriate files.")
        # Always-present safety-net steps.
        steps.append("Apply file edits safely in the workspace.")
        steps.append("Run the verification/test command and confirm it passes.")
        steps.append("Review the generated code for issues before committing.")
        steps.append("Prepare a git branch, commit, and push for a pull request.")

        return _normalize_steps(steps)


# ── helpers ───────────────────────────────────────────────────────────────────
def _is_local(provider: Any) -> bool:
    try:
        from ai_provider import LocalProvider

        return isinstance(provider, LocalProvider)
    except Exception:
        return False


def _normalize_steps(steps: List[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i, s in enumerate(steps):
        if isinstance(s, dict):
            desc = s.get("description") or s.get("step") or str(s)
        else:
            desc = str(s)
        out.append({"id": i + 1, "description": desc, "status": "pending", "retries": 0})
    return out
