"""Security hardening utilities for PK Ninja Agent (v0.8.0).

This module centralises the v0.8.0 security improvements so they can be
reused by the terminal, workspace manager, and API layer without duplicating
logic.  It is deliberately additive — the existing ``terminal.validate_command``
and ``workspace.safe_path`` remain the primary guards; the helpers here
*supplement* them with deeper checks.

Three areas of hardening:

1. **Workspace validation** — ``validate_workspace`` walks a workspace
   directory and verifies that no symlink escapes the sandbox root, that
   the root itself lives inside the configured ``workspace_root``, and that
   no world-writable permissions are present on directories (which would
   allow other local users to inject files).

2. **Command argument containment** — ``check_destructive_args`` inspects
   the *arguments* of destructive commands (``rm``, ``mv``, ``cp``) and
   rejects attempts to target the workspace root itself (``rm -rf .``) or
   paths that resolve outside the workspace via ``..``.

3. **Sensitive-file protection** — ``SENSITIVE_PATTERNS`` lists filenames
   whose contents must never be read or exported by the agent (API keys,
   SSH private keys, ``.env`` files, etc.).  ``is_sensitive_path`` is a
   fast predicate used by the history/export layers to redact or block.

All functions are pure (no I/O side effects except ``validate_workspace``
which only *reads* the filesystem) and raise ``SecurityError`` on violations.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set, Tuple

try:  # pragma: no cover - import shim
    from workspace import WorkspaceError
except ImportError:  # pragma: no cover
    class WorkspaceError(Exception):
        pass


class SecurityError(Exception):
    """Raised when a security check fails."""


# ── Sensitive-file protection ─────────────────────────────────────────────

# Filenames / patterns whose contents must never be read or exported.
SENSITIVE_PATTERNS: List[str] = [
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    ".env.staging",
    ".env.test",
    ".env.example",  # examples are fine to read but we flag for awareness
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".pgpass",
    ".htpasswd",
    "credentials.json",
    "service_account.json",
    "client_secret.json",
    "google-services.json",
    "GoogleService-Info.plist",
    "keystore.jks",
    "keystore.keystore",
]

# Substrings that, if found in a filename, mark it as likely sensitive.
SENSITIVE_NAME_SUBSTRINGS: List[str] = [
    "private_key",
    "privatekey",
    "secret_key",
    "secretkey",
    "api_key",
    "apikey",
    "access_key",
    "accesskey",
]


def is_sensitive_path(rel_path: str) -> bool:
    """Return ``True`` if *rel_path* looks like a sensitive file.

    Checks both exact filename matches (basename) and filename substrings.
    The path is normalised to forward slashes and leading slashes stripped
    before the basename is examined.
    """
    if not rel_path:
        return False
    norm = str(rel_path).replace("\\", "/").lstrip("/")
    basename = norm.rsplit("/", 1)[-1]
    if not basename:
        return False
    low = basename.lower()
    for pat in SENSITIVE_PATTERNS:
        if low == pat.lower():
            return True
    for sub in SENSITIVE_NAME_SUBSTRINGS:
        if sub in low:
            return True
    # ``.pem`` / ``.key`` / ``.p12`` / ``.pfx`` certificate/key extensions.
    for ext in (".pem", ".key", ".p12", ".pfx", ".kdbx"):
        if low.endswith(ext):
            return True
    return False


# ── Workspace validation ──────────────────────────────────────────────────


@dataclass
class WorkspaceValidationResult:
    """Outcome of ``validate_workspace``."""
    valid: bool
    root: str
    issues: List[str] = field(default_factory=list)
    checked_files: int = 0
    checked_dirs: int = 0
    symlinks: int = 0


def _resolve_root(workspace_root: str) -> Path:
    if not workspace_root:
        raise SecurityError("workspace_root is not configured.")
    root = Path(workspace_root).resolve()
    return root


def validate_workspace(
    ws_dir: Path,
    *,
    workspace_root: str,
    max_files: int = 200_000,
) -> WorkspaceValidationResult:
    """Validate that *ws_dir* is a safe, contained workspace.

    Checks performed:
    * The workspace directory resolves to a path inside *workspace_root*.
    * No symlink inside the workspace points outside the workspace root
      (this would let the agent read/write host files via a link).
    * No directory is world-writable (``stat.S_IWOTH``) — other local users
      could inject or modify files.
    * The file count does not exceed *max_files* (defence against
      pathological / zip-bomb workspaces).

    Returns a ``WorkspaceValidationResult``.  If ``valid`` is ``False`` the
    ``issues`` list contains human-readable explanations.  Hard violations
    that make the workspace unusable raise ``SecurityError``.
    """
    issues: List[str] = []
    root = _resolve_root(workspace_root)

    ws_resolved = Path(ws_dir).resolve()

    # Containment: workspace must be inside the configured root.
    try:
        ws_resolved.relative_to(root)
    except ValueError:
        raise SecurityError(
            f"Workspace {ws_dir} resolves outside workspace_root {root}."
        )

    if not ws_resolved.exists():
        raise SecurityError(f"Workspace {ws_dir} does not exist.")

    checked_files = 0
    checked_dirs = 0
    symlinks = 0

    for dirpath, dirnames, filenames in os.walk(
        ws_resolved, followlinks=False
    ):
        cur = Path(dirpath)

        # Skip .git to avoid walking a potentially huge / external-linked repo.
        if ".git" in dirnames:
            dirnames.remove(".git")
        if "__pycache__" in dirnames:
            dirnames.remove("__pycache__")

        checked_dirs += 1

        # World-writable directory check.
        try:
            st = cur.stat()
            if st.st_mode & 0o002:  # S_IWOTH
                issues.append(
                    f"World-writable directory: {cur} (mode {oct(st.st_mode & 0o777)})"
                )
        except OSError:
            pass

        # Symlink directory check — symlinks must not escape the workspace.
        for dname in list(dirnames):
            dpath = cur / dname
            if dpath.is_symlink():
                symlinks += 1
                target = dpath.resolve()
                try:
                    target.relative_to(ws_resolved)
                except ValueError:
                    issues.append(
                        f"Symlink escapes workspace: {dpath} -> {target}"
                    )
                    # Do not descend into escaping symlinks.
                    dirnames.remove(dname)

        for fname in filenames:
            checked_files += 1
            if checked_files > max_files:
                issues.append(
                    f"File count exceeds limit ({max_files}); scan stopped."
                )
                return WorkspaceValidationResult(
                    valid=False,
                    root=str(ws_resolved),
                    issues=issues,
                    checked_files=checked_files,
                    checked_dirs=checked_dirs,
                    symlinks=symlinks,
                )
            fpath = cur / fname
            if fpath.is_symlink():
                symlinks += 1
                target = fpath.resolve()
                try:
                    target.relative_to(ws_resolved)
                except ValueError:
                    issues.append(
                        f"Symlink escapes workspace: {fpath} -> {target}"
                    )

    valid = not any(
        "escapes workspace" in i or "World-writable" in i
        for i in issues
    )
    return WorkspaceValidationResult(
        valid=valid,
        root=str(ws_resolved),
        issues=issues,
        checked_files=checked_files,
        checked_dirs=checked_dirs,
        symlinks=symlinks,
    )


# ── Destructive-argument containment ──────────────────────────────────────

# Commands whose arguments are subject to extra path-containment checks.
DESTRUCTIVE_PROGRAMS: Set[str] = {"rm", "mv", "cp", "rmdir"}

# Patterns that are *always* blocked for destructive commands, regardless
# of the workspace context.  These are matched against the *arguments* tail
# (flags + path args, without the program name).
_RM_ROOT_PATTERNS = [
    re.compile(r"(^|\s)\.(\s|$)"),          # . as a standalone arg
    re.compile(r"(^|\s)\*(\s|$)"),          # * as a standalone arg
    re.compile(r"(^|\s)\*\s*/"),            # * /
    re.compile(r"/\*$"),                    # /* at end
    re.compile(r"\*\*"),                    # ** glob
]


@dataclass
class ArgCheckResult:
    """Result of ``check_destructive_args``."""
    allowed: bool
    reason: str = ""
    unsafe_args: List[str] = field(default_factory=list)


def check_destructive_args(
    program: str,
    args: List[str],
    *,
    workspace_root: Optional[Path] = None,
) -> ArgCheckResult:
    """Inspect arguments of destructive commands for unsafe targets.

    * ``rm`` with recursive flags targeting ``.`` or ``*`` is blocked
      (would delete the entire workspace).
    * Any argument that contains ``..`` parent traversal is blocked
      (could escape the workspace).
    * Absolute paths (starting with ``/``) are blocked unless they are in
      the allowlisted set of device paths.

    Returns an ``ArgCheckResult`` with ``allowed=False`` and a reason when
    a violation is detected.
    """
    if program not in DESTRUCTIVE_PROGRAMS:
        return ArgCheckResult(allowed=True)

    # Flag-only args (``-rf``, ``-v``, ``--recursive``) are not path targets.
    path_args = [a for a in args if not a.startswith("-")]

    # rm-specific root-deletion patterns (operate on the reconstructed
    # command tail so flag ordering does not matter).
    if program == "rm":
        tail = " ".join(args)
        for pat in _RM_ROOT_PATTERNS:
            if pat.search(tail):
                return ArgCheckResult(
                    allowed=False,
                    reason=(
                        f"Destructive pattern blocked: {pat.pattern!r}. "
                        "Refusing to delete the workspace root or all files."
                    ),
                    unsafe_args=[a for a in path_args if a in (".", "*", "**")],
                )

    unsafe: List[str] = []
    for arg in path_args:
        # Parent traversal.
        if ".." in arg.split("/"):
            unsafe.append(arg)
            continue
        # Absolute path (outside allowlisted devices).
        if arg.startswith("/"):
            if arg.rstrip("/") not in {
                "/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr",
                "/dev/zero", "/dev/urandom",
            }:
                unsafe.append(arg)
                continue

    if unsafe:
        return ArgCheckResult(
            allowed=False,
            reason=(
                "Destructive command targets paths outside the workspace: "
                + ", ".join(unsafe[:5])
                + ". Only workspace-relative paths are allowed."
            ),
            unsafe_args=unsafe,
        )

    return ArgCheckResult(allowed=True)


# ── Enhanced blocklist patterns ───────────────────────────────────────────

# Additional dangerous patterns to supplement terminal.BLOCKED_PATTERNS.
# These are checked on the raw command string before the program allowlist.
EXTRA_BLOCKED_PATTERNS: List[re.Pattern] = [
    re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+~"),       # rm -rf ~ (home)
    re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+\$HOME"),   # rm -rf $HOME
    re.compile(r"\bchmod\s+-R\s+777\b"),                     # chmod -R 777
    re.compile(r"\bchown\s+-R\b"),                           # chown -R
    re.compile(r"\bcat\b.*\s/etc/shadow\b"),                 # read shadow
    re.compile(r"\bcat\b.*\s/etc/sudoers\b"),                # read sudoers
    re.compile(r">\s*/etc/"),                                # write to /etc
    re.compile(r">\s*~/.ssh/"),                              # write to ~/.ssh
    re.compile(r"\bhistory\s+-c\b"),                         # clear history
    re.compile(r"\bexport\b.*(TOKEN|KEY|SECRET|PASSWORD)=", re.I),  # leak via export
    re.compile(r"\becho\b.*>>\s*~/.ssh/authorized_keys"),    # ssh backdoor
    re.compile(r"\bnc\b.*-\bl\b"),                           # netcat listener
    re.compile(r"\bcrontab\b"),                              # cron manipulation
    re.compile(r"\bsystemctl\b"),                            # service control
    re.compile(r"\bservice\b.*\b(start|stop|restart)\b"),    # service mgmt
]


def check_extra_blocked(command: str) -> Optional[str]:
    """Return a reason string if *command* matches an extra blocked pattern.

    Returns ``None`` if the command is acceptable under the extra blocklist.
    """
    for pat in EXTRA_BLOCKED_PATTERNS:
        if pat.search(command):
            return f"Blocked by security pattern: {pat.pattern!r}."
    return None


# ── Combined validation helper ────────────────────────────────────────────


def full_command_check(
    command: str,
    *,
    workspace_root: Optional[Path] = None,
) -> Tuple[bool, str, List[str]]:
    """Run the full v0.8.0 command security pipeline.

    This wraps the existing ``terminal.validate_command`` and layers the
    new checks on top:

    1. Extra blocked patterns (``check_extra_blocked``).
    2. Existing terminal validation (program allowlist, sandbox containment,
       blocklist, shell operators).
    3. Destructive-argument containment (``check_destructive_args``).

    Returns ``(allowed, reason, issues)`` where *issues* is a list of
    human-readable strings (empty when allowed).
    """

    # 1) Extra blocklist first (fast string scan).
    extra = check_extra_blocked(command)
    if extra:
        return False, extra, [extra]

    # 2) Existing terminal validation.
    try:
        from terminal import validate_command
    except ImportError:  # pragma: no cover
        from .terminal import validate_command

    decision = validate_command(command)
    if not decision.allowed:
        return False, decision.reason, [decision.reason]

    # 3) Destructive-argument containment.
    arg_check = check_destructive_args(
        decision.program, decision.args, workspace_root=workspace_root
    )
    if not arg_check.allowed:
        return False, arg_check.reason, [arg_check.reason]

    return True, "ok", []
