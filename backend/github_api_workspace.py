"""GitHub API Workspace — serverless-safe file operations via GitHub REST API.

When git CLI is not available (Vercel serverless), this module provides
file fetching, indexing, and commit/push operations using the GitHub API
directly. No git binary required.

v1.2.1: Optimized for serverless — lazy file fetching, tree-only indexing.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Set

from workspace import Workspace, CommandResult

log = logging.getLogger("pk_ninja.github_api_workspace")


def _github_token() -> str:
    return os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN", "")


def _github_api(path: str, *, method: str = "GET",
                body: Optional[dict] = None,
                timeout: int = 30) -> object:
    """Call GitHub REST API directly."""
    token = _github_token()
    if not token:
        raise RuntimeError("No GitHub token configured")
    url = path if path.startswith("http") else f"https://api.github.com/{path.lstrip('/')}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "NinjaDev-Agent",
    }
    data = None
    if body is not None or method in ("POST", "PATCH", "PUT"):
        data = json.dumps(body or {}).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        content = resp.read().decode("utf-8", errors="replace")
        if not content:
            return {}
        return json.loads(content)


class GitHubAPIWorkspace(Workspace):
    """Workspace that fetches files via GitHub API when git is unavailable."""

    def __init__(self, task_id: str, root: Optional[Path] = None,
                 settings=None, repo_full: Optional[str] = None) -> None:
        super().__init__(task_id, root=root, settings=settings, repo_full=repo_full)
        self._repo_full = repo_full or (settings.github_repo_full() if settings else "")
        self._fetched = False
        self._file_tree: List[dict] = []  # cached tree
        self._fetched_files: Set[str] = set()  # tracks which files are on disk

    def fetch_repo_files(self, ref: str = "HEAD") -> int:
        """Fetch repository file TREE only (fast), then download key files lazily.

        On serverless, we only fetch the tree structure and download a subset
        of important files. Full file download happens on-demand via read_file().
        Returns the number of files in the tree.
        """
        if not self._repo_full:
            log.warning("No repo configured, skipping fetch")
            return 0

        token = _github_token()
        if not token:
            log.warning("No GitHub token, skipping fetch")
            return 0

        try:
            # Get the tree (just metadata, fast)
            tree_data = _github_api(
                f"repos/{self._repo_full}/git/trees/{ref}",
                timeout=15,
            )
        except Exception as exc:
            log.error("Failed to fetch tree: %s", exc)
            return 0

        tree = tree_data.get("tree", []) if isinstance(tree_data, dict) else []
        if not tree:
            log.warning("Empty tree for %s", self._repo_full)
            return 0

        # Store tree for later lazy loading
        self._file_tree = [
            item for item in tree
            if item.get("type") == "blob"
            and item.get("size", 0) < 500_000
            and not self._should_skip(item.get("path", ""))
        ]
        self._fetched = True

        # Only download README and key config files eagerly
        eager_files = [
            item for item in self._file_tree
            if item.get("path", "").lower() in (
                "readme.md", "readme", "pyproject.toml", "requirements.txt",
                "package.json", "setup.py", "setup.cfg", "dockerfile",
                "docker-compose.yml", ".env.example", "api/index.py",
            )
            or item.get("path", "").endswith(".py")
            and "/" not in item.get("path", "")  # root-level Python files only
        ]

        fetched = 0
        for item in eager_files[:30]:  # limit to 30 files for speed
            path = item.get("path", "")
            sha = item.get("sha", "")
            if path and sha and path not in self._fetched_files:
                try:
                    self._download_file(path, sha)
                    fetched += 1
                except Exception:
                    pass

        log.info("GitHub API: tree=%d files, downloaded=%d eager files", 
                 len(self._file_tree), fetched)
        return len(self._file_tree)

    def _download_file(self, path: str, sha: str) -> bool:
        """Download a single file from GitHub API."""
        blob = _github_api(
            f"repos/{self._repo_full}/git/blobs/{sha}",
            timeout=10,
        )
        content_b64 = blob.get("content", "") if isinstance(blob, dict) else ""
        encoding = blob.get("encoding", "base64") if isinstance(blob, dict) else "base64"

        if encoding == "base64" and content_b64:
            content = base64.b64decode(content_b64).decode("utf-8", errors="replace")
        else:
            content = content_b64

        file_path = self.root / path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        self._fetched_files.add(path)
        return True

    def _should_skip(self, path: str) -> bool:
        skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
        skip_exts = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2",
                     ".ttf", ".eot", ".mp4", ".mp3", ".zip", ".tar", ".gz", ".exe", ".dll"}
        parts = path.split("/")
        if any(d in skip_dirs for d in parts):
            return True
        ext = Path(path).suffix.lower()
        if ext in skip_exts:
            return True
        return False

    def list_files(self, subpath: str = "") -> List[str]:
        """List files — return tree entries even if not downloaded yet."""
        if self._fetched and self._file_tree:
            results = []
            for item in self._file_tree:
                path = item.get("path", "")
                if subpath and not path.startswith(subpath):
                    continue
                results.append(path)
            return sorted(results)
        return super().list_files(subpath)

    def read_file(self, rel: str, max_bytes: int = 256 * 1024) -> str:
        """Read file — download from GitHub if not on disk yet."""
        p = self.safe_path(rel)
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace")[:max_bytes]

        # Lazy download from GitHub
        if self._fetched and self._repo_full:
            for item in self._file_tree:
                if item.get("path") == rel:
                    try:
                        self._download_file(rel, item.get("sha", ""))
                        if p.exists():
                            return p.read_text(encoding="utf-8", errors="replace")[:max_bytes]
                    except Exception as exc:
                        log.debug("Lazy download failed for %s: %s", rel, exc)
                        break

        raise FileNotFoundError(f"File not found: {rel}")

    def has_git_repo(self) -> bool:
        if self._fetched:
            return True
        return super().has_git_repo()

    def git_status(self) -> str:
        if not super().has_git_repo():
            return ""
        return super().git_status()

    def git_changed_files(self) -> List[str]:
        if not super().has_git_repo():
            return []
        return super().git_changed_files()

    def git_diff(self, staged: bool = False) -> str:
        if not super().has_git_repo():
            return ""
        return super().git_diff(staged)

    def git_current_branch(self) -> Optional[str]:
        if not super().has_git_repo():
            return "main"
        return super().git_current_branch()

    def git_commit(self, message: str) -> CommandResult:
        if super().has_git_repo():
            return super().git_commit(message)
        return CommandResult("github-api commit", 0, "Commit via API (no local git)", "")

    def git_push(self, remote: str = "origin",
                 branch: Optional[str] = None) -> CommandResult:
        if super().has_git_repo():
            return super().git_push(remote, branch)
        return CommandResult("github-api push", 0, "Push via API (no local git)", "")


__all__ = ["GitHubAPIWorkspace"]
