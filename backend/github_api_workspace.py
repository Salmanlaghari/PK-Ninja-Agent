"""GitHub API Workspace — serverless-safe file operations via GitHub REST API.

When git CLI is not available (Vercel serverless), this module provides
file tree listing and lazy file reading using the GitHub API directly.
No git binary required.

v1.2.2: Ultra-minimal — tree-only fetch, no eager downloads.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional, Set

from workspace import Workspace, CommandResult

log = logging.getLogger("pk_ninja.github_api_workspace")


def _github_token(token: str = "") -> str:
    return token or os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN", "")


def _github_api(path: str, timeout: int = 10, token: str = "") -> object:
    """Call GitHub REST API directly."""
    token = _github_token(token)
    if not token:
        raise RuntimeError("No GitHub token")
    url = f"https://api.github.com/{path.lstrip('/')}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "NinjaDev-Agent",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


class GitHubAPIWorkspace(Workspace):
    """Workspace that uses GitHub API for file listing when git is unavailable."""

    def __init__(self, task_id: str, root: Optional[Path] = None,
                 settings=None, repo_full: Optional[str] = None,
                 token: Optional[str] = None) -> None:
        super().__init__(task_id, root=root, settings=settings, repo_full=repo_full)
        self._repo_full = repo_full or (settings.github_repo_full() if settings else "")
        self._token = token or ""
        self._fetched = False
        self._file_paths: List[str] = []
        self._file_shas: dict = {}  # path -> sha
        self._downloaded: Set[str] = set()

    def fetch_repo_files(self, ref: str = "HEAD") -> int:
        """Fetch repository file TREE only (fast, single API call).

        Returns the number of files in the tree. Files are NOT downloaded
        yet — they are fetched on-demand via read_file().
        """
        token = _github_token(self._token)
        if not self._repo_full or not token:
            return 0

        try:
            tree_data = _github_api(f"repos/{self._repo_full}/git/trees/{ref}", token=token)
        except Exception as exc:
            log.error("GitHub tree fetch failed: %s", exc)
            return 0

        tree = tree_data.get("tree", []) if isinstance(tree_data, dict) else []
        self._file_paths = []
        self._file_shas = {}

        for item in tree:
            if item.get("type") != "blob":
                continue
            path = item.get("path", "")
            if not path or self._should_skip(path):
                continue
            self._file_paths.append(path)
            self._file_shas[path] = item.get("sha", "")

        self._fetched = True
        log.info("GitHub API: tree has %d files", len(self._file_paths))
        return len(self._file_paths)

    def _should_skip(self, path: str) -> bool:
        skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
        skip_exts = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2",
                     ".ttf", ".eot", ".mp4", ".mp3", ".zip", ".tar", ".gz", ".exe", ".dll", ".db"}
        parts = path.split("/")
        if any(d in skip_dirs for d in parts):
            return True
        return Path(path).suffix.lower() in skip_exts

    def list_files(self, subpath: str = "") -> List[str]:
        if self._fetched:
            if subpath:
                return [p for p in self._file_paths if p.startswith(subpath)]
            return list(self._file_paths)
        return super().list_files(subpath)

    def read_file(self, rel: str, max_bytes: int = 256 * 1024) -> str:
        # Check local disk first
        p = self.safe_path(rel)
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace")[:max_bytes]

        # Lazy download from GitHub API
        if self._fetched and rel in self._file_shas:
            sha = self._file_shas[rel]
            try:
                blob = _github_api(f"repos/{self._repo_full}/git/blobs/{sha}", timeout=10, token=_github_token(self._token))
                content_b64 = blob.get("content", "")
                if blob.get("encoding") == "base64" and content_b64:
                    content = base64.b64decode(content_b64).decode("utf-8", errors="replace")
                else:
                    content = content_b64
                # Cache to disk
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
                self._downloaded.add(rel)
                return content[:max_bytes]
            except Exception as exc:
                log.debug("Lazy download failed for %s: %s", rel, exc)

        raise FileNotFoundError(f"File not found: {rel}")

    def file_exists(self, rel: str) -> bool:
        if self._fetched:
            return rel in self._file_shas
        return self.safe_path(rel).exists()

    def has_git_repo(self) -> bool:
        return self._fetched or super().has_git_repo()

    def git_status(self) -> str:
        return "" if not super().has_git_repo() else super().git_status()

    def git_changed_files(self) -> List[str]:
        return [] if not super().has_git_repo() else super().git_changed_files()

    def git_diff(self, staged: bool = False) -> str:
        return "" if not super().has_git_repo() else super().git_diff(staged)

    def git_current_branch(self) -> Optional[str]:
        return "main" if not super().has_git_repo() else super().git_current_branch()

    def git_commit(self, message: str) -> CommandResult:
        if super().has_git_repo():
            return super().git_commit(message)
        return CommandResult("github-api", 0, "API mode: no local git", "")

    def git_push(self, remote: str = "origin", branch: Optional[str] = None) -> CommandResult:
        if super().has_git_repo():
            return super().git_push(remote, branch)
        return CommandResult("github-api", 0, "API mode: no local git", "")


__all__ = ["GitHubAPIWorkspace"]
