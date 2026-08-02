"""Testing Agent.

Runs tests, analyzes failures, and requests fixes if necessary.

It reuses the existing verification-command picker logic (mirrored from
``agent.Agent._pick_verification_command``) and ``terminal.run_command`` so
the run is real and sandboxed. On failure it returns ``next_agent=coding`` so
the coordinator routes back to the Coding agent for a fix (up to the
coordinator's ``MAX_FIX_ROUNDS``).
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
    get_runtime_for_ctx,
)
from agents.registry import register_agent

log = logging.getLogger("pk_ninja.agents.testing")


@register_agent(AgentRole.testing)
class TestingAgent(BaseAgent):
    # Prevent pytest from collecting this class as a test (name starts with
    # "Test"). This is the standard, non-invasive way to opt out.
    __test__ = False

    role = AgentRole.testing
    name = "TestingAgent"

    def handle(self, ctx: AgentContext, message: AgentMessage) -> AgentResult:
        ws = ctx.workspace
        cmd = self._pick_verification_command(ws)

        if not cmd:
            ctx.emit_event(
                "info",
                "Testing agent: no verification command detected; skipping run.",
                agent=AgentRole.testing.value,
            )
            ctx.test_report = {"skipped": True, "reason": "no verification command"}
            return AgentResult(
                success=True,
                agent=self.role,
                summary="No verification command; skipped.",
                data={"skipped": True},
                messages=[self.reply(AgentRole.review, "No tests to run; proceed to review.")],
                next_agent=AgentRole.review,
            )

        from terminal import TerminalError, run_command, validate_command

        ctx.emit_event("test_started", f"Testing agent running: {cmd}", command=cmd,
                       agent=AgentRole.testing.value)
        try:
            validate_command(cmd)  # noqa: F841 — validates before run
            result = run_command(cmd, ws, rt=get_runtime_for_ctx(ctx))
        except TerminalError as exc:
            ctx.emit_event("error", f"Testing command rejected: {exc}", command=cmd,
                           agent=AgentRole.testing.value)
            ctx.test_report = {"success": False, "rejected": True, "stderr": str(exc), "command": cmd}
            ctx.emit_event("test_finished", "verification rejected", success=False,
                           agent=AgentRole.testing.value)
            return AgentResult(
                success=False,
                agent=self.role,
                summary=f"Verification command rejected: {exc}",
                data={"rejected": True, "command": cmd, "error": str(exc)},
                messages=[self.reply(AgentRole.coordinator, "Command rejected; needs human review.",
                                     priority="urgent")],
            )

        success = result.success
        ctx.emit_event(
            "test_finished",
            f"Verification exit code: {result.returncode}",
            success=success, returncode=result.returncode,
            agent=AgentRole.testing.value,
        )

        report: Dict[str, Any] = {
            "command": cmd,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "success": success,
        }
        ctx.test_report = report

        if success:
            return AgentResult(
                success=True,
                agent=self.role,
                summary="Verification passed.",
                data=report,
                messages=[self.reply(AgentRole.review, "Tests passed; review the code.")],
                next_agent=AgentRole.review,
            )

        # Failure — analyze and request a fix from Coding.
        analysis = self._analyze_failure(ctx, report)
        report["analysis"] = analysis
        fix_files = self._extract_failing_files(report)
        ctx.emit_event("fixing", f"Testing agent: verification failed. Analysis: {analysis}",
                       analysis=analysis, agent=AgentRole.testing.value)
        return AgentResult(
            success=False,
            agent=self.role,
            summary=f"Verification failed (exit {result.returncode}).",
            data={**report, "fix_files": fix_files},
            messages=[
                self.reply(
                    AgentRole.coding,
                    f"Fix this failure: {analysis}",
                    error=result.stderr or result.stdout,
                    fix_files=fix_files,
                    priority="high",
                )
            ],
            next_agent=AgentRole.coding,
        )

    # ── Verification command picker (mirrors agent.Agent._pick_verification_command) ─
    def _pick_verification_command(self, ws: Any) -> Optional[str]:
        files = set(ws.list_files())
        if "pytest.ini" in files or any(
            f.startswith("tests/") and f.endswith(".py") for f in files
        ) or any(f.endswith("conftest.py") for f in files):
            return "python3 -m pytest -q"
        if "setup.py" in files or "pyproject.toml" in files:
            py = [f for f in files if f.endswith(".py") and "/" not in f]
            if py:
                return "python3 -m py_compile " + " ".join(py[:10])[:200]
        if "package.json" in files:
            return "npm test"
        if "Cargo.toml" in files:
            return "cargo build"
        if "go.mod" in files:
            return "go build ./..."
        py_files = [f for f in files if f.endswith(".py")]
        if py_files:
            return "python3 -m py_compile " + " ".join(py_files[:5])
        return None

    def _analyze_failure(self, ctx: AgentContext, report: Dict[str, Any]) -> str:
        """Produce a short analysis of the failure (provider-independent)."""
        provider = ctx.provider
        if provider is not None and not _is_local(provider):
            try:
                from ai_provider import ChatMessage

                messages = [
                    ChatMessage("system", "A verification command failed. In one short sentence, "
                                           "describe the most likely cause and fix."),
                    ChatMessage("user", f"Error:\n{(report.get('stderr') or report.get('stdout') or '')[:1500]}"),
                ]
                if hasattr(provider, "generate"):
                    return provider.generate(messages).strip()
                elif hasattr(provider, "stream_chat"):
                    return provider.stream_chat(messages).text.strip()
            except Exception as exc:
                log.warning("testing analysis provider failed: %s", exc)
        # Deterministic fallback analysis.
        err = (report.get("stderr") or report.get("stdout") or "").lower()
        if "no module named" in err:
            return "A required module is missing; check imports/dependencies."
        if "syntaxerror" in err:
            return "There is a syntax error in the edited code; fix the syntax."
        if "assert" in err or "assertion" in err:
            return "An assertion failed; review the changed logic."
        if "importerror" in err:
            return "An import failed; verify the import path."
        return "Verification failed; inspect the output and fix the offending code."

    def _extract_failing_files(self, report: Dict[str, Any]) -> List[str]:
        """Best-effort extraction of file names mentioned in the failure output."""
        import re

        text = (report.get("stderr") or "") + "\n" + (report.get("stdout") or "")
        found = re.findall(r"([\w/\\\-]+\.py)", text)
        # de-dup, keep order
        seen = set()
        out = []
        for f in found:
            if f not in seen:
                seen.add(f)
                out.append(f)
        return out[:10]


# ── helpers ───────────────────────────────────────────────────────────────────
def _is_local(provider: Any) -> bool:
    try:
        from ai_provider import LocalProvider

        return isinstance(provider, LocalProvider)
    except Exception:
        return False



