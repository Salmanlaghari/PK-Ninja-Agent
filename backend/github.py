"""GitHub integration — all operations stay server-side.

Uses the authenticated ``gh`` CLI (which reads GITHUB_TOKEN from the env) for
API calls, and plain ``git`` for clone/pull/commit/push. Credentials are never
exposed to the frontend.

Functions:
  * repo_info()           -> public metadata for the configured repo
  * clone_or_pull()       -> populate a workspace from the repo
  * prepare_pull_request()-> build a PR title/body + ready-to-call PR payload
                              (does NOT create the PR — that's an explicit user action)
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Optional

from workspace import CommandResult, Workspace

from config import Settings, get_settings


class GitHubError(Exception):
    pass


@dataclass
class RepoInfo:
    full_name: str
    default_branch: str
    private: bool
    description: str
    html_url: str
    clone_url: str


def _masked_clone_url(owner: str, repo: str) -> str:
    """Build an HTTPS clone URL that injects the token for git only.

    We never print this URL to logs or the frontend; we pass it directly to
    ``git clone`` and rely on git not echoing credentials.
    """
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
    return f"https://github.com/{owner}/{repo}.git"


def _run(args, cwd, timeout=60) -> CommandResult:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True,
        timeout=timeout, env=env,
    )
    return CommandResult(
        command=" ".join(args),
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def _gh(args, timeout=30) -> CommandResult:
    """Run a ``gh`` CLI command using the token from the environment."""
    env = dict(os.environ)
    env["GH_TOKEN"] = os.environ.get("GITHUB_TOKEN", "")
    proc = subprocess.run(
        ["gh", *args], capture_output=True, text=True,
        timeout=timeout, env=env,
    )
    return CommandResult(
        command="gh " + " ".join(args),
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def repo_info(settings: Optional[Settings] = None) -> RepoInfo:
    settings = settings or get_settings()
    if not settings.github_repo_full():
        raise GitHubError("GITHUB_OWNER / GITHUB_REPO are not configured.")
    res = _gh(["repo", "view", settings.github_repo_full(), "--json",
               "nameWithOwner,defaultBranchRef,isPrivate,description,url,url"])
    if not res.success:
        raise GitHubError(f"gh repo view failed: {res.stderr.strip()}")
    data = json.loads(res.stdout)
    return RepoInfo(
        full_name=data.get("nameWithOwner", settings.github_repo_full()),
        default_branch=(data.get("defaultBranchRef") or {}).get("name", "main"),
        private=bool(data.get("isPrivate", False)),
        description=data.get("description") or "",
        html_url=data.get("url", ""),
        clone_url=f"https://github.com/{settings.github_repo_full()}.git",
    )


def clone_or_pull(workspace: Workspace,
                  settings: Optional[Settings] = None) -> CommandResult:
    """Populate ``workspace`` from the configured repo.

    If the workspace already has a ``.git`` dir, runs ``git pull``; otherwise
    clones into the workspace root. Returns the CommandResult of the last op.
    """
    settings = settings or workspace.settings
    full = settings.github_repo_full()
    if not full:
        raise GitHubError("GITHUB_OWNER / GITHUB_REPO are not configured.")

    if workspace.has_git_repo():
        # Reset to origin default branch then pull.
        info = repo_info(settings)
        res = _run(["git", "fetch", "origin"], cwd=str(workspace.root))
        if not res.success:
            return res
        res = _run(["git", "checkout", info.default_branch], cwd=str(workspace.root))
        # checkout may fail if already on a branch with commits; pull instead.
        res = _run(["git", "pull", "--ff-only"], cwd=str(workspace.root))
        return res

    # Fresh clone. We clone into a temp dir then move contents so the
    # workspace root itself becomes the repo root (cleaner for the agent).
    url = _masked_clone_url(settings.github_owner, settings.github_repo)
    res = _run(["git", "clone", "--depth", "1", url, str(workspace.root)],
               cwd=str(workspace.settings.workspace_root_path), timeout=120)
    if not res.success and "already exists and is not an empty directory" in res.stderr:
        # Workspace dir had stray files (e.g. .gitkeep). Retry by cloning to a
        # subdir and moving.
        import shutil
        tmp = str(workspace.root) + "_clone_tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        res = _run(["git", "clone", "--depth", "1", url, tmp],
                   cwd=str(workspace.settings.workspace_root_path), timeout=120)
        if res.success:
            for item in os.listdir(tmp):
                src = os.path.join(tmp, item)
                dst = os.path.join(str(workspace.root), item)
                if os.path.exists(dst):
                    shutil.rmtree(dst, ignore_errors=True) if os.path.isdir(dst) else os.remove(dst)
                shutil.move(src, dst)
            shutil.rmtree(tmp, ignore_errors=True)
    return res


def prepare_pull_request(workspace: Workspace, *,
                         title: Optional[str] = None,
                         body: Optional[str] = None,
                         settings: Optional[Settings] = None) -> dict:
    """Build everything needed to open a PR **without** opening it.

    Returns a dict with the suggested title, body, base branch, head branch,
    and the exact ``gh pr create`` command the user can run. PR creation is an
    explicit user action in this MVP.
    """
    settings = settings or workspace.settings
    info = repo_info(settings)
    head = workspace.git_current_branch()
    changed = workspace.git_changed_files()
    diff = workspace.git_diff(staged=True) or workspace.git_diff(staged=False)

    if not title:
        title = f"PK Ninja Agent: {head or 'changes'}"
    if not body:
        body_lines = [
            "## Summary",
            "",
            "Changes produced by the PK Ninja Agent MVP.",
            "",
            "## Changed files",
            "",
        ]
        for f in changed or ["(none)"]:
            body_lines.append(f"- `{f}`")
        body_lines += ["", "## Diff", "", "```diff", diff[:60000], "```"]
        body = "\n".join(body_lines)

    cmd = (
        f"gh pr create --base {info.default_branch} --head {head or 'HEAD'} "
        f"--title {json.dumps(title)} --body {json.dumps(body)}"
    )
    return {
        "ready": bool(changed) and bool(head),
        "base": info.default_branch,
        "head": head,
        "title": title,
        "body": body,
        "changed_files": changed,
        "command": cmd,
        "html_url": info.html_url,
    }


# ── GitHub API exploration tools (v1.3.0) ─────────────────────────────
# These functions let the agent (and the API endpoints) query GitHub
# directly via the ``gh`` CLI using the stored token. They power "show me
# my repo list", "list issues", "list PRs", etc.

def list_user_repos(settings: Optional[Settings] = None,
                    limit: int = 30) -> list:
    """List the authenticated user's repositories.

    Uses ``gh repo list`` which reads ``GH_TOKEN`` / ``GITHUB_TOKEN`` from
    the environment. Returns a list of dicts with name, full_name, private,
    description, default_branch, updated_at, and html_url.
    """
    res = _gh([
        "repo", "list", "--limit", str(limit),
        "--json", "name,nameWithOwner,isPrivate,description,"
                  "defaultBranchRef,updatedAt,url",
    ], timeout=30)
    if not res.success:
        raise GitHubError(f"gh repo list failed: {res.stderr.strip()[:300]}")
    try:
        data = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        data = []
    repos = []
    for item in data:
        repos.append({
            "name": item.get("name", ""),
            "full_name": item.get("nameWithOwner", ""),
            "private": bool(item.get("isPrivate", False)),
            "description": item.get("description") or "",
            "default_branch": (item.get("defaultBranchRef") or {}).get("name", "main"),
            "updated_at": item.get("updatedAt", ""),
            "html_url": item.get("url", ""),
        })
    return repos


def list_repo_issues(owner: str, repo: str,
                     state: str = "open",
                     limit: int = 20,
                     settings: Optional[Settings] = None) -> list:
    """List issues for ``owner/repo``.

    ``state`` can be ``open``, ``closed``, or ``all``.
    """
    full = f"{owner}/{repo}"
    res = _gh([
        "issue", "list", "--repo", full,
        "--state", state, "--limit", str(limit),
        "--json", "number,title,state,author,createdAt,updatedAt,url,labels",
    ], timeout=30)
    if not res.success:
        raise GitHubError(f"gh issue list failed: {res.stderr.strip()[:300]}")
    try:
        data = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        data = []
    issues = []
    for item in data:
        author = item.get("author") or {}
        issues.append({
            "number": item.get("number", 0),
            "title": item.get("title", ""),
            "state": item.get("state", ""),
            "author": author.get("login", "") if isinstance(author, dict) else str(author),
            "created_at": item.get("createdAt", ""),
            "updated_at": item.get("updatedAt", ""),
            "url": item.get("url", ""),
            "labels": [l.get("name", "") if isinstance(l, dict) else str(l)
                       for l in (item.get("labels") or [])],
        })
    return issues


def list_repo_prs(owner: str, repo: str,
                  state: str = "open",
                  limit: int = 20,
                  settings: Optional[Settings] = None) -> list:
    """List pull requests for ``owner/repo``.

    ``state`` can be ``open``, ``closed``, ``merged``, or ``all``.
    """
    full = f"{owner}/{repo}"
    res = _gh([
        "pr", "list", "--repo", full,
        "--state", state, "--limit", str(limit),
        "--json", "number,title,state,author,headRefName,baseRefName,"
                  "createdAt,updatedAt,url,isDraft,additions,deletions",
    ], timeout=30)
    if not res.success:
        raise GitHubError(f"gh pr list failed: {res.stderr.strip()[:300]}")
    try:
        data = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        data = []
    prs = []
    for item in data:
        author = item.get("author") or {}
        prs.append({
            "number": item.get("number", 0),
            "title": item.get("title", ""),
            "state": item.get("state", ""),
            "author": author.get("login", "") if isinstance(author, dict) else str(author),
            "head_branch": item.get("headRefName", ""),
            "base_branch": item.get("baseRefName", ""),
            "created_at": item.get("createdAt", ""),
            "updated_at": item.get("updatedAt", ""),
            "url": item.get("url", ""),
            "is_draft": bool(item.get("isDraft", False)),
            "additions": item.get("additions", 0),
            "deletions": item.get("deletions", 0),
        })
    return prs


def get_repo_details(owner: str, repo: str,
                     settings: Optional[Settings] = None) -> dict:
    """Get detailed info about a single repo."""
    full = f"{owner}/{repo}"
    res = _gh([
        "repo", "view", full, "--json",
        "name,nameWithOwner,isPrivate,description,defaultBranchRef,"
        "url,stargazerCount,forkCount,primaryLanguage,createdAt,updatedAt",
    ], timeout=30)
    if not res.success:
        raise GitHubError(f"gh repo view failed: {res.stderr.strip()[:300]}")
    try:
        data = json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    lang = data.get("primaryLanguage") or {}
    return {
        "name": data.get("name", ""),
        "full_name": data.get("nameWithOwner", full),
        "private": bool(data.get("isPrivate", False)),
        "description": data.get("description") or "",
        "default_branch": (data.get("defaultBranchRef") or {}).get("name", "main"),
        "html_url": data.get("url", ""),
        "stars": data.get("stargazerCount", 0),
        "forks": data.get("forkCount", 0),
        "language": lang.get("name", "") if isinstance(lang, dict) else str(lang),
        "created_at": data.get("createdAt", ""),
        "updated_at": data.get("updatedAt", ""),
    }


def list_repo_branches(owner: str, repo: str,
                       limit: int = 30,
                       settings: Optional[Settings] = None) -> list:
    """List branches for ``owner/repo``."""
    full = f"{owner}/{repo}"
    res = _gh([
        "api", f"repos/{full}/branches",
        "--paginate", "--jq",
        f'[.[:{limit}][] | {{"name": .name, "protected": .protected}}]',
    ], timeout=30)
    if not res.success:
        raise GitHubError(f"gh api branches failed: {res.stderr.strip()[:300]}")
    try:
        return json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return []


def create_pull_request(workspace: Workspace, *,
                        title: Optional[str] = None,
                        body: Optional[str] = None,
                        settings: Optional[Settings] = None) -> dict:
    """Actually open a PR via ``gh``. Called only on explicit user action."""
    settings = settings or workspace.settings
    prep = prepare_pull_request(workspace, title=title, body=body, settings=settings)
    head = prep["head"]
    if not head:
        raise GitHubError("No current branch; create a branch first.")
    res = _gh([
        "pr", "create",
        "--base", prep["base"],
        "--head", head,
        "--title", prep["title"],
        "--body", prep["body"],
    ], timeout=60)
    if not res.success:
        raise GitHubError(f"gh pr create failed: {res.stderr.strip()}")
    # gh prints the new PR URL to stdout.
    return {"pr_url": res.stdout.strip(), "prepared": prep}
