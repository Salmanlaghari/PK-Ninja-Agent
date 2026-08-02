"""Repository Context Engine: hybrid candidate filtering and AI-driven file selection."""
from __future__ import annotations

import json
import logging
import re
from typing import List

import aiosqlite
from ai_provider import AIProvider, ChatMessage
from workspace import Workspace

log = logging.getLogger("pk_ninja.context_engine")

STOP_WORDS = {
    "the", "and", "for", "with", "that", "this", "from", "your", "have", "with",
    "add", "create", "update", "delete", "remove", "make", "write", "read",
    "edit", "find", "search", "change", "task", "file", "repo", "repository", "code"
}


def extract_keywords(text: str) -> List[str]:
    """Extract Alphanumeric search tokens from user task text, filtering stop words."""
    words = re.findall(r"\b[a-zA-Z0-9_]{3,}\b", text.lower())
    return list(set(w for w in words if w not in STOP_WORDS))


async def find_candidate_files(task_id: str, task_desc: str, db_path: str, ws: Workspace) -> List[str]:
    """Query SQLite index for file paths or symbol names matching keywords in task description."""
    keywords = extract_keywords(task_desc)
    candidates = set()

    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row

        # Match paths containing keywords
        for kw in keywords:
            async with conn.execute(
                "SELECT path FROM repo_files WHERE task_id=? AND path LIKE ?",
                (task_id, f"%{kw}%")
            ) as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    candidates.add(r["path"])

        # Match symbol names containing keywords
        for kw in keywords:
            async with conn.execute(
                "SELECT path FROM repo_symbols WHERE task_id=? AND symbol_name LIKE ?",
                (task_id, f"%{kw}%")
            ) as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    candidates.add(r["path"])

    # Fallback to standard interesting files if candidates list is too small
    try:
        all_files = ws.list_files()
    except Exception:
        all_files = []

    if len(candidates) < 3 and all_files:
        interesting_extensions = (".py", ".js", ".ts", ".md", ".txt", ".json", ".yml", ".yaml", ".html", ".css", ".go", ".rs", ".java")
        interesting = [f for f in all_files if f.endswith(interesting_extensions)]
        for f in interesting[:15]:
            candidates.add(f)
        for f in all_files[:10]:
            candidates.add(f)

    # Clean candidates - make sure they actually exist
    valid_candidates = []
    for c in sorted(candidates):
        try:
            if ws.safe_path(c).exists():
                valid_candidates.append(c)
        except Exception:
            pass

    return valid_candidates[:30]  # Cap candidates to keep prompt size small


async def ai_select_relevant_files(task_desc: str, candidates: List[str], provider: AIProvider) -> List[str]:
    """Use the AI provider to select the most relevant files from candidate paths."""
    if not candidates:
        return []
    if len(candidates) <= 3:
        return candidates

    prompt_messages = [
        ChatMessage(
            role="system",
            content=(
                "You are an expert repository context filter. Given a user's task description "
                "and a list of candidate file paths, choose ONLY the most relevant files "
                "that are absolutely necessary to understand, edit, or verify to complete the task. "
                "Avoid selecting unrelated files. "
                "Return the selected files as a JSON array of strings. Do not explain, return ONLY valid JSON."
            )
        ),
        ChatMessage(
            role="user",
            content=f"Task:\n{task_desc}\n\nCandidate Files:\n{json.dumps(candidates)}\n\nSelected Files JSON List:"
        )
    ]

    try:
        if hasattr(provider, "generate"):
            text = provider.generate(prompt_messages)
        else:
            res = provider.stream_chat(prompt_messages)
            text = res.text

        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            selected = json.loads(m.group(0))
            if isinstance(selected, list):
                valid_selected = [f for f in selected if f in candidates]
                if valid_selected:
                    return valid_selected
    except Exception as e:
        log.warning(f"AI file selection failed: {e}. Falling back to top 3 candidates.")

    return candidates[:3]
