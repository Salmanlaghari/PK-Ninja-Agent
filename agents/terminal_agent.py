"""Terminal Agent.

Executes sandboxed commands, streams real output, and reports errors.

It reuses the existing ``terminal.run_command`` + ``terminal.validate_command``
layer verbatim, so all command-injection protections, the dangerous-command
blocklist, and the live subprocess streaming stay exactly as they are. The
agent never shells out directly and never fakes output.

If no command is needed for this task (e.g. a pure-edit task), the agent
honestly reports "no command required" and succeeds.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from agents.base import (
    AgentContext,
    AgentMessage,
    AgentResult,
    AgentRole,
    BaseAgent,
)
from agents.registry import register_agent

log = logging.getLogger("pk_ninja.agents.terminal")


@register_agent(AgentRole.terminal)
class TerminalAgent(BaseAgent):
    role = AgentRole.terminal
    name = "TerminalAgent"

    def handle(self, ctx: AgentContext, message: AgentMessage) -> AgentResult:
        ws = ctx.workspace
        commands = self._choose_commands(ctx, message)

        if not commands:
            ctx.emit_event(
                "info",
                "Terminal agent: no command required for this task.",
                agent=AgentRole.terminal.value,
            )
            return AgentResult(
                success=True,
                agent=self.role,
                summary="No command required.",
                data={"commands": []},
                messages=[self.reply(AgentRole.testing, "No terminal work; proceed to testing.")],
                next_agent=AgentRole.testing,
            )

        from terminal import TerminalError, run_command, validate_command

        results: List[Dict[str, Any]] = []
        all_success = True
        for cmd in commands:
            if ctx.is_cancelled():
                break
            try:
                decision = validate_command(cmd)
                warning = decision.warning if decision.allowed else None
                ctx.emit_event("command_started", f"$ {cmd}",
                               warning=warning or "", agent=AgentRole.terminal.value)
                result = run_command(cmd, ws, rt=_rt_for(ctx))
                ctx.emit_event(
                    "command_output",
                    result.stdout or result.stderr or "(no output)",
                    stdout=result.stdout, stderr=result.stderr,
                    returncode=result.returncode, agent=AgentRole.terminal.value,
                )
                ctx.emit_event(
                    "command_finished",
                    f"exit {result.returncode}",
                    returncode=result.returncode, success=result.success,
                    agent=AgentRole.terminal.value,
                )
                results.append({
                    "command": cmd, "returncode": result.returncode,
                    "stdout": result.stdout, "stderr": result.stderr,
                    "success": result.success,
                })
                if not result.success:
                    all_success = False
            except TerminalError as exc:
                # Policy rejection — honest, never retried silently.
                ctx.emit_event("error", f"Terminal rejected command: {exc}", command=cmd,
                               agent=AgentRole.terminal.value)
                results.append({"command": cmd, "rejected": True, "stderr": str(exc), "success": False})
                all_success = False

        ctx.scratch["terminal"] = {"results": results}
        return AgentResult(
            success=all_success,
            agent=self.role,
            summary=f"Ran {len(results)} command(s); "
                    + ("all succeeded." if all_success else "one or more failed."),
            data={"commands": results},
            messages=[self.reply(AgentRole.testing, "Terminal work done; verify with tests.",
                                 commands=results)],
            next_agent=AgentRole.testing,
        )

    # ── Command selection (real, content-driven) ─────────────────────────────
    def _choose_commands(self, ctx: AgentContext, message: AgentMessage) -> List[str]:
        """Pick real, useful commands based on workspace contents.

        We do NOT run the full test suite here (that's the Testing agent's job)
        to keep responsibilities distinct. The terminal agent runs setup/build
        commands that the task implies (e.g. compile, install deps).
        """
        try:
            files = set(ctx.workspace.list_files())
        except Exception:
            return []

        cmds: List[str] = []
        # A quick syntax compile of changed Python files is a safe, useful step.
        if ctx.edits:
            changed_py = [e["path"] for e in ctx.edits if e["path"].endswith(".py")]
            if changed_py:
                cmds.append("python3 -m py_compile " + " ".join(changed_py[:10]))
        return cmds


def _rt_for(ctx: AgentContext):
    """Return a runtime-like object so run_command can store the live proc.

    The agent module's TaskRuntime has current_proc + current_proc_lock which
    allow cancellation to kill the running process. We expose a minimal shim
    that mirrors that interface if the real runtime is available, else None.
    """
    try:
        import threading

        from agent import get_runtime

        rt = get_runtime(ctx.task_id)
        if rt is not None:
            return rt
    except Exception:
        pass
    return None
