"""Git Agent.

Handles git status, diff, branch management, commit, push, and pull-request
preparation.

Every operation goes through the existing ``Workspace`` git methods, which run
git as a subprocess constrained to the workspace root — no shell injection, no
escape. The agent never fakes git activity: if there's nothing to commit it
says so honestly.
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

log = logging.getLogger("pk_ninja.agents.git")


@register_agent(AgentRole.git)
class GitAgent(BaseAgent):
    role = AgentRole.git
    name = "GitAgent"

    def handle(self, ctx: AgentContext, message: AgentMessage) -> AgentResult:
        ws = ctx.workspace

        if not _has_git(ws):
            ctx.emit_event("info", "Git agent: workspace is not a git repo; skipping git steps.",
                           agent=AgentRole.git.value)
            return AgentResult(
                success=True,
                agent=self.role,
                summary="No git repo; skipped.",
                data={"git": False},
            )

        # 1) Status + diff
        status = _safe(ws.git_status)
        changed = _safe(ws.git_changed_files, default=[])
        diff = _safe(lambda: ws.git_diff(staged=False), default="")
        ctx.emit_event("info", f"Git agent: status — {len(changed)} changed file(s).",
                       changed=changed, agent=AgentRole.git.value)

        if not changed:
            ctx.emit_event("info", "Git agent: no changes to commit.", agent=AgentRole.git.value)
            return AgentResult(
                success=True,
                agent=self.role,
                summary="No changes to commit.",
                data={"status": status, "changed": [], "diff": ""},
            )

        # 2) Branch
        branch = f"pk-ninja/{ctx.task_id[:8]}"
        try:
            res = ws.create_branch(branch)
            if not res.success:
                ws.git_checkout(branch)
            ctx.emit_event("info", f"Git agent: on branch {branch}", branch=branch,
                           agent=AgentRole.git.value)
        except Exception as exc:
            ctx.emit_event("error", f"Git agent: branch creation failed: {exc}",
                           agent=AgentRole.git.value)
            return AgentResult(
                success=False, agent=self.role,
                summary=f"Branch creation failed: {exc}",
                data={"error": str(exc)},
            )

        # 3) Stage + commit
        ws.git_add_all()
        commit_msg = f"PK Ninja Agent (multi-agent): {ctx.description[:100]}"
        res = ws.git_commit(commit_msg)
        if not res.success:
            ctx.emit_event("error", f"Git agent: commit failed: {res.stderr.strip()[:300]}",
                           agent=AgentRole.git.value)
            return AgentResult(
                success=False, agent=self.role,
                summary=f"Commit failed: {res.stderr.strip()[:200]}",
                data={"error": res.stderr},
            )
        ctx.emit_event("info", f"Git agent: committed — {commit_msg}", commit=commit_msg,
                       files=changed, agent=AgentRole.git.value)

        # 4) Push (only if credentials are configured)
        pushed = False
        push_error = ""
        if _has_credentials(ctx):
            res = ws.git_push()
            pushed = res.success
            if res.success:
                ctx.emit_event("info", f"Git agent: pushed branch {branch}.", branch=branch,
                               agent=AgentRole.git.value)
            else:
                push_error = res.stderr.strip()[:300]
                ctx.emit_event("error", f"Git agent: push failed: {push_error}",
                               agent=AgentRole.git.value)
        else:
            ctx.emit_event("info", "Git agent: push skipped (no GitHub credentials configured).",
                           agent=AgentRole.git.value)

        # 5) PR preparation (draft body, not auto-opened to stay safe)
        pr_body = self._pr_body(ctx, changed, branch)

        # Record the branch in scratch so the integration layer can surface it
        # to the UI (e.g. for the Push button / completion event).
        ctx.scratch["git"] = {"branch": branch, "pushed": pushed, "changed": changed}

        return AgentResult(
            success=True,
            agent=self.role,
            summary=f"Committed {len(changed)} file(s) on {branch}"
                    + (" and pushed." if pushed else " (not pushed)."),
            data={
                "branch": branch,
                "changed": changed,
                "commit": commit_msg,
                "pushed": pushed,
                "push_error": push_error,
                "pr_title": f"PK Ninja Agent: {ctx.description[:80]}",
                "pr_body": pr_body,
                "status": status,
                "diff": diff,
            },
            messages=[self.reply(AgentRole.coordinator, "Git work complete.",
                                 branch=branch, pushed=pushed)],
            next_agent=None,
        )

    def _pr_body(self, ctx: AgentContext, changed: List[str], branch: str) -> str:
        review = ctx.review or {}
        review_summary = review.get("summary", "no review notes")
        edits = ctx.edits or []
        lines = [
            f"## Summary\n{ctx.description}",
            "",
            "## Multi-Agent Orchestration",
            "This change was produced by the PK-Ninja multi-agent architecture "
            "(Planner → Repository → Coding → Terminal → Testing → Review → Git).",
            "",
            "## Files Changed",
            *[f"- `{f}`" for f in changed],
            "",
            "## Review Notes",
            review_summary,
            "",
            "## Branch",
            f"`{branch}`",
        ]
        return "\n".join(lines)


# ── helpers ───────────────────────────────────────────────────────────────────
def _has_git(ws: Any) -> bool:
    try:
        return bool(ws.has_git_repo())
    except Exception:
        return False


def _has_credentials(ctx: AgentContext) -> bool:
    try:
        return bool(ctx.settings.github_token and ctx.settings.github_repo_full())
    except Exception:
        return False


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default
