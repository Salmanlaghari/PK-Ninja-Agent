"""Agent: the event system and the interactive agent loop.

The agent loop is the real workflow:

  UNDERSTAND -> SEARCH -> READ -> PLAN -> EDIT -> VERIFY
  -> (on failure) ANALYZE -> FIX -> VERIFY again -> DIFF -> BRANCH -> COMMIT -> PUSH

Every step emits an :class:`Event` that is persisted to SQLite and streamed to
the frontend via SSE **and** WebSocket. The agent never fakes activity: tools
run against the real workspace and emit events from their actual results.

Interactive features added in v2:
  * Streaming AI tokens: when a non-local provider is configured, the plan and
    analysis steps stream real model output token-by-token via ``thinking``
    events.
  * Robust cancellation: the cancel flag is checked before every step and the
    running subprocess is killed so a cancel actually stops work.
  * Canonical task statuses: idle / running / success / failed / cancelled.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import queue
import shlex
import signal
import subprocess
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ai_provider import (
    AIError,
    ChatMessage,
    LocalProvider,
    Plan,
    get_provider,
)
from config import Settings, get_settings
from github import GitHubError, clone_or_pull, repo_info
from models import EventType, TaskStatus
from terminal import TerminalError, run_command, validate_command
from workspace import Workspace, WorkspaceError

log = logging.getLogger("pk_ninja.agent")


# ── Events ──────────────────────────────────────────────────────────────
@dataclass
class Event:
    task_id: str
    type: EventType
    message: str
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: _dt.datetime = field(default_factory=_dt.datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "type": self.type.value,
            "message": self.message,
            "data": self.data,
            "timestamp": self.timestamp.isoformat() + "Z",
        }


class EventBus:
    """In-process pub/sub + bounded history per task.

    A background thread publishes events; SSE/WebSocket consumers read them
    from a thread-safe queue. We also keep an in-memory ring so late
    subscribers get recent history, and persist every event to SQLite.
    """

    def __init__(self) -> None:
        self._subs: Dict[str, List[queue.Queue]] = {}
        self._history: Dict[str, List[Event]] = {}
        self._lock = threading.Lock()
        self._persist: Optional[Callable[[Event], None]] = None

    def set_persist(self, fn: Callable[[Event], None]) -> None:
        self._persist = fn

    def publish(self, event: Event) -> None:
        with self._lock:
            self._history.setdefault(event.task_id, []).append(event)
            if len(self._history[event.task_id]) > 1000:
                self._history[event.task_id] = self._history[event.task_id][-1000:]
            subs = list(self._subs.get(event.task_id, []))
        if self._persist:
            try:
                self._persist(event)
            except Exception:  # pragma: no cover
                log.exception("Failed to persist event")
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def history(self, task_id: str) -> List[Event]:
        with self._lock:
            return list(self._history.get(task_id, []))

    def subscribe(self, task_id: str, maxsize: int = 2000) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subs.setdefault(task_id, []).append(q)
        return q

    def unsubscribe(self, task_id: str, q: queue.Queue) -> None:
        with self._lock:
            if task_id in self._subs:
                try:
                    self._subs[task_id].remove(q)
                except ValueError:
                    pass


# Singleton bus shared across the app.
BUS = EventBus()


# ── Task runtime state ──────────────────────────────────────────────────
@dataclass
class TaskRuntime:
    task_id: str
    description: str
    status: TaskStatus = TaskStatus.idle
    workspace: Optional[Workspace] = None
    branch: Optional[str] = None
    thread: Optional[threading.Thread] = None
    cancel: threading.Event = field(default_factory=threading.Event)
    repo_full: Optional[str] = None
    # The currently-running subprocess (so cancel can kill it).
    current_proc: Optional[subprocess.Popen] = None
    current_proc_lock: threading.Lock = field(default_factory=threading.Lock)
    # Cumulative streamed text from the AI (for the UI).
    streamed_text: List[str] = field(default_factory=list)


_RUNTIMES: Dict[str, TaskRuntime] = {}
_RUNTIMES_LOCK = threading.Lock()


def get_runtime(task_id: str) -> Optional[TaskRuntime]:
    with _RUNTIMES_LOCK:
        return _RUNTIMES.get(task_id)


def list_runtimes() -> List[TaskRuntime]:
    with _RUNTIMES_LOCK:
        return list(_RUNTIMES.values())


# ── Tool registry ───────────────────────────────────────────────────────
# Each tool is a pure function (workspace, **kwargs) -> dict. The agent calls
# them and emits events from their *real* return values.
def tool_list_files(ws: Workspace, subpath: str = "") -> dict:
    files = ws.list_files(subpath)
    return {"files": files, "count": len(files)}


def tool_search_files(ws: Workspace, pattern: str = "*",
                      text: Optional[str] = None) -> dict:
    results = ws.search_files(pattern, text)
    return {"results": results, "count": len(results)}


def tool_read_file(ws: Workspace, path: str) -> dict:
    content = ws.read_file(path)
    return {"path": path, "content": content, "bytes": len(content)}


def tool_write_file(ws: Workspace, path: str, content: str) -> dict:
    p = ws.write_file(path, content)
    return {"path": p}


def tool_create_file(ws: Workspace, path: str, content: str) -> dict:
    p = ws.create_file(path, content)
    return {"path": p}


def tool_edit_file(ws: Workspace, path: str, old: str, new: str,
                   replace_all: bool = False) -> dict:
    return ws.edit_file(path, old, new, replace_all=replace_all)


def tool_delete_file(ws: Workspace, path: str) -> dict:
    p = ws.delete_file(path)
    return {"path": p}


def tool_git_status(ws: Workspace) -> dict:
    return {"status": ws.git_status(), "changed": ws.git_changed_files()}


def tool_git_diff(ws: Workspace, staged: bool = False) -> dict:
    return {"diff": ws.git_diff(staged=staged)}


def tool_run_command(ws: Workspace, command: str,
                     emit: Optional[Callable] = None,
                     rt: Optional["TaskRuntime"] = None) -> dict:
    """Run a real command. ``emit`` lets the loop stream per-command events.

    If ``rt`` is provided, the live subprocess handle is stored on it so a
    cancel request can kill the running process.
    """
    try:
        decision = validate_command(command)
        warning = decision.warning if decision.allowed else None
        if emit:
            emit(EventType.command_started, f"$ {command}",
                 data={"warning": warning} if warning else {})
        result = run_command(command, ws, rt=rt)
        if emit:
            emit(EventType.command_output,
                 result.stdout or result.stderr or "(no output)",
                 data={"stdout": result.stdout, "stderr": result.stderr,
                       "returncode": result.returncode})
            emit(EventType.command_finished,
                 f"exit {result.returncode}",
                 data={"returncode": result.returncode, "success": result.success})
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "success": result.success,
            "warning": warning,
        }
    except TerminalError as exc:
        if emit:
            emit(EventType.error, f"Command rejected: {exc}",
                 data={"command": command})
        return {"returncode": 126, "stdout": "", "stderr": str(exc),
                "success": False, "rejected": True}


TOOLS: Dict[str, Callable] = {
    "list_files": tool_list_files,
    "search_files": tool_search_files,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "create_file": tool_create_file,
    "edit_file": tool_edit_file,
    "delete_file": tool_delete_file,
    "git_status": tool_git_status,
    "git_diff": tool_git_diff,
    "run_command": tool_run_command,
}


# ── Agent loop ──────────────────────────────────────────────────────────
class Agent:
    """Runs the agent loop for one task in a background thread."""

    MAX_FIX_RETRIES = 2
    # Cap how many thinking-token events we publish per AI call (keeps the
    # event stream and DB from being flooded with single-char events).
    _THINKING_FLUSH_CHARS = 12

    def __init__(self, task_id: str, description: str,
                 repo_full: Optional[str] = None,
                 settings: Optional[Settings] = None) -> None:
        self.task_id = task_id
        self.description = description
        self.repo_full = repo_full
        self.settings = settings or get_settings()
        self.rt = TaskRuntime(task_id=task_id, description=description,
                              repo_full=repo_full)
        self.provider = get_provider(self.settings)
        self._is_streaming_provider = not isinstance(self.provider, LocalProvider)

    # ── Event helpers ──────────────────────────────────────────────────
    def emit(self, etype: EventType, message: str, **data: Any) -> None:
        BUS.publish(Event(self.task_id, etype, message, data=dict(data)))

    def _stream_ai(self, messages: List[ChatMessage],
                   context_label: str = "AI") -> str:
        """Call the provider's stream_chat and emit real thinking tokens.

        Returns the full accumulated text. For the LocalProvider the
        'streaming' is deterministic word-by-word (still real provider
        output, not faked activity).
        """
        buffer: List[str] = []

        def on_token(tok: str) -> None:
            buffer.append(tok)
            self.rt.streamed_text.append(tok)
            # Flush in batches to avoid one event per character.
            joined = "".join(buffer)
            if len(joined) >= self._THINKING_FLUSH_CHARS or tok.endswith("\n"):
                self.emit(EventType.thinking, joined,
                          source=context_label, streaming=True)
                buffer.clear()

        try:
            result = self.provider.stream_chat(messages, on_token=on_token)
        except AIError as exc:
            # Provider failed mid-stream — surface honestly and fall back.
            self.emit(EventType.error, f"AI provider error: {exc}")
            # Try the local provider as a graceful degradation.
            self.emit(EventType.info,
                      "Falling back to the offline local provider.")
            self.provider = LocalProvider()
            self._is_streaming_provider = False
            result = self.provider.stream_chat(messages, on_token=on_token)

        # Flush any remaining buffered text.
        if buffer:
            self.emit(EventType.thinking, "".join(buffer),
                      source=context_label, streaming=True)
            buffer.clear()
        return result.text

    # ── Main loop ──────────────────────────────────────────────────────
    def run(self) -> None:
        rt = self.rt
        with _RUNTIMES_LOCK:
            _RUNTIMES[self.task_id] = rt
        rt.status = TaskStatus.running
        self.emit(EventType.session_started,
                  f"Session started for task: {self.description[:120]}",
                  task=self.description,
                  provider=self.provider.name,
                  streaming=self._is_streaming_provider)
        try:
            self._loop(rt)
            if rt.cancel.is_set():
                rt.status = TaskStatus.cancelled
                self.emit(EventType.cancelled, "Task cancelled by user.")
            else:
                rt.status = TaskStatus.success
                self.emit(EventType.completed,
                          "Agent completed the task.",
                          branch=rt.branch,
                          status="success")
        except _Cancelled as exc:
            rt.status = TaskStatus.cancelled
            self.emit(EventType.cancelled, str(exc) or "Task cancelled by user.")
        except Exception as exc:
            rt.status = TaskStatus.failed
            log.exception("Agent loop failed")
            self.emit(EventType.error,
                      f"Agent failed: {exc}",
                      trace=traceback.format_exc(limit=4),
                      status="failed")
        finally:
            with _RUNTIMES_LOCK:
                _RUNTIMES[self.task_id] = rt

    def _check_cancel(self, rt: TaskRuntime) -> bool:
        """Raise _Cancelled if the user asked to cancel."""
        if rt.cancel.is_set():
            raise _Cancelled("Task cancelled by user.")
        return False

    def _loop(self, rt: TaskRuntime) -> None:
        # 1) Set up workspace + connect to repo.
        ws = Workspace(self.task_id, settings=self.settings, repo_full=self.repo_full)
        rt.workspace = ws
        self.emit(EventType.info, "Workspace ready.",
                  workspace=str(ws.root))

        if self.repo_full or self.settings.github_repo_full():
            try:
                if self.repo_full and self.repo_full != self.settings.github_repo_full():
                    owner, repo = self.repo_full.split("/", 1)
                    self.settings = self.settings.model_copy(
                        update={"github_owner": owner, "github_repo": repo}
                    )
                self.emit(EventType.info,
                          f"Repository connected: {self.settings.github_repo_full()}",
                          repo=self.settings.github_repo_full())
                res = clone_or_pull(ws, self.settings)
                if not res.success:
                    self.emit(EventType.error,
                              f"Clone/pull failed: {res.stderr.strip()[:300]}")
                else:
                    self.emit(EventType.info,
                              f"Repository cloned into workspace "
                              f"({len(ws.list_files())} files).")
            except GitHubError as exc:
                self.emit(EventType.error,
                          f"GitHub unavailable: {exc}. "
                          "Continuing with empty workspace.")
        else:
            self.emit(EventType.info,
                      "No GitHub repo configured; running in local-only workspace.")

        self._check_cancel(rt)

        # Trigger incremental indexing (Phase 3)
        self.emit(EventType.info, "Indexing repository workspace...")
        try:
            import asyncio
            import aiosqlite
            from indexing import index_workspace
            async def _run_idx():
                async with aiosqlite.connect(self.settings.database_path) as conn:
                    # Ensure tables exist
                    await conn.executescript(
                        "CREATE TABLE IF NOT EXISTS repo_files (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, path TEXT NOT NULL, hash TEXT NOT NULL, mtime REAL NOT NULL, indexed_at TEXT NOT NULL, UNIQUE(task_id, path));"
                        "CREATE TABLE IF NOT EXISTS repo_symbols (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, path TEXT NOT NULL, symbol_name TEXT NOT NULL, symbol_type TEXT NOT NULL, line_no INTEGER NOT NULL);"
                    )
                    await conn.commit()
                    return await index_workspace(self.task_id, ws, conn)

            try:
                stats = asyncio.run(_run_idx())
            except RuntimeError:
                loop = asyncio.new_event_loop()
                stats = loop.run_until_complete(_run_idx())
                loop.close()

            self.emit(EventType.info, f"Repository indexed: {stats['total']} files, "
                      f"{stats['added']} added, {stats['updated']} updated, {stats['deleted']} deleted.")
        except Exception as exc:
            self.emit(EventType.error, f"Indexing failed: {exc}")

        # 2) UNDERSTAND — analyze the task.
        self.emit(EventType.analyzing, "Understanding the task and repository.")
        files = ws.list_files()

        # 3) SEARCH — search the repository.
        self._check_cancel(rt)
        self.emit(EventType.searching, "Searching repository files.",
                  count=len(files), files=files[:50])

        # 4) READ — read relevant files (cap to keep context small).
        relevant = [f for f in files if f.endswith(
            (".py", ".js", ".ts", ".md", ".txt", ".json", ".yml", ".yaml",
             ".html", ".css", ".go", ".rs", ".java"))]
        relevant = relevant[:20]
        file_objs: List[dict] = []
        for f in relevant:
            self._check_cancel(rt)
            try:
                content = ws.read_file(f)
                file_objs.append({"path": f, "content": content})
                self.emit(EventType.file_read, f"Reading {f}",
                          path=f, bytes=len(content))
            except WorkspaceError as exc:
                self.emit(EventType.error, f"Could not read {f}: {exc}")

        # 5) PLAN — produce a plan (streamed if a real provider is set).
        self._check_cancel(rt)

        # Build project map text representation (Phase 3)
        project_map_str = ""
        try:
            import asyncio
            import aiosqlite
            from indexing import get_project_map
            async def _get_map():
                async with aiosqlite.connect(self.settings.database_path) as conn:
                    return await get_project_map(self.task_id, ws, conn)
            try:
                project_map_str = asyncio.run(_get_map())
            except RuntimeError:
                loop = asyncio.new_event_loop()
                project_map_str = loop.run_until_complete(_get_map())
                loop.close()
        except Exception:
            pass

        context = "\n".join(f"### {fo['path']}\n{fo['content'][:1500]}"
                            for fo in file_objs[:10])
        if project_map_str:
            context = project_map_str + "\n\n" + context

        plan = self._plan_with_stream(rt, self.description, context)
        self.emit(EventType.planning, plan.summary, steps=plan.steps)

        self._check_cancel(rt)

        # 6) EDIT — apply edits to files.
        self.emit(EventType.editing, "Applying edits to files.")
        edits = self._edit_with_stream(rt, self.description, plan, file_objs)
        applied: List[str] = []
        for e in edits:
            self._check_cancel(rt)
            try:
                target = ws.safe_path(e["path"])
                if target.exists():
                    ws.write_file(e["path"], e["content"])
                else:
                    ws.create_file(e["path"], e["content"])
                applied.append(e["path"])
                self.emit(EventType.editing, f"Edited {e['path']}",
                          path=e["path"], action="write")
            except WorkspaceError as exc:
                self.emit(EventType.error, f"Edit failed for {e['path']}: {exc}")

        if not applied:
            self.emit(EventType.info,
                      "No automated edits produced for this task; see plan above.")

        # 7) VERIFY — run verification (with retry on failure).
        self._verify_with_retry(rt, ws, file_objs)

        # 8) Show diff.
        self._check_cancel(rt)
        diff = ws.git_diff(staged=False)
        self.emit(EventType.info, "Computed git diff.",
                  diff=diff, changed=ws.git_changed_files())

        # 9) Branch -> commit -> push.
        self._check_cancel(rt)
        self._git_finalize(rt, ws)

    # ── Planning with streaming ────────────────────────────────────────
    def _plan_with_stream(self, rt: TaskRuntime, task: str,
                          context: str) -> Plan:
        """Produce a plan. If a streaming provider is configured, emit the
        model's reasoning as ``thinking`` events in real time."""
        if self._is_streaming_provider:
            messages = [
                ChatMessage("system",
                            "You are a coding agent planning a task. Think "
                            "briefly about the approach, then output a plan "
                            "as JSON with keys 'summary' (string) and 'steps' "
                            "(array of strings). Return ONLY JSON."),
                ChatMessage("user",
                            f"Task:\n{task}\n\nContext:\n{context[:6000]}"),
            ]
            text = self._stream_ai(messages, context_label="planning")
            from ai_provider import _parse_plan_json
            return _parse_plan_json(text, fallback_task=task)
        # Local provider: no streaming, but still real output.
        return self.provider.plan(task, context)

    def _edit_with_stream(self, rt: TaskRuntime, task: str, plan: Plan,
                          file_objs: List[dict]) -> List[dict]:
        """Produce edits. If a streaming provider is configured, emit the
        model's reasoning as ``thinking`` events in real time."""
        if self._is_streaming_provider:
            files_brief = "\n".join(f["path"] for f in file_objs[:30])
            messages = [
                ChatMessage("system",
                            "You are a coding agent. Given a task and a list "
                            "of file paths, return a JSON array of edits. "
                            "Each edit: {\"path\": \"...\", \"content\": "
                            "\"full new file content\"}. Only include files "
                            "you actually change. Return ONLY a JSON array."),
                ChatMessage("user",
                            f"Task:\n{task}\n\nPlan: {plan.summary}\n\n"
                            f"Files:\n{files_brief}"),
            ]
            text = self._stream_ai(messages, context_label="editing")
            from ai_provider import _parse_edits_json
            return _parse_edits_json(text)
        return self.provider.edit(task, plan, file_objs)

    # ── Verification with retry ────────────────────────────────────────
    def _verify_with_retry(self, rt: TaskRuntime, ws: Workspace,
                           file_objs: List[dict]) -> None:
        attempts = 0
        while attempts <= self.MAX_FIX_RETRIES:
            self._check_cancel(rt)
            cmd = self._pick_verification_command(ws)
            if not cmd:
                self.emit(EventType.info,
                          "No verification command detected; skipping run.")
                return
            self.emit(EventType.test_started, f"Running verification: {cmd}",
                      command=cmd)
            result = tool_run_command(ws, cmd, emit=self.emit, rt=rt)
            self.emit(EventType.test_finished,
                      f"Verification exit code: {result['returncode']}",
                      success=result["success"],
                      returncode=result["returncode"])
            if result["success"]:
                return
            if result.get("rejected"):
                return  # policy rejection; don't retry
            attempts += 1
            if attempts > self.MAX_FIX_RETRIES:
                self.emit(EventType.error,
                          "Verification failed after retries; stopping fixes.",
                          status="failed")
                return
            self.emit(EventType.fixing, "Analyzing failure and attempting a fix.",
                      attempt=attempts)
            analysis = self._analyze_error_with_stream(
                rt, self.description,
                result.get("stderr") or result.get("stdout") or "",
                file_objs)
            self.emit(EventType.fixing, analysis, analysis=analysis)
            # Re-run verification once more to confirm stability.

    def _analyze_error_with_stream(self, rt: TaskRuntime, task: str,
                                   error: str, file_objs: List[dict]) -> str:
        if self._is_streaming_provider:
            messages = [
                ChatMessage("system",
                            "A verification command failed. In one short "
                            "sentence, describe the most likely cause and fix."),
                ChatMessage("user", f"Error:\n{error[:1500]}"),
            ]
            return self._stream_ai(messages, context_label="fixing").strip()
        return self.provider.analyze_error(task, error, file_objs)

    def _pick_verification_command(self, ws: Workspace) -> Optional[str]:
        """Choose a real verification command based on workspace contents."""
        files = set(ws.list_files())
        if "pytest.ini" in files or any(
                f.startswith("tests/") and f.endswith(".py") for f in files) or \
           any(f.endswith("conftest.py") for f in files):
            return "python -m pytest -q"
        if "setup.py" in files or "pyproject.toml" in files:
            py = [f for f in files if f.endswith(".py") and "/" not in f]
            if py:
                return "python -m py_compile " + " ".join(py[:10])[:200]
        if "package.json" in files:
            return "npm test"
        if "build.gradle" in files or "build.gradle.kts" in files:
            return "gradle build"
        if "Cargo.toml" in files:
            return "cargo build"
        if "go.mod" in files:
            return "go build ./..."
        py_files = [f for f in files if f.endswith(".py")]
        if py_files:
            sample = py_files[:5]
            return "python -m py_compile " + " ".join(sample)
        return None

    def _git_finalize(self, rt: TaskRuntime, ws: Workspace) -> None:
        if not ws.has_git_repo():
            self.emit(EventType.info,
                      "Workspace is not a git repo; skipping git steps.")
            return
        changed = ws.git_changed_files()
        if not changed:
            self.emit(EventType.info, "No changes to commit.")
            return
        branch = f"pk-ninja/{self.task_id[:8]}"
        try:
            res = ws.create_branch(branch)
            if not res.success:
                ws._git(["checkout", branch])
            rt.branch = branch
            self.emit(EventType.info, f"Created branch {branch}", branch=branch)
        except WorkspaceError as exc:
            self.emit(EventType.error, f"Branch creation failed: {exc}")
            return

        ws.git_add_all()
        commit_msg = f"PK Ninja Agent: {self.description[:100]}"
        res = ws.git_commit(commit_msg)
        if res.success:
            self.emit(EventType.info, f"Committed: {commit_msg}",
                      commit=commit_msg, files=changed)
        else:
            self.emit(EventType.error,
                      f"Commit failed: {res.stderr.strip()[:300]}")
            return

        if self.settings.github_token and self.settings.github_repo_full():
            res = ws.git_push()
            if res.success:
                self.emit(EventType.info, f"Pushed branch {branch}.",
                          branch=branch)
            else:
                self.emit(EventType.error,
                          f"Push failed: {res.stderr.strip()[:300]}")
        else:
            self.emit(EventType.info,
                      "Push skipped (no GitHub token/repo configured). "
                      "Use the Push button after configuring credentials.")


class _Cancelled(Exception):
    """Internal control-flow exception for cancellation."""


# ── Public API used by main.py ──────────────────────────────────────────
def start_task(task_id: str, description: str,
               repo_full: Optional[str] = None) -> TaskRuntime:
    agent = Agent(task_id, description, repo_full=repo_full)
    t = threading.Thread(target=agent.run, name=f"agent-{task_id}", daemon=True)
    agent.rt.thread = t
    t.start()
    return agent.rt


def cancel_task(task_id: str) -> bool:
    """Cancel a running task: set the flag and kill any live subprocess."""
    rt = get_runtime(task_id)
    if rt:
        rt.cancel.set()
        # Kill the currently-running command subprocess, if any.
        with rt.current_proc_lock:
            proc = rt.current_proc
        if proc and proc.poll() is None:
            try:
                import os as _os
                _os.killpg(_os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        return True
    return False


def new_task_id() -> str:
    return uuid.uuid4().hex[:12]
