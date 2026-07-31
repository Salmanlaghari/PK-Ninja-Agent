"""Coding Agent.

Modifies files, generates code, refactors code, and applies edits safely.

It ALWAYS goes through ``Workspace`` for every file operation so the existing
path-traversal protections (``safe_path``) and write restrictions stay intact.
It never shells out directly to write files.

Provider-independent: with a real provider it asks the model for edits; with
the local/no provider it applies deterministic, task-keyword-driven edits
(matching the behaviour of the existing ``LocalProvider.edit`` so tests stay
stable).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List

from agents.base import (
    AgentContext,
    AgentMessage,
    AgentResult,
    AgentRole,
    BaseAgent,
)
from agents.registry import register_agent

log = logging.getLogger("pk_ninja.agents.coding")


@register_agent(AgentRole.coding)
class CodingAgent(BaseAgent):
    role = AgentRole.coding
    name = "CodingAgent"

    def handle(self, ctx: AgentContext, message: AgentMessage) -> AgentResult:
        ws = ctx.workspace
        files = ctx.relevant_files or []
        plan = ctx.plan or []

        # If a previous agent (testing/review) flagged specific files to fix,
        # prefer those targets.
        fix_targets = (message.payload.get("previous", {}) or {}).get("data", {}).get("fix_files", [])
        if fix_targets:
            files = [f for f in files if f["path"] in fix_targets] or files

        ctx.emit_event(
            "editing",
            f"Coding agent: applying edits across {len(files)} file(s).",
            agent=AgentRole.coding.value,
        )

        edits = self._produce_edits(ctx, files, plan, message)
        applied = self._apply_edits(ctx, ws, edits)

        ctx.edits = applied

        return AgentResult(
            success=True,
            agent=self.role,
            summary=f"Applied {len(applied)} edit(s).",
            data={"edits": [e["path"] for e in applied]},
            messages=[
                self.reply(
                    AgentRole.terminal,
                    "Edits applied; run any necessary commands.",
                    edits=[e["path"] for e in applied],
                )
            ],
            next_agent=AgentRole.terminal,
        )

    # ── Edit production ──────────────────────────────────────────────────────
    def _produce_edits(self, ctx: AgentContext, files: List[Dict[str, Any]],
                       plan: List[Dict[str, Any]], message: AgentMessage) -> List[Dict[str, Any]]:
        provider = ctx.provider
        if provider is not None and not _is_local(provider):
            try:
                edits = self._edits_with_provider(ctx, provider, files, plan, message)
                if edits:
                    return edits
            except Exception as exc:
                ctx.emit_event("error", f"Coding provider error: {exc}; using deterministic edits.",
                               agent=AgentRole.coding.value)
                log.warning("coding provider failed: %s", exc)
        return self._deterministic_edits(ctx, files, plan, message)

    def _edits_with_provider(self, ctx: AgentContext, provider: Any,
                             files: List[Dict[str, Any]], plan: List[Dict[str, Any]],
                             message: AgentMessage) -> List[Dict[str, Any]]:
        from ai_provider import ChatMessage, Plan, _parse_edits_json

        files_brief = "\n".join(f["path"] for f in files[:30])
        plan_text = "\n".join(f"{s['id']}. {s['description']}" for s in plan) or "(no plan)"
        # If this is a fix round, include the failure context.
        prev = message.payload.get("previous", {})
        fix_hint = ""
        if prev and prev.get("data"):
            fix_hint = f"\n\nPrevious failure to fix:\n{json.dumps(prev['data'], default=str)[:1500]}"

        messages = [
            ChatMessage(
                "system",
                "You are a coding agent. Given a task, plan, and file paths, "
                "return a JSON array of edits. Each edit: "
                "{\"path\": \"...\", \"content\": \"full new file content\"}. "
                "Only include files you actually change. Return ONLY a JSON array.",
            ),
            ChatMessage(
                "user",
                f"Task:\n{ctx.description}\n\nPlan:\n{plan_text}\n\nFiles:\n{files_brief}{fix_hint}",
            ),
        ]
        text = ""
        if hasattr(provider, "generate"):
            text = provider.generate(messages)
        elif hasattr(provider, "stream_chat"):
            text = provider.stream_chat(messages).text
        return _parse_edits_json(text)

    def _deterministic_edits(self, ctx: AgentContext, files: List[Dict[str, Any]],
                             plan: List[Dict[str, Any]], message: AgentMessage) -> List[Dict[str, Any]]:
        """Deterministic, keyword-driven edits — mirrors LocalProvider.edit behaviour."""
        desc = (ctx.description or "").lower()
        edits: List[Dict[str, Any]] = []

        # Documentation tasks: add/update README.md
        if any(k in desc for k in ("readme", "document", "doc ")):
            edits.append({
                "path": "README.md",
                "content": self._readme_content(ctx),
            })

        # Docstring tasks: prepend a module docstring to Python files lacking one
        if any(k in desc for k in ("docstring", "doc string", "comment")):
            for f in files:
                if f["path"].endswith(".py") and not f["content"].lstrip().startswith('"""'):
                    new_content = self._add_module_docstring(f["content"], f["path"])
                    if new_content != f["content"]:
                        edits.append({"path": f["path"], "content": new_content})

        # If a fix was requested and we have failure context, surface a TODO marker
        prev = (message.payload.get("previous") or {}).get("data") or {}
        if prev.get("error") and files:
            target = files[0]
            marker = f"\n# TODO(agent): address failure — {str(prev.get('error'))[:120]}\n"
            if marker not in target["content"]:
                edits.append({"path": target["path"], "content": target["content"] + marker})

        # Generic 'add feature'/'implement' with no specific edit: ensure a
        # NOTES file exists so there is always a real change to review/commit.
        if not edits and any(k in desc for k in ("add", "implement", "create", "build", "feature")):
            edits.append({
                "path": "AGENT_NOTES.md",
                "content": f"# Agent Notes\n\nTask: {ctx.description}\n\n"
                           f"Plan steps: {len(plan)}\nFiles considered: "
                           f"{', '.join(f['path'] for f in files[:8])}\n",
            })

        return edits

    # ── Apply edits safely through Workspace ─────────────────────────────────
    def _apply_edits(self, ctx: AgentContext, ws: Any, edits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        from workspace import WorkspaceError

        applied: List[Dict[str, Any]] = []
        for e in edits:
            if ctx.is_cancelled():
                break
            path = e.get("path")
            content = e.get("content", "")
            if not path:
                continue
            try:
                target = ws.safe_path(path)
                if target.exists():
                    ws.write_file(path, content)
                    action = "write"
                else:
                    ws.create_file(path, content)
                    action = "create"
                applied.append({"path": path, "action": action, "bytes": len(content)})
                ctx.emit_event("editing", f"Coding agent edited {path}", path=path, action=action,
                               agent=AgentRole.coding.value)
            except WorkspaceError as exc:
                ctx.emit_event("error", f"Safe edit rejected for {path}: {exc}", path=path,
                               agent=AgentRole.coding.value)
        return applied

    # ── Content helpers ──────────────────────────────────────────────────────
    def _readme_content(self, ctx: AgentContext) -> str:
        try:
            files = ctx.workspace.list_files()
        except Exception:
            files = []
        py_files = [f for f in files if f.endswith(".py") and "/" not in f][:10]
        return (
            f"# Project\n\nThis project was documented by the PK-Ninja coding agent.\n\n"
            f"## Task\n{ctx.description}\n\n## Top-level Python files\n"
            + "\n".join(f"- `{f}`" for f in py_files)
            + "\n"
        )

    def _add_module_docstring(self, content: str, path: str) -> str:
        doc = f'"""Module: {path} — documented by the PK-Ninja coding agent."""\n'
        # Preserve any leading shebang/__future__ by inserting after them.
        lines = content.splitlines(keepends=True)
        insert_at = 0
        for i, line in enumerate(lines[:4]):
            if line.startswith("#!") or "__future__" in line:
                insert_at = i + 1
            else:
                break
        lines.insert(insert_at, doc)
        return "".join(lines)


# ── helpers ───────────────────────────────────────────────────────────────────
def _is_local(provider: Any) -> bool:
    try:
        from ai_provider import LocalProvider

        return isinstance(provider, LocalProvider)
    except Exception:
        return False
