"""Review Agent.

Reviews the generated code, detects potential issues, and suggests improvements
before commit.

It inspects the actual edited file contents (never fakes a review) and looks
for concrete, deterministic red flags (syntax issues via ``py_compile``, TODO
markers left behind, debug prints, etc.). With a real provider it additionally
asks the model for a short review pass.

If it finds actionable issues it returns ``next_agent=coding`` so the
coordinator routes back for a fix (up to ``MAX_FIX_ROUNDS``). Otherwise it
approves and routes to the Git agent.
"""
from __future__ import annotations

import logging
import py_compile
from typing import Any, Dict, List

from agents.base import (
    AgentContext,
    AgentMessage,
    AgentResult,
    AgentRole,
    BaseAgent,
)
from agents.registry import register_agent

log = logging.getLogger("pk_ninja.agents.review")


@register_agent(AgentRole.review)
class ReviewAgent(BaseAgent):
    role = AgentRole.review
    name = "ReviewAgent"

    # Simple, deterministic red-flag patterns.
    _RED_FLAGS = [
        ("debug print", "print("),
        ("debug breakpoint", "breakpoint()"),
        ("debug pdb", "import pdb"),
        ("leftover TODO", "TODO(agent)"),
        ("leftover FIXME", "FIXME"),
        ("leftover XXX", "XXX"),
    ]

    def handle(self, ctx: AgentContext, message: AgentMessage) -> AgentResult:
        ws = ctx.workspace
        edits = ctx.edits or []
        files_to_review = [e["path"] for e in edits] or [f["path"] for f in (ctx.relevant_files or [])][:5]

        ctx.emit_event("info", f"Review agent: reviewing {len(files_to_review)} file(s).",
                       files=files_to_review, agent=AgentRole.review.value)

        issues: List[Dict[str, Any]] = []

        # 1) Syntax check every edited Python file (real compile).
        for path in files_to_review:
            if ctx.is_cancelled():
                break
            if path.endswith(".py"):
                issue = self._syntax_check(ws, path)
                if issue:
                    issues.append(issue)

        # 2) Red-flag scan on file contents.
        for path in files_to_review:
            if ctx.is_cancelled():
                break
            try:
                content = ws.read_file(path)
            except Exception:
                continue
            for label, needle in self._RED_FLAGS:
                if needle in content:
                    issues.append({"file": path, "severity": "warning",
                                   "message": f"Possible {label} found in {path}."})

        # 3) Optional AI review pass.
        ai_notes = ""
        provider = ctx.provider
        if provider is not None and not _is_local(provider):
            ai_notes = self._ai_review(ctx, provider, files_to_review, issues)
            if ai_notes:
                ctx.emit_event("thinking", ai_notes, source="review", streaming=True,
                               agent=AgentRole.review.value)

        # Classify issues.
        errors = [i for i in issues if i.get("severity") == "error"]
        warnings = [i for i in issues if i.get("severity") == "warning"]

        review = {
            "files_reviewed": files_to_review,
            "errors": errors,
            "warnings": warnings,
            "ai_notes": ai_notes,
            "summary": self._summary(errors, warnings),
        }
        ctx.review = review

        ctx.emit_event("info", f"Review agent: {review['summary']}",
                       errors=len(errors), warnings=len(warnings), agent=AgentRole.review.value)

        if errors:
            # Route back to coding to fix the blocking errors.
            fix_files = list({i["file"] for i in errors if i.get("file")})
            return AgentResult(
                success=False,
                agent=self.role,
                summary=f"Review found {len(errors)} blocking issue(s).",
                data={**review, "fix_files": fix_files},
                messages=[
                    self.reply(AgentRole.coding, f"Fix these review issues: {review['summary']}",
                               fix_files=fix_files, priority="high")
                ],
                next_agent=AgentRole.coding,
            )

        return AgentResult(
            success=True,
            agent=self.role,
            summary=review["summary"],
            data=review,
            messages=[self.reply(AgentRole.git, "Review passed; proceed to git.")],
            next_agent=AgentRole.git,
        )

    # ── Checks ───────────────────────────────────────────────────────────────
    def _syntax_check(self, ws: Any, path: str) -> Dict[str, Any] | None:
        try:
            real = ws.safe_path(path)
            py_compile.compile(str(real), doraise=True)
            return None
        except py_compile.PyCompileError as exc:
            return {"file": path, "severity": "error",
                    "message": f"Syntax error in {path}: {exc}"}
        except Exception as exc:
            return {"file": path, "severity": "error",
                    "message": f"Could not compile {path}: {exc}"}

    def _ai_review(self, ctx: AgentContext, provider: Any,
                   files: List[str], issues: List[Dict[str, Any]]) -> str:
        try:
            from ai_provider import ChatMessage

            known_issues = "\n".join(f"- {i['message']}" for i in issues) or "none"
            messages = [
                ChatMessage("system", "You are a code reviewer. In 2-3 short sentences, note any "
                                       "risks or improvements for the changed files. Be concise."),
                ChatMessage("user",
                            f"Task:\n{ctx.description}\n\nFiles:\n{', '.join(files)}\n\n"
                            f"Known issues found:\n{known_issues}"),
            ]
            if hasattr(provider, "generate"):
                return provider.generate(messages).strip()
            elif hasattr(provider, "stream_chat"):
                return provider.stream_chat(messages).text.strip()
        except Exception as exc:
            log.warning("review AI pass failed: %s", exc)
        return ""

    def _summary(self, errors: List[Dict[str, Any]], warnings: List[Dict[str, Any]]) -> str:
        if errors:
            return f"{len(errors)} error(s), {len(warnings)} warning(s) — needs fixes."
        if warnings:
            return f"{len(warnings)} warning(s) — approved with notes."
        return "No issues found — approved."


# ── helpers ───────────────────────────────────────────────────────────────────
def _is_local(provider: Any) -> bool:
    try:
        from ai_provider import LocalProvider

        return isinstance(provider, LocalProvider)
    except Exception:
        return False
