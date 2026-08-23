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
    Official adapter for Google's async coding agent Jules via the
    https://jules.googleapis.com/v1alpha REST API (x-goog-api-key auth).
    Bridges Jules' async session model (create → poll → collect activities
    & artifacts) to the synchronous AIProvider protocol.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    runtime_checkable,
)

import httpx

from config import Settings, get_settings


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
    "xiaomi": "https://api.xiaomimimo.com/v1",
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
                 max_tokens: Optional[int] = None,
                 json_mode: bool = False) -> str:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "temperature": temperature if temperature is not None
            else self.settings.ai_temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
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



# JulesProvider — official Google Jules async coding-agent adapter (v1.1.0)
# ────────────────────────────────────────────────────────────────────────────
class JulesProvider:
    """Adapter for the *official* Google Jules REST API.

    Jules is an *asynchronous, session-based* coding agent (not an
    OpenAI-compatible chat-completions endpoint). The official API lives at
    ``https://jules.googleapis.com/v1alpha`` and authenticates with the
    ``x-goog-api-key`` header (NOT ``Authorization: Bearer``).

    Workflow
    --------
    1. Create a session (optionally repoless) with a user prompt.
    2. Poll ``GET /sessions/{id}`` until ``state`` reaches a terminal value
       (``COMPLETED`` or ``FAILED``), auto-approving any generated plan.
    3. List ``GET /sessions/{id}/activities`` to collect the structured event
       stream (plan, progress, agent messages).
    4. Collect artifacts (``changeSet`` with a ``gitPatch`` / ``unidiffPatch``,
       ``bashOutput``, ``media``) and parse them into the {path, content} edit
       format used by the rest of the agent loop.

    Because the rest of PK-Ninja-Agent expects a *synchronous* provider
    (``generate``, ``plan``, ``edit``, ``stream_chat``, ``analyze_error``),
    this adapter bridges the two models: each synchronous call creates a
    short-lived Jules session, polls it to completion, and returns the
    parsed result. Diagnostics, metrics, structured logging, retry/timeout
    and error recovery are all built in.
    """

    name = "jules"
    model = "jules-async-agent"

    # Official Jules session states (terminal states are COMPLETED / FAILED).
    _TERMINAL_STATES = {"COMPLETED", "FAILED"}
    _NEEDS_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.api_key = self.settings.effective_jules_key()
        if not self.api_key:
            raise AIError(
                "JULES_API_KEY (or AI_API_KEY / GEMINI_API_KEY) is not set; "
                "cannot use JulesProvider."
            )
        self.base_url = (
            self.settings.jules_base_url.rstrip("/")
            or "https://jules.googleapis.com/v1alpha"
        )
        self.timeout = self.settings.ai_timeout_seconds
        self.poll_interval = max(1.0, float(self.settings.jules_poll_interval_seconds))
        self.poll_timeout = max(30, int(self.settings.jules_poll_timeout_seconds))
        self.max_retries = max(0, int(self.settings.jules_max_retries))
        # Lightweight diagnostics / metrics counters (non-secret).
        self.diagnostics: Dict[str, Any] = {
            "sessions_created": 0,
            "sessions_completed": 0,
            "sessions_failed": 0,
            "plan_approvals": 0,
            "retries": 0,
            "last_session_id": None,
            "last_error": None,
        }

    # ── HTTP layer with retry / structured logging ────────────────────────
    def _headers(self) -> Dict[str, str]:
        # Official Jules auth: x-goog-api-key header (NOT Bearer).
        return {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Perform an HTTP request to the Jules API with retry + logging.

        Retries on network errors, 429 and 5xx using a small exponential
        back-off. Raises :class:`AIError` on non-recoverable failures.
        """
        import logging
        import time as _time

        log = logging.getLogger("pk_ninja.jules")
        url = f"{self.base_url}{path}"
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    r = client.request(method, url, json=json_body, params=params,
                                       headers=self._headers())
                # Retryable status codes (429 / 5xx) — raise a flagged error so
                # the except clause can decide whether to retry.
                if r.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError(
                        f"retryable status {r.status_code}", request=r.request,
                        response=r,
                    )
                r.raise_for_status()
                if not r.content:
                    return {}
                return r.json()
            except httpx.HTTPStatusError as exc:
                # Only retry on the retryable status codes we flagged above.
                status = getattr(getattr(exc, "response", None), "status_code", None)
                is_retryable = status in (429, 500, 502, 503, 504)
                last_exc = exc
                if is_retryable and attempt < self.max_retries:
                    self.diagnostics["retries"] += 1
                    backoff = min(2 ** attempt, 8)
                    log.warning(
                        "jules.request.retry attempt=%d status=%s backoff=%ds",
                        attempt + 1, _status_of(exc), backoff,
                    )
                    _time.sleep(backoff)
                    continue
                self.diagnostics["last_error"] = str(last_exc)[:200]
                raise AIError(
                    f"Jules API request failed ({method} {path}): {last_exc}"
                ) from last_exc
            except httpx.HTTPError as exc:
                # Network-level errors (connect/timeout/read) — always retryable.
                last_exc = exc
                if attempt < self.max_retries:
                    self.diagnostics["retries"] += 1
                    backoff = min(2 ** attempt, 8)
                    log.warning(
                        "jules.request.retry attempt=%d status=%s backoff=%ds",
                        attempt + 1, _status_of(exc), backoff,
                    )
                    _time.sleep(backoff)
                    continue
                self.diagnostics["last_error"] = str(last_exc)[:200]
                raise AIError(
                    f"Jules API request failed ({method} {path}): {last_exc}"
                ) from last_exc
        # Should not reach here, but guard anyway.
        self.diagnostics["last_error"] = str(last_exc)[:200]
        raise AIError(f"Jules API request failed ({method} {path}): {last_exc}") \
            from last_exc

    # ── Session lifecycle ─────────────────────────────────────────────────
    def _create_session(self, prompt: str, repo_url: Optional[str] = None,
                        branch: Optional[str] = None) -> str:
        """Create a Jules session and return its name (id).

        A *repoless* session (no ``sourceContext``) is used for free-form
        chat / plan / analyze tasks. When ``repo_url`` is given, a session
        with repository context is created.

        Uses the official ``prompt`` field per the Jules REST API spec
        (NOT ``userInput`` — that was a previous incorrect assumption).
        """
        body: Dict[str, Any] = {"prompt": prompt}
        if repo_url:
            body["sourceContext"] = {
                "githubRepoContext": {
                    "url": repo_url,
                }
            }
            if branch:
                body["sourceContext"]["githubRepoContext"]["startingBranch"] = branch
        data = self._request("POST", "/sessions", json_body=body)
        name = data.get("name")
        if not name:
            raise AIError(
                f"Jules session creation returned no name: "
                f"{json.dumps(data)[:300]}"
            )
        self.diagnostics["sessions_created"] += 1
        self.diagnostics["last_session_id"] = name
        return name

    def _get_session(self, session_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/sessions/{session_id.split('/')[-1]}")

    def _approve_plan(self, session_id: str) -> None:
        self._request("POST", f"/sessions/{session_id.split('/')[-1]}:approvePlan")
        self.diagnostics["plan_approvals"] += 1

    def _send_message(self, session_id: str, message: str) -> None:
        # Official Jules API uses "prompt" field for sendMessage.
        self._request(
            "POST", f"/sessions/{session_id.split('/')[-1]}:sendMessage",
            json_body={"prompt": message},
        )

    def _list_activities(self, session_id: str) -> List[Dict[str, Any]]:
        data = self._request(
            "GET", f"/sessions/{session_id.split('/')[-1]}/activities",
        )
        return data.get("activities", []) if isinstance(data, dict) else []

    def _poll_to_terminal(self, session_id: str) -> Dict[str, Any]:
        """Poll a session until it reaches a terminal state.

        Auto-approves a generated plan when the session enters
        ``AWAITING_PLAN_APPROVAL`` so the synchronous bridge does not block
        forever waiting for a human.
        """
        import time as _time

        deadline = _time.monotonic() + self.poll_timeout
        session: Dict[str, Any] = {}
        while _time.monotonic() < deadline:
            session = self._get_session(session_id)
            state = session.get("state", "")
            if state == self._NEEDS_PLAN_APPROVAL:
                try:
                    self._approve_plan(session_id)
                except AIError:
                    # If approval fails, keep polling — the session may still
                    # transition (e.g. auto-complete) or fail.
                    pass
            if state in self._TERMINAL_STATES:
                if state == "COMPLETED":
                    self.diagnostics["sessions_completed"] += 1
                else:
                    self.diagnostics["sessions_failed"] += 1
                return session
            _time.sleep(self.poll_interval)
        raise AIError(
            f"Jules session {session_id} did not reach a terminal state "
            f"within {self.poll_timeout}s (last state="
            f"{session.get('state', 'unknown')})."
        )

    # ── Artifact parsing ──────────────────────────────────────────────────
    def _collect_agent_text(self, session_id: str) -> str:
        """Collect all agentMessaged activity text from a session.

        Per the official Jules REST API, each activity has exactly one
        populated event-type field (e.g. ``agentMessaged``, ``planGenerated``)
        as a direct key on the activity object — not inside an ``events``
        array.
        """
        activities = self._list_activities(session_id)
        texts: List[str] = []
        for act in activities:
            # Official API: event type is a direct field on the activity.
            agent_msg = act.get("agentMessaged")
            if agent_msg:
                txt = agent_msg.get("agentMessage") or agent_msg.get("text") or ""
                if txt:
                    texts.append(txt)
                continue
            # Fallback: legacy nested events structure (backward compat).
            for ev in act.get("events", []) or []:
                kind = ev.get("event") or ev.get("type")
                if kind == "agentMessaged":
                    payload = ev.get("payload", {}) or {}
                    txt = payload.get("agentMessage") or payload.get("text") or ""
                    if txt:
                        texts.append(txt)
        return "\n".join(texts).strip()

    def _collect_edits(self, session_id: str) -> List[dict]:
        """Parse a Jules changeSet (gitPatch) into {path, content} edits.

        Per the official Jules REST API, artifacts (including ``changeSet``)
        are direct fields on the activity object.
        """
        activities = self._list_activities(session_id)
        edits: List[dict] = []
        for act in activities:
            # Official API: artifacts are direct fields on the activity.
            cs = act.get("changeSet")
            if not cs:
                # Also check artifacts array (alternate API structure).
                for art in act.get("artifacts", []) or []:
                    cs = art.get("changeSet")
                    if cs:
                        break
            if not cs:
                continue
            patch = cs.get("gitPatch", {}) or {}
            unidiff = patch.get("unidiffPatch", "")
            if not unidiff:
                continue
            edits.extend(_parse_unidiff(unidiff))
        return edits

    # ── Sync-protocol bridge ──────────────────────────────────────────────
    def generate(self, messages: List[ChatMessage], *,
                 temperature: Optional[float] = None,
                 max_tokens: Optional[int] = None) -> str:
        """Synchronous bridge: build a prompt, run a repoless Jules session.

        Returns the concatenated ``agentMessaged`` text. ``temperature`` and
        ``max_tokens`` are accepted for protocol parity but are not part of
        the Jules API (Jules controls its own model params) — they are ignored.
        """
        prompt = _messages_to_prompt(messages)
        session_id = self._create_session(prompt)
        self._poll_to_terminal(session_id)
        text = self._collect_agent_text(session_id)
        if not text:
            text = "(Jules completed the session with no agent message.)"
        return text

    def stream_chat(
        self, messages: List[ChatMessage], on_token: Optional[Callable[[str], None]] = None
    ) -> ChatResult:
        """Jules has no SSE streaming endpoint.

        We run the session to completion and then *emulate* streaming by
        delivering the collected agent text in chunks so the caller's
        ``on_token`` callback still receives incremental updates and the
        streaming UI contract is preserved.
        """
        import time as _time

        prompt = _messages_to_prompt(messages)
        session_id = self._create_session(prompt)
        self._poll_to_terminal(session_id)
        text = self._collect_agent_text(session_id)
        if not text:
            text = "(Jules completed the session with no agent message.)"
        # Emulated streaming: deliver in ~12-token chunks.
        words = text.split()
        chunk_size = 12
        delivered: List[str] = []
        for i in range(0, len(words), chunk_size):
            piece = " ".join(words[i:i + chunk_size])
            delivered.append(piece)
            if on_token:
                on_token(piece + (" " if i + chunk_size < len(words) else ""))
            _time.sleep(0.01)  # small cadence so the UI animates
        full = " ".join(delivered)
        return ChatResult(text=full, model=self.model)

    def plan(self, task: str, context: str) -> Plan:
        messages = [
            ChatMessage(
                "system",
                "You are a coding agent. Given a task and repository "
                "context, produce a concise plan as JSON with keys "
                "'summary' (string) and 'steps' (array of strings). "
                "Return ONLY JSON.",
            ),
            ChatMessage(
                "user",
                f"Task:\n{task}\n\nContext:\n{context[:6000]}\n\n"
                "Return ONLY JSON.",
            ),
        ]
        text = self.generate(messages)
        return _parse_plan_json(text, fallback_task=task)

    def edit(self, task: str, plan: Plan, files: List[dict]) -> List[dict]:
        files_brief = "\n".join(f["path"] for f in files[:30])
        prompt = (
            f"Task:\n{task}\n\nPlan: {plan.summary}\n\n"
            f"Files:\n{files_brief}\n\n"
            "Apply the changes to the repository. Return the diff."
        )
        session_id = self._create_session(prompt)
        self._poll_to_terminal(session_id)
        edits = self._collect_edits(session_id)
        if not edits:
            # Fall back to agent text → JSON parsing like the other providers.
            text = self._collect_agent_text(session_id)
            edits = _parse_edits_json(text)
        return edits

    def analyze_error(self, task: str, error: str, files: List[dict]) -> str:
        messages = [
            ChatMessage(
                "system",
                "A verification command failed. In one short sentence, "
                "describe the most likely cause and fix.",
            ),
            ChatMessage("user", f"Error:\n{error[:1500]}"),
        ]
        return self.generate(messages).strip()

    def diagnostics_summary(self) -> Dict[str, Any]:
        """Return a non-secret diagnostics/metrics snapshot."""
        return dict(self.diagnostics)


def _status_of(exc: Exception) -> str:
    """Best-effort extraction of an HTTP status code from an httpx error."""
    try:
        return str(getattr(exc, "response", None).status_code)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        return "network"


def _messages_to_prompt(messages: List[ChatMessage]) -> str:
    """Flatten a list of ChatMessage into a single Jules user prompt."""
    parts: List[str] = []
    for m in messages:
        role = m.role.upper()
        parts.append(f"[{role}]\n{m.content}")
    return "\n\n".join(parts)


def _parse_unidiff(unidiff: str) -> List[dict]:
    """Parse a unidiff patch into a list of {path, content} edits.

    Each edited file's *full new content* is reconstructed by applying the
    hunks to the original lines present in the diff context. This is a
    best-effort parser sufficient for the agent loop; files that cannot be
    reconstructed are skipped.
    """
    edits: List[dict] = []
    current_path: Optional[str] = None
    current_lines: List[str] = []
    hunk_header_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

    def _flush() -> None:
        nonlocal current_path, current_lines
        if current_path and current_lines:
            edits.append({"path": current_path, "content": "".join(current_lines)})
        current_path = None
        current_lines = []

    for line in unidiff.splitlines(keepends=True):
        if line.startswith("+++ "):
            _flush()
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            current_path = path.split("\t")[0] if path != "/dev/null" else None
            continue
        if line.startswith("--- "):
            continue
        if hunk_header_re.match(line):
            continue
        if not current_path:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            current_lines.append(line[1:])
        elif line.startswith("-"):
            continue
        elif line.startswith(" "):
            current_lines.append(line[1:])
        elif line.startswith("\\"):
            continue
    _flush()
    return edits


# ── JSON parsing helpers ────────────────────────────────────────────────
def _parse_plan_json(text: str, fallback_task: str) -> Plan:
    """Parse a plan from model output, tolerating common wrapper noise.

    Handles markdown code fences, leading/trailing prose around the JSON,
    and uses raw_decode so text after the JSON object is ignored.
    """
    if not text or not text.strip():
        return Plan(summary=f"Plan for: {fallback_task[:80]}",
                    steps=["Review task."])
    cleaned = re.sub(r"```(?:json)?", "", text)
    # Fast path: greedy match like before.
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    candidates = []
    if m:
        candidates.append(m.group(0))
    # Robust path: balanced-brace scan from the first '{' — survives prose
    # before/after the JSON and nested braces inside strings.
    start = cleaned.find("{")
    if start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(cleaned)):
            c = cleaned[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        candidates.insert(0, cleaned[start:i + 1])
                        break
    for cand in candidates:
        try:
            obj = json.loads(cand)
            steps = obj.get("steps") or ["Review task."]
            if isinstance(steps, list):
                steps = [str(s) for s in steps if s] or ["Review task."]
            summary = obj.get("summary") or f"Plan for: {fallback_task[:80]}"
            return Plan(summary=str(summary), steps=steps)
        except json.JSONDecodeError:
            continue
    # Last resort: no JSON at all — use the model's own text as the answer
    # step instead of a generic "unavailable" message.
    snippet = cleaned.strip().splitlines()[0][:120] if cleaned.strip() else fallback_task[:120]
    return Plan(summary=snippet or f"Plan for: {fallback_task[:80]}",
                steps=["Review task and respond."])


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
        if settings.effective_jules_key():
            try:
                return JulesProvider(settings)
            except AIError:
                pass
        return LocalProvider()

    if name == "xiaomi":
        if settings.effective_api_key():
            try:
                return OpenAIProvider(settings, provider_hint="xiaomi")
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
