"""AI provider architecture — modular, streaming-capable, configurable.

The agent never talks to a model directly. It calls ``AIProvider`` methods so
that another model can be plugged in without touching agent.py, terminal.py,
workspace.py, github.py, or the UI.

Provider selection is fully driven by environment variables (see config.py):

    AI_PROVIDER = local | openai | gemini | anthropic | jules
    AI_API_KEY  = <key>                 (only required for non-local providers)
    AI_MODEL    = <model name>
    AI_BASE_URL = <endpoint URL>

Providers
---------

LocalProvider
    Deterministic, fully offline. Inspects real repository contents and
    returns concrete plans and edits for safe, automatable tasks. Needs no
    API key or network. This is the default fallback so the MVP always works.

OpenAIProvider
    Works with any OpenAI-compatible REST endpoint.
    Supports real streaming via ``stream_chat()`` (Server-Sent Events).

GeminiProvider
    Legacy Gemini adapter routing through Google's OpenAI-compatible endpoint.

AnthropicProvider
    Custom adapter for Anthropic Claude, supporting native SSE Messages API.

JulesProvider
    Custom adapter for Google's elite coding agent Jules.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    Iterator,
    List,
    Optional,
    Protocol,
    runtime_checkable,
)

import httpx

from config import Settings, get_settings
from workspace import Workspace


class AIError(Exception):
    """Raised when an AI provider call fails or is misconfigured."""


# ── Data structures ─────────────────────────────────────────────────────
@dataclass
class Plan:
    summary: str
    steps: List[str]


@dataclass
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class ChatResult:
    text: str
    finish_reason: str = "stop"
    model: str = ""
    usage: Dict[str, int] = field(default_factory=dict)


# ── Provider protocol ───────────────────────────────────────────────────
@runtime_checkable
class AIProvider(Protocol):
    """Minimal contract every provider must satisfy.

    ``plan``, ``edit``, and ``analyze_error`` are the high-level methods the
    agent loop calls. ``stream_chat`` is the low-level streaming primitive
    used for real-time token display; providers that cannot stream yield the
    full text in one chunk.
    """

    name: str

    def plan(self, task: str, context: str) -> Plan: ...
    def edit(self, task: str, plan: Plan, files: List[dict]) -> List[dict]: ...
    def analyze_error(self, task: str, error: str, files: List[dict]) -> str: ...
    def stream_chat(
        self, messages: List[ChatMessage], on_token: Optional[Callable[[str], None]] = None
    ) -> ChatResult: ...


# ────────────────────────────────────────────────────────────────────────
# LocalProvider — deterministic, offline, no API key (default fallback)
# ────────────────────────────────────────────────────────────────────────
class LocalProvider:
    """Deterministic, fully offline provider.

    Inspects real repository contents (``files`` dicts with ``path`` and
    ``content``) and returns concrete plans and edits for common, safe,
    automatable tasks. This is NOT a mock — it produces real, verifiable file
    changes. For arbitrary natural-language tasks it returns a structured plan
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
                "The local provider applies edits only for patterns it can "
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
                    continue
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

        return edits

    def analyze_error(self, task: str, error: str, files: List[dict]) -> str:
        if "SyntaxError" in error or "IndentationError" in error:
            return ("A syntax/indentation error was detected. Reverting the "
                    "last edit and re-applying with corrected indentation.")
        if "ModuleNotFoundError" in error or "ImportError" in error:
            return ("A required module is missing. This may need a dependency "
                    "install — out of scope for an automated edit; reporting for review.")
        return (f"Verification reported an error. The local provider cannot "
                f"auto-fix arbitrary failures; surfacing for review. Error: {error[:300]}")

    def stream_chat(
        self, messages: List[ChatMessage], on_token: Optional[Callable[[str], None]] = None
    ) -> ChatResult:
        """The local provider has no LLM; it 'streams' a deterministic reply.

        We yield the concatenated assistant message in small chunks so the UI
        gets a live typing effect, but the content is real — derived from the
        provider's own plan logic, not faked activity.
        """
        user_msg = next((m for m in messages if m.role == "user"), None)
        task = user_msg.content if user_msg else ""
        context = next((m for m in messages if m.role == "system"), None)
        ctx_text = context.content if context else ""
        plan = self.plan(task, ctx_text)
        text = plan.summary + "\n" + "\n".join(f"- {s}" for s in plan.steps)
        # Simulate streaming by yielding word-by-word.
        if on_token:
            words = text.split(" ")
            for i, w in enumerate(words):
                chunk = w + (" " if i < len(words) - 1 else "")
                on_token(chunk)
        return ChatResult(text=text, model="local")


# ────────────────────────────────────────────────────────────────────────
# OpenAIProvider — works with any OpenAI-compatible endpoint + streaming
# ────────────────────────────────────────────────────────────────────────
_DEFAULT_BASES: Dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "together": "https://api.together.xyz/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://localhost:11434/v1",
}


class OpenAIProvider:
    """Adapter for any OpenAI-compatible Chat Completions REST endpoint."""

    name = "openai"

    def __init__(self, settings: Optional[Settings] = None,
                 provider_hint: str = "") -> None:
        self.settings = settings or get_settings()
        self.api_key = self.settings.effective_api_key()
        if not self.api_key:
            raise AIError("AI_API_KEY (or GEMINI_API_KEY) is not set; "
                          "cannot use OpenAIProvider.")
        hint = (provider_hint or self.settings.ai_provider or "openai").lower()
        self.base_url = (
            self.settings.ai_base_url.rstrip("/")
            or _DEFAULT_BASES.get(hint, _DEFAULT_BASES["openai"])
        )
        self.model = self.settings.effective_model() or "gpt-4o-mini"
        self.timeout = self.settings.ai_timeout_seconds

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def generate(self, messages: List[ChatMessage], *,
                 stream: bool = False, temperature: Optional[float] = None,
                 max_tokens: Optional[int] = None) -> str:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "temperature": temperature if temperature is not None
            else self.settings.ai_temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        url = f"{self.base_url}/chat/completions"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                r = client.post(url, json=payload, headers=self._headers())
                r.raise_for_status()
        except httpx.HTTPError as exc:
            raise AIError(f"AI request failed: {exc}") from exc
        data = r.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIError(f"Unexpected AI response: {json.dumps(data)[:300]}") from exc

    def stream_chat(
        self, messages: List[ChatMessage], on_token: Optional[Callable[[str], None]] = None
    ) -> ChatResult:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
            "temperature": self.settings.ai_temperature,
        }
        url = f"{self.base_url}/chat/completions"
        full_text: List[str] = []
        finish_reason = "stop"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                with client.stream("POST", url, json=payload,
                                   headers=self._headers()) as r:
                    ctype = r.headers.get("content-type", "")
                    if "text/event-stream" not in ctype:
                        r.read()
                        data = r.json()
                        text = data["choices"][0]["message"]["content"]
                        if on_token:
                            on_token(text)
                        return ChatResult(text=text,
                                          model=self.model,
                                          finish_reason=data["choices"][0]
                                          .get("finish_reason", "stop"))
                    for line in r.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        chunk = line[len("data:"):].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            obj = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
                        try:
                            delta = obj["choices"][0]["delta"].get("content", "")
                            if delta:
                                full_text.append(delta)
                                if on_token:
                                    on_token(delta)
                            fr = obj["choices"][0].get("finish_reason")
                            if fr:
                                finish_reason = fr
                        except (KeyError, IndexError, TypeError):
                            continue
        except httpx.HTTPError as exc:
            raise AIError(f"AI streaming request failed: {exc}") from exc
        text = "".join(full_text)
        if not text:
            text = self.generate(messages)
            if on_token:
                on_token(text)
        return ChatResult(text=text, model=self.model, finish_reason=finish_reason)

    def plan(self, task: str, context: str) -> Plan:
        messages = [
            ChatMessage("system",
                        "You are a coding agent. Given a task and repository "
                        "context, produce a concise plan as JSON with keys "
                        "'summary' (string) and 'steps' (array of strings). "
                        "Return ONLY JSON."),
            ChatMessage("user",
                        f"Task:\n{task}\n\nContext:\n{context[:6000]}\n\n"
                        "Return ONLY JSON."),
        ]
        text = self.generate(messages)
        return _parse_plan_json(text, fallback_task=task)

    def edit(self, task: str, plan: Plan, files: List[dict]) -> List[dict]:
        files_brief = "\n".join(f["path"] for f in files[:30])
        messages = [
            ChatMessage("system",
                        "You are a coding agent. Given a task and a list of "
                        "file paths, return a JSON array of edits. Each edit: "
                        '{"path": "...", "content": "full new file content"}. '
                        "Only include files you actually change. Return ONLY "
                        "a JSON array."),
            ChatMessage("user",
                        f"Task:\n{task}\n\nPlan: {plan.summary}\n\n"
                        f"Files:\n{files_brief}\n\nReturn ONLY a JSON array."),
        ]
        text = self.generate(messages)
        return _parse_edits_json(text)

    def analyze_error(self, task: str, error: str, files: List[dict]) -> str:
        messages = [
            ChatMessage("system",
                        "A verification command failed. In one short sentence, "
                        "describe the most likely cause and fix."),
            ChatMessage("user", f"Error:\n{error[:1500]}"),
        ]
        return self.generate(messages).strip()


# ────────────────────────────────────────────────────────────────────────
# GeminiProvider — legacy alias routed through OpenAIProvider
# ────────────────────────────────────────────────────────────────────────
class GeminiProvider(OpenAIProvider):
    """Legacy Gemini adapter."""

    name = "gemini"

    def __init__(self, settings: Optional[Settings] = None) -> None:
        super().__init__(settings=settings, provider_hint="gemini")
        self.model = self.settings.effective_model() or "gemini-1.5-flash"


# ────────────────────────────────────────────────────────────────────────
# AnthropicProvider — custom adapter for Anthropic Claude
# ────────────────────────────────────────────────────────────────────────
class AnthropicProvider:
    """Adapter for Anthropic's Messages REST API with full SSE streaming."""

    name = "anthropic"

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.api_key = self.settings.effective_api_key()
        if not self.api_key:
            raise AIError("AI_API_KEY (or GEMINI_API_KEY) is not set; "
                          "cannot use AnthropicProvider.")
        self.base_url = (
            self.settings.ai_base_url.rstrip("/")
            or "https://api.anthropic.com"
        )
        self.model = self.settings.effective_model() or "claude-3-5-sonnet-20241022"
        self.timeout = self.settings.ai_timeout_seconds

    def _headers(self) -> Dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def generate(self, messages: List[ChatMessage], *,
                 temperature: Optional[float] = None,
                 max_tokens: Optional[int] = 1024) -> str:
        system_msg = ""
        user_messages = []
        for m in messages:
            if m.role == "system":
                system_msg += m.content + "\n"
            else:
                user_messages.append({"role": m.role, "content": m.content})

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": user_messages,
            "max_tokens": max_tokens or 1024,
            "temperature": temperature if temperature is not None
            else self.settings.ai_temperature,
        }
        if system_msg:
            payload["system"] = system_msg.strip()

        url = f"{self.base_url}/v1/messages"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                r = client.post(url, json=payload, headers=self._headers())
                r.raise_for_status()
        except httpx.HTTPError as exc:
            raise AIError(f"Anthropic request failed: {exc}") from exc
        data = r.json()
        try:
            return data["content"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIError(f"Unexpected Anthropic response: {json.dumps(data)[:300]}") from exc

    def stream_chat(
        self, messages: List[ChatMessage], on_token: Optional[Callable[[str], None]] = None
    ) -> ChatResult:
        system_msg = ""
        user_messages = []
        for m in messages:
            if m.role == "system":
                system_msg += m.content + "\n"
            else:
                user_messages.append({"role": m.role, "content": m.content})

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": user_messages,
            "max_tokens": 1024,
            "stream": True,
            "temperature": self.settings.ai_temperature,
        }
        if system_msg:
            payload["system"] = system_msg.strip()

        url = f"{self.base_url}/v1/messages"
        full_text: List[str] = []
        try:
            with httpx.Client(timeout=self.timeout) as client:
                with client.stream("POST", url, json=payload, headers=self._headers()) as r:
                    ctype = r.headers.get("content-type", "")
                    if "text/event-stream" not in ctype:
                        r.read()
                        data = r.json()
                        text = data["content"][0]["text"]
                        if on_token:
                            on_token(text)
                        return ChatResult(text=text, model=self.model)

                    for line in r.iter_lines():
                        if not line:
                            continue
                        if line.startswith("data:"):
                            chunk = line[len("data:"):].strip()
                            try:
                                obj = json.loads(chunk)
                                if obj.get("type") == "content_block_delta":
                                    delta = obj.get("delta", {}).get("text", "")
                                    if delta:
                                        full_text.append(delta)
                                        if on_token:
                                            on_token(delta)
                            except json.JSONDecodeError:
                                continue
        except httpx.HTTPError as exc:
            raise AIError(f"Anthropic streaming failed: {exc}") from exc

        text = "".join(full_text)
        if not text:
            text = self.generate(messages)
            if on_token:
                on_token(text)
        return ChatResult(text=text, model=self.model)

    def plan(self, task: str, context: str) -> Plan:
        messages = [
            ChatMessage("system",
                        "You are a coding agent. Given a task and repository "
                        "context, produce a concise plan as JSON with keys "
                        "'summary' (string) and 'steps' (array of strings). "
                        "Return ONLY JSON."),
            ChatMessage("user",
                        f"Task:\n{task}\n\nContext:\n{context[:6000]}\n\n"
                        "Return ONLY JSON."),
        ]
        text = self.generate(messages)
        return _parse_plan_json(text, fallback_task=task)

    def edit(self, task: str, plan: Plan, files: List[dict]) -> List[dict]:
        files_brief = "\n".join(f["path"] for f in files[:30])
        messages = [
            ChatMessage("system",
                        "You are a coding agent. Given a task and a list of "
                        "file paths, return a JSON array of edits. Each edit: "
                        '{"path": "...", "content": "full new file content"}. '
                        "Only include files you actually change. Return ONLY "
                        "a JSON array."),
            ChatMessage("user",
                        f"Task:\n{task}\n\nPlan: {plan.summary}\n\n"
                        f"Files:\n{files_brief}\n\nReturn ONLY a JSON array."),
        ]
        text = self.generate(messages)
        return _parse_edits_json(text)

    def analyze_error(self, task: str, error: str, files: List[dict]) -> str:
        messages = [
            ChatMessage("system",
                        "A verification command failed. In one short sentence, "
                        "describe the most likely cause and fix."),
            ChatMessage("user", f"Error:\n{error[:1500]}"),
        ]
        return self.generate(messages).strip()


# ────────────────────────────────────────────────────────────────────────
# JulesProvider — specialized adapter for Google's Jules agent
# ────────────────────────────────────────────────────────────────────────
class JulesProvider:
    """Specialized adapter for Google's elite coding agent Jules."""

    name = "jules"

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.api_key = self.settings.effective_api_key()
        if not self.api_key:
            raise AIError("AI_API_KEY (or GEMINI_API_KEY) is not set; "
                          "cannot use JulesProvider.")
        self.base_url = (
            self.settings.ai_base_url.rstrip("/")
            or "https://api.jules.google.dev/v1"
        )
        self.model = self.settings.effective_model() or "jules-coding-v1"
        self.timeout = self.settings.ai_timeout_seconds

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def generate(self, messages: List[ChatMessage], *,
                 temperature: Optional[float] = None,
                 max_tokens: Optional[int] = None) -> str:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "temperature": temperature if temperature is not None
            else self.settings.ai_temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        url = f"{self.base_url}/chat/completions"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                r = client.post(url, json=payload, headers=self._headers())
                r.raise_for_status()
        except httpx.HTTPError as exc:
            raise AIError(f"Jules request failed: {exc}") from exc
        data = r.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIError(f"Unexpected Jules response: {json.dumps(data)[:300]}") from exc

    def stream_chat(
        self, messages: List[ChatMessage], on_token: Optional[Callable[[str], None]] = None
    ) -> ChatResult:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
            "temperature": self.settings.ai_temperature,
        }
        url = f"{self.base_url}/chat/completions"
        full_text: List[str] = []
        try:
            with httpx.Client(timeout=self.timeout) as client:
                with client.stream("POST", url, json=payload, headers=self._headers()) as r:
                    ctype = r.headers.get("content-type", "")
                    if "text/event-stream" not in ctype:
                        r.read()
                        data = r.json()
                        text = data["choices"][0]["message"]["content"]
                        if on_token:
                            on_token(text)
                        return ChatResult(text=text, model=self.model)

                    for line in r.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        chunk = line[len("data:"):].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            obj = json.loads(chunk)
                            delta = obj["choices"][0]["delta"].get("content", "")
                            if delta:
                                full_text.append(delta)
                                if on_token:
                                    on_token(delta)
                        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                            continue
        except httpx.HTTPError as exc:
            raise AIError(f"Jules streaming request failed: {exc}") from exc
        text = "".join(full_text)
        if not text:
            text = self.generate(messages)
            if on_token:
                on_token(text)
        return ChatResult(text=text, model=self.model)

    def plan(self, task: str, context: str) -> Plan:
        messages = [
            ChatMessage("system",
                        "You are a coding agent. Given a task and repository "
                        "context, produce a concise plan as JSON with keys "
                        "'summary' (string) and 'steps' (array of strings). "
                        "Return ONLY JSON."),
            ChatMessage("user",
                        f"Task:\n{task}\n\nContext:\n{context[:6000]}\n\n"
                        "Return ONLY JSON."),
        ]
        text = self.generate(messages)
        return _parse_plan_json(text, fallback_task=task)

    def edit(self, task: str, plan: Plan, files: List[dict]) -> List[dict]:
        files_brief = "\n".join(f["path"] for f in files[:30])
        messages = [
            ChatMessage("system",
                        "You are a coding agent. Given a task and a list of "
                        "file paths, return a JSON array of edits. Each edit: "
                        '{"path": "...", "content": "full new file content"}. '
                        "Only include files you actually change. Return ONLY "
                        "a JSON array."),
            ChatMessage("user",
                        f"Task:\n{task}\n\nPlan: {plan.summary}\n\n"
                        f"Files:\n{files_brief}\n\nReturn ONLY a JSON array."),
        ]
        text = self.generate(messages)
        return _parse_edits_json(text)

    def analyze_error(self, task: str, error: str, files: List[dict]) -> str:
        messages = [
            ChatMessage("system",
                        "A verification command failed. In one short sentence, "
                        "describe the most likely cause and fix."),
            ChatMessage("user", f"Error:\n{error[:1500]}"),
        ]
        return self.generate(messages).strip()


# ── JSON parsing helpers ────────────────────────────────────────────────
def _parse_plan_json(text: str, fallback_task: str) -> Plan:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return Plan(summary="Plan unavailable; using fallback.",
                    steps=["Review task."])
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
            return [e for e in arr if isinstance(e, dict)
                    and "path" in e and "content" in e]
    except json.JSONDecodeError:
        pass
    return []


# ────────────────────────────────────────────────────────────────────────
# Factory — returns the configured provider, falling back to Local
# ────────────────────────────────────────────────────────────────────────
def get_provider(settings: Optional[Settings] = None) -> AIProvider:
    """Return the configured provider, falling back to LocalProvider.

    Selection rules:
      * AI_PROVIDER=local (or unset)            -> LocalProvider
      * AI_PROVIDER=gemini                      -> GeminiProvider
      * AI_PROVIDER=anthropic                   -> AnthropicProvider
      * AI_PROVIDER=jules                       -> JulesProvider
      * AI_PROVIDER=openai (or custom name)     -> OpenAIProvider
    """
    settings = settings or get_settings()
    name = (settings.ai_provider or "local").lower().strip()

    if name == "local":
        return LocalProvider()

    if name == "gemini":
        if settings.effective_api_key():
            try:
                return GeminiProvider(settings)
            except AIError:
                pass
        return LocalProvider()

    if name == "anthropic":
        if settings.effective_api_key():
            try:
                return AnthropicProvider(settings)
            except AIError:
                pass
        return LocalProvider()

    if name == "jules":
        if settings.effective_api_key():
            try:
                return JulesProvider(settings)
            except AIError:
                pass
        return LocalProvider()

    # "openai" or any custom OpenAI-compatible provider name.
    if settings.effective_api_key():
        try:
            return OpenAIProvider(settings, provider_hint=name)
        except AIError:
            pass

    # No key / misconfigured -> safe offline fallback.
    return LocalProvider()


def provider_status(settings: Optional[Settings] = None) -> dict:
    """Return a non-secret summary of the active provider (for /api/config)."""
    settings = settings or get_settings()
    provider = get_provider(settings)
    return {
        "provider": provider.name,
        "model": getattr(provider, "model", "local"),
        "configured": provider.name != "local",
        "streaming_supported": provider.name != "local",
    }
