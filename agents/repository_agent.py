"""Repository Agent.

Searches the repository, builds project context, and finds relevant files and
symbols. It reuses the existing ``context_engine`` (hybrid keyword matching +
optional LLM selection) and ``indexing`` (AST-based symbol index) modules so
no repository logic is duplicated and all path-safety stays intact.
"""
from __future__ import annotations

import asyncio
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

log = logging.getLogger("pk_ninja.agents.repository")


@register_agent(AgentRole.repository)
class RepositoryAgent(BaseAgent):
    role = AgentRole.repository
    name = "RepositoryAgent"

    def handle(self, ctx: AgentContext, message: AgentMessage) -> AgentResult:
        ws = ctx.workspace
        ctx.emit_event("searching", "Repository agent: searching the repository and building context.")

        # 1) Ensure the workspace is indexed (reuses existing indexing module).
        index_stats = self._index(ctx, ws)

        # 2) Find candidate files via the context engine (keyword matching).
        candidates = self._find_candidates(ctx, ws)

        # 3) Optionally narrow candidates with the AI provider.
        relevant = self._select_relevant(ctx, candidates)

        # 4) Read the relevant files (real reads, emit file_read events).
        file_objs: List[Dict[str, Any]] = []
        for f in relevant:
            if ctx.is_cancelled():
                break
            try:
                content = ws.read_file(f)
                file_objs.append({"path": f, "content": content})
                ctx.emit_event("file_read", f"Repository agent loaded {f}", path=f, bytes=len(content),
                               agent=AgentRole.repository.value)
            except Exception as exc:
                ctx.emit_event("error", f"Could not read {f}: {exc}", agent=AgentRole.repository.value)

        # 5) Build a project map string for downstream agents.
        project_map = self._project_map(ctx, ws)

        ctx.relevant_files = file_objs
        ctx.scratch["repository"] = {
            "index_stats": index_stats,
            "candidates": candidates,
            "relevant": relevant,
            "project_map": project_map,
        }

        return AgentResult(
            success=True,
            agent=self.role,
            summary=f"Built context from {len(file_objs)} files "
                    f"({len(candidates)} candidates, {len(relevant)} selected).",
            data={
                "files": [f["path"] for f in file_objs],
                "project_map": project_map,
                "index_stats": index_stats,
            },
            messages=[
                self.reply(
                    AgentRole.coding,
                    f"Repository context ready: {len(file_objs)} files.",
                    files=[f["path"] for f in file_objs],
                    project_map=project_map,
                )
            ],
            next_agent=AgentRole.coding,
        )

    # ── Indexing (reuses indexing.index_workspace) ───────────────────────────
    def _index(self, ctx: AgentContext, ws: Any) -> Dict[str, Any]:
        try:
            import aiosqlite

            from indexing import index_workspace

            async def _run():
                async with aiosqlite.connect(ctx.settings.database_path) as conn:
                    await conn.executescript(
                        "CREATE TABLE IF NOT EXISTS repo_files (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "task_id TEXT NOT NULL, path TEXT NOT NULL, hash TEXT NOT NULL, mtime REAL NOT NULL, "
                        "indexed_at TEXT NOT NULL, UNIQUE(task_id, path));"
                        "CREATE TABLE IF NOT EXISTS repo_symbols (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "task_id TEXT NOT NULL, path TEXT NOT NULL, symbol_name TEXT NOT NULL, "
                        "symbol_type TEXT NOT NULL, line_no INTEGER NOT NULL);"
                    )
                    await conn.commit()
                    return await index_workspace(ctx.task_id, ws, conn)

            return _run_async(_run)
        except Exception as exc:
            ctx.emit_event("error", f"Repository indexing failed: {exc}", agent=AgentRole.repository.value)
            return {"error": str(exc)}

    # ── Candidate finding (reuses context_engine.find_candidate_files) ───────
    def _find_candidates(self, ctx: AgentContext, ws: Any) -> List[str]:
        try:
            from context_engine import find_candidate_files

            return _run_async(lambda: find_candidate_files(
                ctx.task_id, ctx.description, ctx.settings.database_path, ws))
        except Exception as exc:
            ctx.emit_event("error", f"Candidate search failed: {exc}", agent=AgentRole.repository.value)
            # Fallback: list workspace files directly (still real, just broader).
            try:
                return ws.list_files()[:20]
            except Exception:
                return []

    # ── AI selection (reuses context_engine.ai_select_relevant_files) ────────
    def _select_relevant(self, ctx: AgentContext, candidates: List[str]) -> List[str]:
        provider = ctx.provider
        if provider is None or _is_local(provider):
            return candidates[:8]
        try:
            from context_engine import ai_select_relevant_files

            return _run_async(lambda: ai_select_relevant_files(ctx.description, candidates, provider))
        except Exception as exc:
            ctx.emit_event("error", f"AI selection failed: {exc}; using top candidates.",
                           agent=AgentRole.repository.value)
            return candidates[:8]

    # ── Project map (reuses indexing.get_project_map) ────────────────────────
    def _project_map(self, ctx: AgentContext, ws: Any) -> str:
        try:
            import aiosqlite

            from indexing import get_project_map

            async def _run():
                async with aiosqlite.connect(ctx.settings.database_path) as conn:
                    return await get_project_map(ctx.task_id, ws, conn)

            return _run_async(_run)
        except Exception:
            return ""


# ── helpers ───────────────────────────────────────────────────────────────────
def _is_local(provider: Any) -> bool:
    try:
        from ai_provider import LocalProvider

        return isinstance(provider, LocalProvider)
    except Exception:
        return False


def _run_async(coro_factory):
    """Run a coroutine factory from a sync context, loop-safe.

    If an event loop is already running (e.g. inside the FastAPI thread pool
    bridge), create a fresh loop; otherwise use asyncio.run.
    """
    try:
        asyncio.get_running_loop()
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro_factory())
        finally:
            loop.close()
    except RuntimeError:
        return asyncio.run(coro_factory())
