"""Workspace: path-safe file operations and git helpers for one task.

Every file operation resolves the target path against the task's workspace root
and refuses anything that escapes it (path-traversal protection). Git helpers
always run with ``cwd`` set to the workspace so the agent cannot touch the host
filesystem outside its assigned directory.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from config import Settings, get_settings


class WorkspaceError(Exception):
    """Raised when a path escapes the workspace or an op is invalid."""


@dataclass
class CommandResult:
    command: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        return self.returncode == 0


# Files/dirs that are never returned by list_files / search_files.
_IGNORE_PATTERNS = [
    ".git",
    "__pycache__",
    "*.pyc",
    ".venv",
    "venv",
    "node_modules",
    ".pytest_cache",
]


def _is_ignored(rel_path: str) -> bool:
    parts = rel_path.replace("\\", "/").split("/")
    for pat in _IGNORE_PATTERNS:
        if any(fnmatch.fnmatch(part, pat) for part in parts):
            return True
    return False


class Workspace:
    """A per-task sandboxed directory rooted at ``workspace_root/<task_id>``."""

    def __init__(self, task_id: str, root: Optional[Path] = None,
                 settings: Optional[Settings] = None,
                 repo_full: Optional[str] = None) -> None:
        self.task_id = task_id
        self.settings = settings or get_settings()
        base = root or self.settings.workspace_root_path

        # Resolve directory name: persistent repo-based name or fallback to task_id
        target_repo = repo_full or self.settings.github_repo_full()
        if target_repo:
            dir_name = "repo_" + target_repo.replace("/", "__").replace("\\", "__")
        else:
            dir_name = task_id

        self.root = (base / dir_name).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ── Path safety ────────────────────────────────────────────────────────
    def safe_path(self, rel: str) -> Path:
        """Resolve ``rel`` under the workspace, rejecting traversal escapes."""
        if rel is None:
            raise WorkspaceError("Path must not be None")
        rel = str(rel).strip()
        # Normalize separators and strip leading slashes so absolute paths
        # are treated as relative to the workspace root.
        rel = rel.replace("\\", "/").lstrip("/")
        if not rel:
            raise WorkspaceError("Path must not be empty")
        if ".." in rel.split("/"):
            raise WorkspaceError(f"Parent references are not allowed: {rel!r}")
        candidate = (self.root / rel).resolve()
        # Ensure the resolved path is inside the workspace root.
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError(
                f"Path escapes workspace: {rel!r}"
            ) from exc
        return candidate

    def rel(self, abs_path: Path) -> str:
        return abs_path.relative_to(self.root).as_posix()

    # ── File operations ────────────────────────────────────────────────────
    def list_files(self, subpath: str = "") -> List[str]:
        base = self.safe_path(subpath) if subpath else self.root
        if not base.exists():
            raise WorkspaceError(f"Path does not exist: {subpath!r}")
        results: List[str] = []
        if base.is_file():
            return [self.rel(base)]
        for p in sorted(base.rglob("*")):
            if p.is_dir():
                continue
            rp = self.rel(p)
            if _is_ignored(rp):
                continue
            results.append(rp)
        return results

    def search_files(self, pattern: str, text: Optional[str] = None,
                     max_results: int = 200) -> List[dict]:
        """Return files whose *name* matches ``pattern`` (glob) and, if
        ``text`` is given, whose contents contain ``text`` (case-insensitive)."""
        results: List[dict] = []
        name_re = re.compile(fnmatch.translate(pattern), re.IGNORECASE)
        for rp in self.list_files():
            name = rp.rsplit("/", 1)[-1]
            if not name_re.match(name):
                continue
            entry = {"path": rp, "name": name, "matched_text": False}
            if text:
                try:
                    content = (self.root / rp).read_text(
                        encoding="utf-8", errors="ignore"
                    )
                    if text.lower() in content.lower():
                        entry["matched_text"] = True
                        # First matching line as a preview.
                        for line in content.splitlines():
                            if text.lower() in line.lower():
                                entry["preview"] = line.strip()[:200]
                                break
                except (OSError, UnicodeDecodeError):
                    pass
                else:
                    if not entry["matched_text"]:
                        continue
            results.append(entry)
            if len(results) >= max_results:
                break
        return results

    def read_file(self, rel: str, max_bytes: int = 256 * 1024) -> str:
        p = self.safe_path(rel)
        if not p.exists():
            raise WorkspaceError(f"File does not exist: {rel!r}")
        if p.is_dir():
            raise WorkspaceError(f"Path is a directory: {rel!r}")
        data = p.read_bytes()[:max_bytes]
        return data.decode("utf-8", errors="replace")

    def write_file(self, rel: str, content: str) -> str:
        p = self.safe_path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return self.rel(p)

    def create_file(self, rel: str, content: str) -> str:
        p = self.safe_path(rel)
        if p.exists():
            raise WorkspaceError(f"File already exists: {rel!r}")
        return self.write_file(rel, content)

    def edit_file(self, rel: str, old: str, new: str,
                  replace_all: bool = False) -> dict:
        """Replace the first (or all) occurrence(s) of ``old`` with ``new``.

        Raises if ``old`` is not found or appears more than once when
        ``replace_all`` is False (mirrors a strict str_replace contract)."""
        p = self.safe_path(rel)
        if not p.exists():
            raise WorkspaceError(f"File does not exist: {rel!r}")
        content = p.read_text(encoding="utf-8")
        count = content.count(old)
        if count == 0:
            raise WorkspaceError(f"Search text not found in {rel!r}")
        if count > 1 and not replace_all:
            raise WorkspaceError(
                f"Search text appears {count} times in {rel!r}; "
                "use replace_all=true or a more specific string."
            )
        if replace_all:
            new_content = content.replace(old, new)
        else:
            new_content = content.replace(old, new, 1)
        p.write_text(new_content, encoding="utf-8")
        return {"path": self.rel(p), "replacements": count if replace_all else 1}

    def delete_file(self, rel: str) -> str:
        p = self.safe_path(rel)
        if not p.exists():
            raise WorkspaceError(f"Path does not exist: {rel!r}")
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return self.rel(p) if p.exists() else rel

    # ── Git helpers (always scoped to this workspace) ──────────────────────
    def _git(self, args: List[str], timeout: int = 30) -> CommandResult:
        env = dict(os.environ)
        # Make sure git never prompts interactively.
        env["GIT_TERMINAL_PROMPT"] = "0"
        proc = subprocess.run(
            ["git", *args],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return CommandResult(
            command="git " + " ".join(args),
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    def git_status(self) -> str:
        return self._git(["status", "--porcelain"]).stdout

    def git_changed_files(self) -> List[str]:
        out = self.git_status()
        files: List[str] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            # Format: "XY path" — take path after the 2 status chars.
            files.append(line[3:].strip().strip('"'))
        return files

    def git_diff(self, staged: bool = False) -> str:
        args = ["diff"]
        if staged:
            args.append("--staged")
        return self._git(args).stdout

    def git_current_branch(self) -> Optional[str]:
        res = self._git(["rev-parse", "--abbrev-ref", "HEAD"])
        if res.success and res.stdout.strip() != "HEAD":
            return res.stdout.strip()
        return None

    def create_branch(self, branch: str) -> CommandResult:
        # Validate branch name.
        check = self._git(["check-ref-format", "--branch", branch])
        if not check.success:
            raise WorkspaceError(f"Invalid branch name: {branch!r}")
        return self._git(["checkout", "-b", branch])

    def git_add_all(self) -> CommandResult:
        return self._git(["add", "-A"])

    def git_commit(self, message: str) -> CommandResult:
        # Use -F to avoid shell-escaping issues with the message.
        import tempfile
        with tempfile.NamedTemporaryFile(
            "w", suffix=".msg", delete=False, encoding="utf-8"
        ) as f:
            f.write(message)
            msg_file = f.name
        try:
            return self._git(["commit", "-F", msg_file])
        finally:
            os.unlink(msg_file)

    def git_push(self, remote: str = "origin",
                 branch: Optional[str] = None) -> CommandResult:
        branch = branch or self.git_current_branch()
        if not branch:
            raise WorkspaceError("No current branch to push.")
        return self._git(["push", "-u", remote, branch], timeout=60)

    def has_git_repo(self) -> bool:
        return (self.root / ".git").exists()

    def git_log_oneline(self, n: int = 5) -> str:
        return self._git(["log", f"-{n}", "--oneline"]).stdout

    def git_list_branches(self) -> List[str]:
        res = self._git(["branch", "-a"])
        if not res.success:
            return []
        branches = []
        for line in res.stdout.splitlines():
            line = line.strip().lstrip("*").strip()
            if line:
                branches.append(line)
        return branches

    def git_checkout(self, branch: str, create: bool = False) -> CommandResult:
        # Validate branch name first
        if not create:
            # Switch to existing branch
            return self._git(["checkout", branch])
        # Validate ref format for creation
        check = self._git(["check-ref-format", "--branch", branch])
        if not check.success:
            raise WorkspaceError(f"Invalid branch name: {branch!r}")
        return self._git(["checkout", "-b", branch])

    def git_stage_file(self, rel_path: str) -> CommandResult:
        p = self.safe_path(rel_path)  # Sandbox containment check
        return self._git(["add", rel_path])

    def git_unstage_file(self, rel_path: str) -> CommandResult:
        p = self.safe_path(rel_path)  # Sandbox containment check
        return self._git(["reset", "HEAD", rel_path])

    def git_discard_file(self, rel_path: str) -> CommandResult:
        p = self.safe_path(rel_path)  # Sandbox containment check
        return self._git(["checkout", "--", rel_path])
