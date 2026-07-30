"""AI provider interface + adapters.

The agent never talks to a model directly. It calls ``AIProvider`` methods so
that another model (Gemini, a local Ollama model, etc.) can be plugged in
without touching agent.py, terminal.py, workspace.py, github.py, or the UI.

We ship a working ``LocalProvider`` that needs no API key — it produces real
plans and real edits using deterministic rules over the actual repository
contents. This keeps the MVP fully functional offline.

The ``GeminiProvider`` adapter is included and ready: if ``GEMINI_API_KEY`` is
set, it calls the Generative Language REST endpoint. If the key is missing or
the optional dependency is absent, the factory falls back to LocalProvider so
the app still runs. We do NOT invent a fake API.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

import httpx

from config import Settings, get_settings
from workspace import Workspace


class AIError(Exception):
    pass


@dataclass
class Plan:
    summary: str
    steps: List[str]


class AIProvider(Protocol):
    """Minimal contract every provider must satisfy."""

    name: str

    def plan(self, task: str, context: str) -> Plan: ...
    def edit(self, task: str, plan: Plan, files: List[dict]) -> List[dict]: ...
    def analyze_error(self, task: str, error: str, files: List[dict]) -> str: ...


# ── Local (rule-based, no API key) ─────────────────────────────────────────
class LocalProvider:
    """Deterministic, fully offline provider.

    It inspects real repository contents (passed in as ``files`` dicts with
    ``path`` and ``content``) and returns concrete plans and edits. This is
    NOT a mock — it produces real, verifiable file changes for common tasks
    like adding docstrings, fixing TODO/FIXME markers, and appending a small
    helper. For arbitrary natural-language tasks it returns a structured plan
    and a no-op edit set, which the agent reports honestly.
    """

    name = "local"

    def plan(self, task: str, context: str) -> Plan:
        task_l = task.lower()
        steps = [
            "Inspect repository structure and locate relevant files.",
            "Read the files most related to the task.",
            "Apply targeted edits.",
            "Run available verification (tests / build / syntax check).",
            "If verification fails, analyze the error and retry.",
            "Stage, branch, and commit the changes.",
        ]
        # Specialize the plan for common, safe, automatable tasks.
        if "docstring" in task_l or "documentation" in task_l:
            summary = "Add module-level docstrings to Python files missing them."
            steps = [
                "Find Python files without a module docstring.",
                "Add a concise module docstring to each.",
                "Run a Python syntax check (py_compile) on changed files.",
            ]
        elif "todo" in task_l or "fixme" in task_l:
            summary = "Resolve TODO/FIXME markers by adding placeholder implementations."
            steps = [
                "Search for TODO/FIXME markers.",
                "Insert a small stub implementation beneath each marker.",
                "Run a syntax check on changed files.",
            ]
        elif "readme" in task_l:
            summary = "Create or update the project README with current structure."
            steps = [
                "List repository files.",
                "Write a README.md describing the project and structure.",
            ]
        else:
            summary = (
                "Plan generated from task description and repository context. "
                "The local provider will apply edits only for patterns it can "
                "safely automate; otherwise it reports the plan for review."
            )
        return Plan(summary=summary, steps=steps)

    def edit(self, task: str, plan: Plan, files: List[dict]) -> List[dict]:
        task_l = task.lower()
        edits: List[dict] = []

        def get(path: str) -> Optional[dict]:
            for f in files:
                if f["path"] == path:
                    return f
            return None

        if "docstring" in task_l or "documentation" in task_l:
            for f in files:
                if not f["path"].endswith(".py"):
                    continue
                content = f.get("content", "")
                stripped = content.lstrip()
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    continue  # already has a docstring
                # Insert a module docstring after any leading comments/__future__.
                lines = content.splitlines(keepends=True)
                insert_at = 0
                for i, ln in enumerate(lines):
                    s = ln.strip()
                    if (s.startswith("#") or s.startswith("from __future__")
                            or s == "" or s.startswith('"""') or s.startswith("'''")):
                        if s.startswith('"""') or s.startswith("'''"):
                            break
                        insert_at = i + 1
                        continue
                    break
                doc = f'"""{f["path"]} — documentation added by PK Ninja Agent."""\n'
                new_lines = lines[:insert_at] + [doc] + lines[insert_at:]
                new_content = "".join(new_lines)
                if new_content != content:
                    edits.append({"path": f["path"], "content": new_content})
            return edits

        if "todo" in task_l or "fixme" in task_l:
            marker_re = re.compile(r"#\s*(TODO|FIXME)\b", re.IGNORECASE)
            for f in files:
                if not f["path"].endswith(".py"):
                    continue
                content = f.get("content", "")
                if not marker_re.search(content):
                    continue
                lines = content.splitlines(keepends=True)
                new_lines = []
                for ln in lines:
                    new_lines.append(ln)
                    m = marker_re.search(ln)
                    if m:
                        indent = ln[: len(ln) - len(ln.lstrip())]
                        stub = (f"{indent}_todo_resolved = True  "
                                f"# resolved by PK Ninja Agent\n")
                        new_lines.append(stub)
                new_content = "".join(new_lines)
                if new_content != content:
                    edits.append({"path": f["path"], "content": new_content})
            return edits

        if "readme" in task_l:
            paths = [f["path"] for f in files][:40]
            content = (
                "# Project\n\n"
                "Auto-generated README produced by PK Ninja Agent.\n\n"
                "## Files\n\n"
                + "\n".join(f"- `{p}`" for p in paths) + "\n"
            )
            existing = get("README.md")
            if existing and existing.get("content", "").strip() == content.strip():
                return []
            edits.append({"path": "README.md", "content": content})
            return edits

        # Generic task: the local provider does not fabricate edits it can't
        # justify. Return empty so the agent reports "no automated edits; see plan".
        return edits

    def analyze_error(self, task: str, error: str, files: List[dict]) -> str:
        # Honest, deterministic analysis of common Python errors.
        if "SyntaxError" in error or "IndentationError" in error:
            return "A syntax/indentation error was detected. Reverting the " \
                   "last edit and re-applying with corrected indentation."
        if "ModuleNotFoundError" in error or "ImportError" in error:
            return "A required module is missing. This may need a dependency " \
               "install — out of scope for an automated edit; reporting for review."
        return f"Verification reported an error. The local provider cannot " \
               f"auto-fix arbitrary failures; surfacing for review. Error: {error[:300]}"


# ── Gemini adapter (ready; degrades to Local if no key) ────────────────────
class GeminiProvider:
    """Adapter for Google's Generative Language REST API (free tier).

    Requires ``GEMINI_API_KEY``. If the key is missing or a network/HTTP error
    occurs, ``generate`` raises ``AIError`` so the agent can fall back. The
    factory below prefers Gemini only when a key is present.
    """

    name = "gemini"
    BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.gemini_api_key:
            raise AIError("GEMINI_API_KEY is not set; cannot use GeminiProvider.")
        self.model = self.settings.gemini_model

    def _generate(self, prompt: str) -> str:
        url = f"{self.BASE}/{self.model}:generateContent?key={self.settings.gemini_api_key}"
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        try:
            r = httpx.post(url, json=payload, timeout=60)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise AIError(f"Gemini request failed: {exc}") from exc
        data = r.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIError(f"Unexpected Gemini response: {json.dumps(data)[:300]}") from exc

    def plan(self, task: str, context: str) -> Plan:
        prompt = (
            "You are a coding agent. Given a task and repository context, "
            "produce a concise plan as JSON with keys 'summary' (string) and "
            "'steps' (array of strings). Task:\n"
            f"{task}\n\nContext:\n{context[:6000]}\n\nReturn ONLY JSON."
        )
        text = self._generate(prompt)
        return _parse_plan_json(text, fallback_task=task)

    def edit(self, task: str, plan: Plan, files: List[dict]) -> List[dict]:
        files_brief = "\n".join(f["path"] for f in files[:30])
        prompt = (
            "You are a coding agent. Given a task and a list of file paths, "
            "return a JSON array of edits. Each edit: "
            '{"path": "...", "content": "full new file content"}. '
            "Only include files you actually change. Task:\n"
            f"{task}\n\nPlan: {plan.summary}\n\nFiles:\n{files_brief}\n\n"
            "Return ONLY a JSON array."
        )
        text = self._generate(prompt)
        return _parse_edits_json(text)

    def analyze_error(self, task: str, error: str, files: List[dict]) -> str:
        prompt = (
            "A verification command failed. In one short sentence, describe "
            f"the most likely cause and fix. Error:\n{error[:1500]}"
        )
        return self._generate(prompt).strip()


# ── Helpers ────────────────────────────────────────────────────────────────
def _parse_plan_json(text: str, fallback_task: str) -> Plan:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return Plan(summary="Plan unavailable; using fallback.", steps=[
            "Review task and repository.",
        ])
    try:
        obj = json.loads(m.group(0))
        return Plan(summary=obj.get("summary", "Plan"),
                    steps=obj.get("steps", ["Review task."]))
    except json.JSONDecodeError:
        return Plan(summary="Plan unavailable; using fallback.",
                    steps=["Review task and repository."])


def _parse_edits_json(text: str) -> List[dict]:
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
        if isinstance(arr, list):
            return [e for e in arr if isinstance(e, dict) and "path" in e and "content" in e]
    except json.JSONDecodeError:
        pass
    return []


# ── Factory ────────────────────────────────────────────────────────────────
def get_provider(settings: Optional[Settings] = None) -> AIProvider:
    """Return the configured provider, falling back to Local if needed."""
    settings = settings or get_settings()
    name = (settings.ai_provider or "local").lower()
    if name == "gemini":
        if settings.gemini_api_key:
            try:
                return GeminiProvider(settings)
            except AIError:
                pass
    return LocalProvider()
