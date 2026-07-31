"""Terminal: real, controlled command execution inside a workspace.

Security model:
  * Commands run with ``cwd`` set to the workspace root.
  * The first token (the program) is validated against an allowlist.
  * An explicit blocklist catches destructive/dangerous patterns regardless
    of program (e.g. ``rm -rf /``, ``dd if=`` of devices, ``:(){...}`` forks).
  * A hard timeout kills the process tree.
  * stdout, stderr, and exit code are captured truthfully — no faked output.
"""
from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

from workspace import CommandResult, Workspace, WorkspaceError


# Programs the agent is allowed to invoke. Add cautiously.
ALLOWED_PROGRAMS = {
    "ls", "cat", "head", "tail", "grep", "rg", "find", "wc", "sort", "uniq",
    "echo", "printf", "pwd", "env", "which", "file", "tree", "stat",
    "python", "python3", "pip", "pip3", "pytest", "coverage",
    "node", "npm", "npx", "yarn", "pnpm", "tsc",
    "git", "gh",
    "mkdir", "touch", "cp", "mv", "rm",
    "diff", "sed", "awk", "tr", "cut", "xargs",
    "gradle", "mvn", "cargo", "go", "rustc", "gcc", "make", "cmake",
    "java", "javac", "javap",
    "dotnet",
    "test", "true", "false",
}

# Patterns that are blocked outright even if the program is allowed.
BLOCKED_PATTERNS = [
    re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+/(\s|$)"),  # rm -rf /
    re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+/\*"),      # rm -rf /*
    re.compile(r":\(\)\s*\{"),                                # fork bomb
    re.compile(r"\bdd\b.*\bof=/dev/"),                        # dd to device
    re.compile(r">\s*/dev/sd"),                               # write to disk
    re.compile(r"\bmkfs\b"),                                  # format fs
    re.compile(r"\bshutdown\b"), re.compile(r"\breboot\b"),
    re.compile(r"\bhalt\b"), re.compile(r"\bpoweroff\b"),
    re.compile(r"\bchmod\b.*\b777\b\s*/"),                    # chmod 777 /
    re.compile(r"\bcurl\b.*\|\s*(bash|sh)\b"),                # pipe to shell
    re.compile(r"\bwget\b.*\|\s*(bash|sh)\b"),
    re.compile(r"\beval\b"),                                  # eval is too loose
    re.compile(r"\bexec\b\s+"),                               # exec replaces shell
]

# Commands that can modify or delete data — surfaced as warnings to the UI.
WARN_PATTERNS = [
    re.compile(r"\brm\b"),
    re.compile(r"\bmv\b"),
    re.compile(r"\bgit\s+(reset|clean|push|checkout|rebase)\b"),
    re.compile(r"\bpip\s+install\b"),
    re.compile(r"\bnpm\s+install\b"),
    re.compile(r"\bchmod\b"),
    re.compile(r"\bchown\b"),
]


@dataclass
class TerminalDecision:
    allowed: bool
    reason: str
    warning: Optional[str] = None
    program: str = ""
    args: List[str] = field(default_factory=list)


class TerminalError(Exception):
    pass


def _strip_env_prefix(command: str) -> str:
    """Strip leading ``VAR=val`` assignments so we can identify the program."""
    tokens = shlex.split(command, posix=True)
    i = 0
    while i < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i]):
        i += 1
    if i >= len(tokens):
        return ""
    return " ".join(tokens[i:])


# Absolute paths the agent may legitimately reference even though they are
# outside the workspace — e.g. the Python/Node interpreter, the git binary,
# /dev/null for redirection. These are allowlisted so the containment check
# does not break normal tooling.
_ALLOWED_EXTERNAL_ABS = {
    "/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr",
    "/dev/zero", "/dev/urandom", "/dev/full",
}


def _unsafe_path_refs(command: str) -> List[str]:
    """Return absolute-path / ``..`` references that escape the workspace.

    Detects any token that is an absolute path (starts with ``/``) other than
    a small allowlist of device/interpreter paths, or that contains a
    ``..`` parent traversal. Such references could read or write files
    outside the sandboxed workspace, so they are blocked.
    """
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        # Unparseable — let the later parser report the error.
        return []
    unsafe = []
    for tok in tokens:
        # Skip env-prefix assignments (VAR=val).
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            continue
        # Absolute path reference.
        if tok.startswith("/"):
            if tok in _ALLOWED_EXTERNAL_ABS:
                continue
            # Allow common interpreter binaries invoked by absolute path.
            if tok in ("/usr/bin/python", "/usr/bin/python3",
                       "/usr/bin/node", "/usr/bin/git", "/usr/bin/gh",
                       "/usr/local/bin/python", "/usr/local/bin/python3",
                       "/usr/local/bin/node", "/usr/local/bin/git"):
                continue
            unsafe.append(tok)
            continue
        # Parent traversal anywhere in the token.
        if ".." in tok and ("/../" in tok or tok.startswith("../")
                            or tok.endswith("/..") or tok == ".."):
            unsafe.append(tok)
    return unsafe


def _has_unquoted_shell_operator(command: str) -> bool:
    """True if a shell operator appears outside of single/double quotes.

    This lets `python -c 'import time; time.sleep(1)'` through (the `;` is
    inside single quotes) while still blocking `echo a && echo b`.
    """
    operators = ("&&", "||", "|", ";", "&", "`", "$(")
    i = 0
    n = len(command)
    quote = None
    while i < n:
        ch = command[i]
        # Toggle quote state.
        if quote is None and ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if quote is not None:
            if ch == "\\" and quote == '"':  # escape only in double quotes
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        # Outside quotes: look for operators.
        for op in operators:
            if command.startswith(op, i):
                # Allow a single '&' only if not part of '&&' and not trailing
                # background — but we block all bare '&' in MVP anyway.
                return True
        i += 1
    return False


def validate_command(command: str) -> TerminalDecision:
    """Decide whether ``command`` may run. Returns a TerminalDecision."""
    command = (command or "").strip()
    if not command:
        return TerminalDecision(False, "Empty command.")

    # Reject shell control operators that change semantics / chain danger —
    # but only when they appear OUTSIDE of quotes (so `python -c "a; b"` is
    # allowed because the `;` is inside a quoted argument).
    if _has_unquoted_shell_operator(command):
        return TerminalDecision(
            False,
            "Shell operators (|, &&, ||, ;, &, $(), backticks) outside quotes "
            "are not allowed in the MVP. Run a single command at a time.",
        )

    # Blocklist scan on the raw command.
    for pat in BLOCKED_PATTERNS:
        if pat.search(command):
            return TerminalDecision(
                False, f"Blocked by safety pattern: {pat.pattern!r}."
            )

    # Sandbox containment: reject absolute paths (outside an allowlist) and
    # parent-directory traversals so commands cannot read or write files
    # outside the workspace (e.g. `cat /etc/passwd`, `cat ../../secret`).
    unsafe = _unsafe_path_refs(command)
    if unsafe:
        return TerminalDecision(
            False,
            "Command references paths outside the workspace sandbox: "
            + ", ".join(unsafe[:3])
            + ". Only workspace-relative paths are allowed.",
        )

    # Identify the program (after any env-prefix assignments).
    body = _strip_env_prefix(command)
    if not body:
        return TerminalDecision(False, "No program found in command.")
    try:
        tokens = shlex.split(body, posix=True)
    except ValueError as exc:
        return TerminalDecision(False, f"Could not parse command: {exc}")
    if not tokens:
        return TerminalDecision(False, "No program found in command.")

    program = os.path.basename(tokens[0])
    if program not in ALLOWED_PROGRAMS:
        return TerminalDecision(
            False,
            f"Program {program!r} is not in the allowlist. "
            f"Allowed: {sorted(ALLOWED_PROGRAMS)[:12]}…",
            program=program,
        )

    warning = None
    for pat in WARN_PATTERNS:
        if pat.search(command):
            warning = (
                "This command can modify or delete data. Review before trusting."
            )
            break

    return TerminalDecision(
        True, "ok", warning=warning, program=program, args=tokens[1:]
    )


def run_command(command: str, workspace: Workspace,
                timeout: Optional[int] = None,
                rt: Optional["object"] = None) -> CommandResult:
    """Execute ``command`` inside ``workspace`` and return the real result.

    Raises ``TerminalError`` for policy violations. Captures stdout, stderr,
    and exit code truthfully. On timeout, kills the process tree.

    If ``rt`` (a TaskRuntime) is provided, the live ``subprocess.Popen`` handle
    is stored on it (under ``current_proc``) so a cancel request can kill the
    running command. This is set/cleared under ``rt.current_proc_lock``.
    """
    decision = validate_command(command)
    if not decision.allowed:
        raise TerminalError(decision.reason)

    ws_root = str(workspace.root)
    if timeout is None:
        timeout = workspace.settings.command_timeout_seconds

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["PWD"] = ws_root

    proc: Optional[subprocess.Popen] = None
    # Register the process on the runtime so cancel can kill it.
    if rt is not None:
        try:
            with rt.current_proc_lock:
                rt.current_proc = None  # placeholder; set below once spawned
        except Exception:
            pass

    try:
        proc = subprocess.Popen(
            command,
            cwd=ws_root,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
        if rt is not None:
            with rt.current_proc_lock:
                rt.current_proc = proc

        import threading
        import time

        stdout_buf: List[str] = []
        stderr_buf: List[str] = []

        def read_stdout():
            for line in iter(proc.stdout.readline, ""):
                if line:
                    stdout_buf.append(line)
                    try:
                        from agent import BUS, Event
                        from models import EventType
                        BUS.publish(Event(workspace.task_id, EventType.command_output, line, data={"stdout": line}))
                    except Exception:
                        pass

        def read_stderr():
            for line in iter(proc.stderr.readline, ""):
                if line:
                    stderr_buf.append(line)
                    try:
                        from agent import BUS, Event
                        from models import EventType
                        BUS.publish(Event(workspace.task_id, EventType.command_output, line, data={"stderr": line}))
                    except Exception:
                        pass

        t1 = threading.Thread(target=read_stdout, daemon=True)
        t2 = threading.Thread(target=read_stderr, daemon=True)
        t1.start()
        t2.start()

        start_time = time.time()
        timed_out = False
        while proc.poll() is None:
            if time.time() - start_time > timeout:
                timed_out = True
                break
            time.sleep(0.05)

        if timed_out:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            returncode = 124
            stdout = "".join(stdout_buf)
            stderr = "".join(stderr_buf)
            if not stderr:
                stderr = f"Command timed out after {timeout}s and was killed."
        else:
            t1.join(timeout=2)
            t2.join(timeout=2)
            returncode = proc.returncode
            stdout = "".join(stdout_buf)
            stderr = "".join(stderr_buf)

    except Exception as exc:  # pragma: no cover - defensive
        returncode = 127
        stdout, stderr = "", f"Failed to start command: {exc}"
    finally:
        if rt is not None:
            with rt.current_proc_lock:
                rt.current_proc = None

    # If the task was cancelled mid-run, record it truthfully.
    cancelled = False
    if rt is not None and getattr(rt, "cancel", None) and rt.cancel.is_set():
        cancelled = True
        if not stderr:
            stderr = "Command terminated because the task was cancelled."

    return CommandResult(
        command=command,
        returncode=returncode,
        stdout=stdout or "",
        stderr=stderr or "",
    )


def kill_process_tree(pid: int) -> None:  # pragma: no cover - helper
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
