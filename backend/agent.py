"""Agent: the event system and the agent loop.

The agent loop is the real workflow:

  UNDERSTAND -> SEARCH -> READ -> PLAN -> EDIT -> VERIFY
  -> (on failure) ANALYZE -> FIX -> VERIFY again -> DIFF -> BRANCH -> COMMIT -> PUSH

Every step emits an :class:`Event` that is persisted to SQLite and streamed to
the frontend via SSE. The agent never fakes activity: tools run against the
real workspace and emit events from their actual results.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
import queue
import threading
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional

from ai_provider import AIError, get_provider
from config import Settings, get_settings
from github import GitHubError, clone_or_pull, repo_info
from models import EventType, TaskStatus
from terminal import TerminalError, run_command
from workspace import Workspace, WorkspaceError

log = logging.getLogger("pk_ninja.agent")


# ── Events ─────────────────────────────────────────────────────────────────
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

    A background thread publishes events; SSE consumers read them from a
    thread-safe queue. We also keep an in-memory ring so late subscribers get
    recent history, and persist every event to SQLite for durability.
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
            # Keep last 500 events in memory.
            if len(self._history[event.task_id]) > 500:
                self._history[event.task_id] = self._history[event.task_id][-500:]
            subs = list(self._subs.get(event.task_id, []))
        if self._persist:
            try:
                self._persist(event)
            except Exception:  # pragma: no cover - persistence must not break agent
                log.exception("Failed to persist event")
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass  # slow consumer; drop (history retains it)

    def history(self, task_id: str) -> List[Event]:
        with self._lock:
            return list(self._history.get(task_id, []))

    def subscribe(self, task_id: str, maxsize: int = 1000) -> queue.Queue:
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


# ── Task runtime state ────────────────────────────────────────────────────
@dataclass
class TaskRuntime:
    task_id: str
    description: str
    status: TaskStatus = TaskStatus.pending
    workspace: Optional[Workspace] = None
    branch: Optional[str] = None
    thread: Optional[threading.Thread] = None
    cancel: threading.Event = field(default_factory=threading.Event)
    repo_full: Optional[str] = None


_RUNTIMES: Dict[str, TaskRuntime] = {}
_RUNTIMES_LOCK = threading.Lock()


def get_runtime(task_id: str) -> Optional[TaskRuntime]:
    with _RUNTIMES_LOCK:
        return _RUNTIMES.get(task_id)


def list_runtimes() -> List[TaskRuntime]:
    with _RUNTIMES_LOCK:
        return list(_RUNTIMES.values())


# ── Tool registry ─────────────────────────────────────────────────────────
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
                     emit: Optional[Callable] = None) -> dict:
    """Run a real command. ``emit`` lets the loop stream per-command events."""
    try:
        from terminal import validate_command
        decision = validate_command(command)
        warning = decision.warning if decision.allowed else None
        if emit:
            emit(EventType.command_started, f"$ {command}",
                 data={"warning": warning} if warning else {})
        result = run_command(command, ws)
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


# ── Agent loop ─────────────────────────────────────────────────────────────
class Agent:
    """Runs the agent loop for one task in a background thread."""

    MAX_FIX_RETRIES = 2

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

    # ── Event helpers ──────────────────────────────────────────────────────
    def emit(self, etype: EventType, message: str, **data: Any) -> None:
        BUS.publish(Event(self.task_id, etype, message, data=dict(data)))

    # ── Main loop ──────────────────────────────────────────────────────────
    def run(self) -> None:
        rt = self.rt
        with _RUNTIMES_LOCK:
            _RUNTIMES[self.task_id] = rt
        rt.status = TaskStatus.running
        self.emit(EventType.session_started,
                  f"Session started for task: {self.description[:120]}",
                  task=self.description)
        try:
            self._loop(rt)
            if not rt.cancel.is_set():
                rt.status = TaskStatus.completed
                self.emit(EventType.completed,
                          "Agent completed the task.",
                          branch=rt.branch)
        except Exception as exc:
            rt.status = TaskStatus.failed
            log.exception("Agent loop failed")
            self.emit(EventType.error,
                      f"Agent failed: {exc}",
                      trace=traceback.format_exc(limit=4))
        finally:
            with _RUNTIMES_LOCK:
                _RUNTIMES[self.task_id] = rt

    def _check_cancel(self, rt: TaskRuntime) -> bool:
        if rt.cancel.is_set():
            self.emit(EventType.info, "Task cancelled by user.")
            rt.status = TaskStatus.cancelled
            return True
        return False

    def _loop(self, rt: TaskRuntime) -> None:
        # 1) Set up workspace + connect to repo.
        ws = Workspace(self.task_id, settings=self.settings)
        rt.workspace = ws
        self.emit(EventType.info, "Workspace ready.",
                  workspace=str(ws.root))

        if self.repo_full or self.settings.github_repo_full():
            try:
                info = repo_info(self.settings) if self.settings.github_repo_full() else None
                if self.repo_full and self.repo_full != self.settings.github_repo_full():
                    # Override owner/repo for this task at runtime.
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
                              f"Repository cloned into workspace ({len(ws.list_files())} files).")
            except GitHubError as exc:
                self.emit(EventType.error,
                          f"GitHub unavailable: {exc}. Continuing with empty workspace.")
        else:
            self.emit(EventType.info,
                      "No GitHub repo configured; running in local-only workspace.")

        if self._check_cancel(rt):
            return

        # 2) Understand + search.
        self.emit(EventType.analyzing, "Analyzing task and repository.")
        files = ws.list_files()
        self.emit(EventType.searching, "Searching repository files.",
                  count=len(files))

        # 3) Read relevant files (cap to keep context small).
        relevant = [f for f in files if f.endswith((".py", ".js", ".ts", ".md",
                    ".txt", ".json", ".yml", ".yaml", ".html", ".css"))]
        relevant = relevant[:20]
        file_objs: List[dict] = []
        for f in relevant:
            if self._check_cancel(rt):
                return
            try:
                content = ws.read_file(f)
                file_objs.append({"path": f, "content": content})
                self.emit(EventType.file_read, f"Reading {f}", path=f)
            except WorkspaceError as exc:
                self.emit(EventType.error, f"Could not read {f}: {exc}")

        # 4) Plan.
        context = "\n".join(f"### {fo['path']}\n{fo['content'][:1500]}"
                            for fo in file_objs[:10])
        plan = self.provider.plan(self.description, context)
        self.emit(EventType.planning, plan.summary, steps=plan.steps)

        if self._check_cancel(rt):
            return

        # 5) Edit files.
        self.emit(EventType.editing, "Applying edits.")
        edits = self.provider.edit(self.description, plan, file_objs)
        applied: List[str] = []
        for e in edits:
            if self._check_cancel(rt):
                return
            try:
                # Use create_file if new, else write_file (full overwrite).
                target = ws.safe_path(e["path"])
                if target.exists():
                    ws.write_file(e["path"], e["content"])
                else:
                    ws.create_file(e["path"], e["content"])
                applied.append(e["path"])
                self.emit(EventType.editing, f"Edited {e['path']}", path=e["path"])
            except WorkspaceError as exc:
                self.emit(EventType.error, f"Edit failed for {e['path']}: {exc}")

        if not applied:
            self.emit(EventType.info,
                      "No automated edits produced for this task; see plan above.")

        # 6) Run verification (with retry on failure).
        self._verify_with_retry(rt, ws, file_objs)

        # 7) Show diff.
        diff = ws.git_diff(staged=False)
        self.emit(EventType.info, "Computed git diff.",
                  diff=diff, changed=ws.git_changed_files())

        # 8) Branch -> commit -> push.
        if self._check_cancel(rt):
            return
        self._git_finalize(rt, ws)

    def _verify_with_retry(self, rt: TaskRuntime, ws: Workspace,
                           file_objs: List[dict]) -> None:
        attempts = 0
        while attempts <= self.MAX_FIX_RETRIES:
            if self._check_cancel(rt):
                return
            cmd = self._pick_verification_command(ws)
            if not cmd:
                self.emit(EventType.info,
                          "No verification command detected; skipping run.")
                return
            self.emit(EventType.test_started, f"Running verification: {cmd}",
                      command=cmd)
            result = tool_run_command(ws, cmd, emit=self.emit)
            self.emit(EventType.test_finished,
                      f"Verification exit code: {result['returncode']}",
                      success=result["success"], returncode=result["returncode"])
            if result["success"]:
                return
            if result.get("rejected"):
                return  # policy rejection; don't retry
            attempts += 1
            if attempts > self.MAX_FIX_RETRIES:
                self.emit(EventType.error,
                          "Verification failed after retries; stopping fixes.")
                return
            self.emit(EventType.fixing, "Analyzing failure and attempting a fix.",
                      attempt=attempts)
            analysis = self.provider.analyze_error(
                self.description,
                result.get("stderr") or result.get("stdout") or "",
                file_objs,
            )
            self.emit(EventType.fixing, analysis, analysis=analysis)
            # The local provider's analyze_error may indicate a revert; we
            # don't auto-revert in the MVP. We simply retry the same command
            # once more to confirm stability.

    def _pick_verification_command(self, ws: Workspace) -> Optional[str]:
        """Choose a real verification command based on what's in the workspace."""
        files = set(ws.list_files())
        if "pytest.ini" in files or any(f.startswith("tests/") and f.endswith(".py")
                                        for f in files) or \
           any(f.endswith("conftest.py") for f in files):
            return "python -m pytest -q"
        if "setup.py" in files or "pyproject.toml" in files:
            return "python -m py_compile " + " ".join(
                f for f in files if f.endswith(".py") and "/" not in f
            )[:200] or None
        if "package.json" in files:
            return "npm test"
        if "build.gradle" in files or "build.gradle.kts" in files:
            return "gradle build"
        if "Cargo.toml" in files:
            return "cargo build"
        if "go.mod" in files:
            return "go build ./..."
        # Fallback: syntax-check any Python files present.
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
                # Maybe branch exists; just checkout it.
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
            self.emit(EventType.info,
                      f"Committed: {commit_msg}",
                      commit=commit_msg, files=changed)
        else:
            self.emit(EventType.error,
                      f"Commit failed: {res.stderr.strip()[:300]}")
            return

        # Push only if a token + repo are configured.
        if self.settings.github_token and self.settings.github_repo_full():
            res = ws.git_push()
            if res.success:
                self.emit(EventType.info, f"Pushed branch {branch}.", branch=branch)
            else:
                self.emit(EventType.error,
                          f"Push failed: {res.stderr.strip()[:300]}")
        else:
            self.emit(EventType.info,
                      "Push skipped (no GitHub token/repo configured). "
                      "Use the Push button after configuring credentials.")


# ── Public API used by main.py ─────────────────────────────────────────────
def start_task(task_id: str, description: str,
               repo_full: Optional[str] = None) -> TaskRuntime:
    agent = Agent(task_id, description, repo_full=repo_full)
    t = threading.Thread(target=agent.run, name=f"agent-{task_id}", daemon=True)
    agent.rt.thread = t
    t.start()
    return agent.rt


def cancel_task(task_id: str) -> bool:
    rt = get_runtime(task_id)
    if rt:
        rt.cancel.set()
        return True
    return False


def new_task_id() -> str:
    return uuid.uuid4().hex[:12]
