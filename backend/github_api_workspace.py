"""GitHub API Workspace — serverless-safe file operations via GitHub REST API.

When git CLI is not available (Vercel serverless), this module provides
file fetching, indexing, and commit/push operations using the GitHub API
directly. No git binary required.

Usage:
    from github_api_workspace import GitHubAPIWorkspace
    ws = GitHubAPIWorkspace(task_id, repo_full="owner/repo")
    ws.fetch_repo_files()  # Downloads all files via API
    ws.list_files()        # Lists fetched files
    ws.read_file("path")   # Reads a fetched file
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

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

    def fetch_repo_files(self, ref: str = "HEAD") -> int:
        """Fetch all repository files via GitHub Trees API.

        Downloads the file tree, then fetches each file's content.
        Returns the number of files fetched.
        """
        if not self._repo_full:
            log.warning("No repo configured, skipping fetch")
            return 0

        token = _github_token()
        if not token:
            log.warning("No GitHub token, skipping fetch")
            return 0

        try:
            # Get the tree recursively
            tree_data = _github_api(
                f"repos/{self._repo_full}/git/trees/{ref}?recursive=1",
                timeout=60,
            )
        except Exception as exc:
            log.error("Failed to fetch tree: %s", exc)
            return 0

        tree = tree_data.get("tree", []) if isinstance(tree_data, dict) else []
        if not tree:
            log.warning("Empty tree for %s", self._repo_full)
            return 0

        # Filter to blobs (files) only, skip large files (>1MB)
        files = [
            item for item in tree
            if item.get("type") == "blob"
            and item.get("size", 0) < 1_000_000
            and not self._should_skip(item.get("path", ""))
        ]

        log.info("Fetching %d files from %s", len(files), self._repo_full)
        fetched = 0

        for item in files:
            path = item.get("path", "")
            sha = item.get("sha", "")
            if not path or not sha:
                continue

            try:
                blob = _github_api(
                    f"repos/{self._repo_full}/git/blobs/{sha}",
                    timeout=15,
                )
                content_b64 = blob.get("content", "") if isinstance(blob, dict) else ""
                encoding = blob.get("encoding", "base64") if isinstance(blob, dict) else "base64"

                if encoding == "base64" and content_b64:
                    content = base64.b64decode(content_b64).decode("utf-8", errors="replace")
                else:
                    content = content_b64

                # Write to workspace
                file_path = self.root / path
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content, encoding="utf-8")
                fetched += 1

            except Exception as exc:
                log.debug("Failed to fetch %s: %s", path, exc)
                continue

        self._fetched = True
        log.info("Fetched %d/%d files from GitHub", fetched, len(files))
        return fetched

    def _should_skip(self, path: str) -> bool:
        """Check if a file should be skipped (binary, large, etc.)."""
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

    def has_git_repo(self) -> bool:
        """Always return True after fetch (we have the files)."""
        if self._fetched:
            return True
        return super().has_git_repo()

    def git_status(self) -> str:
        """Return empty status for API workspace (no local git)."""
        if not super().has_git_repo():
            return ""
        return super().git_status()

    def git_changed_files(self) -> List[str]:
        """Return changed files (empty for API workspace without git)."""
        if not super().has_git_repo():
            return []
        return super().git_changed_files()

    def git_diff(self, staged: bool = False) -> str:
        """Return empty diff for API workspace without git."""
        if not super().has_git_repo():
            return ""
        return super().git_diff(staged)

    def git_current_branch(self) -> Optional[str]:
        """Return default branch from settings for API workspace."""
        if not super().has_git_repo():
            if self.settings:
                return self.settings.github_default_branch if hasattr(self.settings, 'github_default_branch') else "main"
            return "main"
        return super().git_current_branch()

    def git_commit(self, message: str) -> CommandResult:
        """Commit via GitHub API (create a tree + commit)."""
        if super().has_git_repo():
            return super().git_commit(message)

        # API-based commit
        if not self._repo_full:
            return CommandResult("github-api commit", 1, "", "No repo configured")

        token = _github_token()
        if not token:
            return CommandResult("github-api commit", 1, "", "No GitHub token")

        try:
            branch = self.git_current_branch() or "main"

            # Get current commit SHA
            ref_data = _github_api(f"repos/{self._repo_full}/git/refs/heads/{branch}")
            if not isinstance(ref_data, dict):
                return CommandResult("github-api commit", 1, "", "Failed to get ref")
            commit_sha = ref_data.get("object", {}).get("sha", "")
            if not commit_sha:
                return CommandResult("github-api commit", 1, "", "No commit SHA")

            # Get the current tree
            commit_data = _github_api(f"repos/{self._repo_full}/git/commits/{commit_sha}")
            tree_sha = commit_data.get("tree", {}).get("sha", "") if isinstance(commit_data, dict) else ""

            # Collect changed files from workspace
            changed = []
            for file_path in self.root.rglob("*"):
                if file_path.is_file() and ".git" not in file_path.parts:
                    rel = str(file_path.relative_to(self.root))
                    try:
                        content = file_path.read_text(encoding="utf-8")
                        blob = _github_api(
                            f"repos/{self._repo_full}/git/blobs",
                            method="POST",
                            body={"content": content, "encoding": "utf-8"},
                        )
                        blob_sha = blob.get("sha", "") if isinstance(blob, dict) else ""
                        if blob_sha:
                            changed.append({
                                "path": rel,
                                "mode": "100644",
                                "type": "blob",
                                "sha": blob_sha,
                            })
                    except Exception:
                        continue

            if not changed:
                return CommandResult("github-api commit", 0, "No changes to commit", "")

            # Create new tree
            new_tree = _github_api(
                f"repos/{self._repo_full}/git/trees",
                method="POST",
                body={"base_tree": tree_sha, "tree": changed},
            )
            new_tree_sha = new_tree.get("sha", "") if isinstance(new_tree, dict) else ""

            # Create commit
            new_commit = _github_api(
                f"repos/{self._repo_full}/git/commits",
                method="POST",
                body={
                    "message": message,
                    "tree": new_tree_sha,
                    "parents": [commit_sha],
                },
            )
            new_commit_sha = new_commit.get("sha", "") if isinstance(new_commit, dict) else ""

            # Update ref
            _github_api(
                f"repos/{self._repo_full}/git/refs/heads/{branch}",
                method="PATCH",
                body={"sha": new_commit_sha, "force": False},
            )

            return CommandResult("github-api commit", 0, f"Committed: {new_commit_sha[:8]}", "")

        except Exception as exc:
            return CommandResult("github-api commit", 1, "", str(exc))

    def git_push(self, remote: str = "origin",
                 branch: Optional[str] = None) -> CommandResult:
        """Push is implicit in API-based commit (ref already updated)."""
        if super().has_git_repo():
            return super().git_push(remote, branch)
        return CommandResult("github-api push", 0, "Push completed (API-based)", "")

    def create_branch(self, branch: str) -> CommandResult:
        """Create branch via GitHub API."""
        if super().has_git_repo():
            return super().create_branch(branch)

        if not self._repo_full:
            return CommandResult("github-api branch", 1, "", "No repo configured")

        try:
            # Get default branch HEAD
            ref_data = _github_api(f"repos/{self._repo_full}/git/refs/heads/main")
            if not isinstance(ref_data, dict):
                return CommandResult("github-api branch", 1, "", "Failed to get ref")
            sha = ref_data.get("object", {}).get("sha", "")

            # Create new ref
            _github_api(
                f"repos/{self._repo_full}/git/refs",
                method="POST",
                body={"ref": f"refs/heads/{branch}", "sha": sha},
            )
            return CommandResult("github-api branch", 0, f"Branch '{branch}' created", "")

        except Exception as exc:
            return CommandResult("github-api branch", 1, "", str(exc))


__all__ = ["GitHubAPIWorkspace"]
