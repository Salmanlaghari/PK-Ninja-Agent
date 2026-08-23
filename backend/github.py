"""GitHub integration — all operations stay server-side.

Uses the GitHub REST API (via ``urllib.request``) for metadata reads and
plain ``git`` for clone/pull/commit/push. Credentials are never exposed to
the frontend. This works on serverless platforms (Vercel) where the ``gh``
CLI is not installed, because it talks to api.github.com directly using
``GITHUB_TOKEN`` from the environment.

Functions:
  * repo_info()           -> public metadata for the configured repo
  * clone_or_pull()       -> populate a workspace from the repo
  * prepare_pull_request()-> build a PR title/body + ready-to-call PR payload
                              (does NOT create the PR — that's an explicit user action)
  * list_user_repos()     -> list the authenticated user's repositories
  * list_repo_issues()    -> list issues for a repo
  * list_repo_prs()       -> list pull requests for a repo
  * get_repo_details()    -> detailed info about a single repo
  * list_repo_branches()  -> list branches for a repo
  * create_pull_request() -> open a PR (explicit user action only)
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
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


# ── Dulwich (pure-Python git) helpers — serverless full GitHub access ──────
def has_git_binary() -> bool:
    import shutil
    return shutil.which("git") is not None


def dulwich_available() -> bool:
    try:
        import dulwich  # noqa: F401
        return True
    except ImportError:
        return False


def _github_token_for_git(settings: Optional[Settings]) -> str:
    return (os.environ.get("GITHUB_TOKEN", "")
            or os.environ.get("GH_TOKEN", "")
            or getattr(settings, "github_token", "") or "")


def dulwich_clone_or_pull(workspace: Workspace,
                          settings: Settings) -> CommandResult:
    """Clone/pull using pure-Python dulwich (no git binary required).

    The token is embedded only in the in-memory URL and is never logged or
    returned to the client.
    """
    token = _github_token_for_git(settings)
    full = settings.github_repo_full()
    url = (f"https://{token}@github.com/{full}.git" if token
           else f"https://github.com/{full}.git")
    try:
        from dulwich import porcelain
        if workspace.has_git_repo():
            porcelain.pull(str(workspace.root), url)
            out = f"pulled {full} via dulwich"
        else:
            porcelain.clone(url, str(workspace.root), depth=1)
            out = f"cloned {full} via dulwich"
        return CommandResult(command="dulwich", returncode=0,
                             stdout=out, stderr="")
    except Exception as exc:  # noqa: BLE001 — surface a clean failure
        return CommandResult(command="dulwich", returncode=1, stdout="",
                             stderr=f"dulwich failed: {exc}"[:400])


def _run(args, cwd, timeout=60) -> CommandResult:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True,
            timeout=timeout, env=env,
        )
    except FileNotFoundError:
        return CommandResult(
            command=" ".join(args),
            returncode=127,
            stdout="",
            stderr=f"Command not found: {args[0]}. "
                   f"Ensure '{args[0]}' is installed and on PATH.",
        )
    return CommandResult(
        command=" ".join(args),
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def _gh(args, timeout=30) -> CommandResult:
    """Run a ``gh`` CLI command using the token from the environment.

    Kept as a fallback for environments where the ``gh`` CLI is installed.
    On serverless platforms (Vercel) the CLI is absent, so callers should
    prefer :func:`_github_api` which talks to the REST API directly.
    """
    env = dict(os.environ)
    env["GH_TOKEN"] = os.environ.get("GITHUB_TOKEN", "")
    try:
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
    except FileNotFoundError:
        return CommandResult(
            command="gh " + " ".join(args),
            returncode=127,
            stdout="",
            stderr="gh CLI is not installed on this host",
        )
    except subprocess.TimeoutExpired:
        return CommandResult(
            command="gh " + " ".join(args),
            returncode=124,
            stdout="",
            stderr="gh CLI timed out",
        )


def _github_token() -> str:
    """Return the GitHub token from the environment (or empty string)."""
    return os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN", "")


def _github_api(path: str, *, method: str = "GET",
                body: Optional[dict] = None,
                timeout: int = 30,
                raw: bool = False) -> object:
    """Call the GitHub REST API directly using ``urllib.request``.

    ``path`` is the part after ``https://api.github.com`` (with or without a
    leading slash). Returns the parsed JSON response (a dict or list), or the
    raw decoded text when ``raw=True``. Raises :class:`GitHubError` on any
    HTTP error or missing token.
    """
    token = _github_token()
    if not token:
        raise GitHubError(
            "No GitHub token configured. Set the GITHUB_TOKEN environment "
            "variable (or connect your GitHub account in Settings)."
        )
    url = path if path.startswith("http") else f"https://api.github.com/{path.lstrip('/')}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "NinjaDev-Agent",
    }
    data = None
    if body is not None or method in ("POST", "PATCH", "PUT"):
        payload = json.dumps(body or {}).encode("utf-8")
        headers["Content-Type"] = "application/json"
        data = payload
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8", errors="replace")
            if raw or not content:
                return content
            return json.loads(content)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            pass
        raise GitHubError(
            f"GitHub API {method} {path} failed: HTTP {exc.code} {detail}"
        )
    except urllib.error.URLError as exc:
        raise GitHubError(f"GitHub API {method} {path} failed: {exc.reason}")
    except json.JSONDecodeError:
        raise GitHubError(f"GitHub API {method} {path} returned invalid JSON")


def repo_info(settings: Optional[Settings] = None) -> RepoInfo:
    settings = settings or get_settings()
    if not settings.github_repo_full():
        raise GitHubError("GITHUB_OWNER / GITHUB_REPO are not configured.")
    data = _github_api(f"repos/{settings.github_repo_full()}")
    return RepoInfo(
        full_name=data.get("full_name", settings.github_repo_full()),
        default_branch=data.get("default_branch", "main"),
        private=bool(data.get("private", False)),
        description=data.get("description") or "",
        html_url=data.get("html_url", ""),
        clone_url=data.get("clone_url", f"https://github.com/{settings.github_repo_full()}.git"),
    )


def clone_or_pull(workspace: Workspace,
                  settings: Optional[Settings] = None) -> CommandResult:
    """Populate ``workspace`` from the configured repo.

    If the workspace already has a ``.git`` dir, runs ``git pull``; otherwise
    clones into the workspace root. Returns the CommandResult of the last op.

    v1.6.1: on runtimes without a git binary (e.g. Vercel serverless), falls
    back to Dulwich — a pure-Python git implementation — so clone/pull still
    work with full token authentication.
    """
    settings = settings or workspace.settings
    full = settings.github_repo_full()
    if not full:
        raise GitHubError("GITHUB_OWNER / GITHUB_REPO are not configured.")

    if not has_git_binary():
        if not dulwich_available():
            raise GitHubError(
                "git binary not found and dulwich not installed — cannot "
                "clone/pull on this runtime. Add 'dulwich' to requirements.")
        return dulwich_clone_or_pull(workspace, settings)

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

    Calls the GitHub REST API (``GET /user/repos``) using ``GITHUB_TOKEN``
    from the environment. Returns a list of dicts with name, full_name,
    private, description, default_branch, updated_at, and html_url.
    """
    data = _github_api(
        f"user/repos?per_page={min(max(limit, 1), 100)}&sort=updated&direction=desc",
        timeout=30,
    )
    if not isinstance(data, list):
        data = []
    repos = []
    for item in data[:limit]:
        repos.append({
            "name": item.get("name", ""),
            "full_name": item.get("full_name", ""),
            "private": bool(item.get("private", False)),
            "description": item.get("description") or "",
            "default_branch": item.get("default_branch", "main"),
            "updated_at": item.get("updated_at", ""),
            "html_url": item.get("html_url", ""),
        })
    return repos


def list_repo_issues(owner: str, repo: str,
                     state: str = "open",
                     limit: int = 20,
                     settings: Optional[Settings] = None) -> list:
    """List issues for ``owner/repo``.

    ``state`` can be ``open``, ``closed``, or ``all``. Calls the GitHub REST
    API (``GET /repos/{owner}/{repo}/issues``).
    """
    data = _github_api(
        f"repos/{owner}/{repo}/issues?state={state}&per_page={min(max(limit, 1), 100)}",
        timeout=30,
    )
    if not isinstance(data, list):
        data = []
    # The issues endpoint also returns PRs (they are issues too); filter those.
    issues = []
    for item in data[:limit]:
        if "pull_request" in item:
            continue
        user = item.get("user") or {}
        issues.append({
            "number": item.get("number", 0),
            "title": item.get("title", ""),
            "state": item.get("state", ""),
            "author": user.get("login", "") if isinstance(user, dict) else str(user),
            "created_at": item.get("created_at", ""),
            "updated_at": item.get("updated_at", ""),
            "url": item.get("html_url", ""),
            "labels": [label.get("name", "") if isinstance(label, dict) else str(label)
                       for label in (item.get("labels") or [])],
        })
    return issues


def list_repo_prs(owner: str, repo: str,
                  state: str = "open",
                  limit: int = 20,
                  settings: Optional[Settings] = None) -> list:
    """List pull requests for ``owner/repo``.

    ``state`` can be ``open``, ``closed``, or ``all``. Calls the GitHub REST
    API (``GET /repos/{owner}/{repo}/pulls``).
    """
    data = _github_api(
        f"repos/{owner}/{repo}/pulls?state={state}&per_page={min(max(limit, 1), 100)}",
        timeout=30,
    )
    if not isinstance(data, list):
        data = []
    prs = []
    for item in data[:limit]:
        user = item.get("user") or {}
        prs.append({
            "number": item.get("number", 0),
            "title": item.get("title", ""),
            "state": item.get("state", ""),
            "author": user.get("login", "") if isinstance(user, dict) else str(user),
            "head_branch": (item.get("head") or {}).get("ref", ""),
            "base_branch": (item.get("base") or {}).get("ref", ""),
            "created_at": item.get("created_at", ""),
            "updated_at": item.get("updated_at", ""),
            "url": item.get("html_url", ""),
            "is_draft": bool(item.get("draft", False)),
            "additions": item.get("additions", 0),
            "deletions": item.get("deletions", 0),
        })
    return prs


def get_repo_details(owner: str, repo: str,
                     settings: Optional[Settings] = None) -> dict:
    """Get detailed info about a single repo.

    Calls the GitHub REST API (``GET /repos/{owner}/{repo}``).
    """
    full = f"{owner}/{repo}"
    data = _github_api(f"repos/{full}")
    if not isinstance(data, dict):
        data = {}
    lang = data.get("language")
    return {
        "name": data.get("name", ""),
        "full_name": data.get("full_name", full),
        "private": bool(data.get("private", False)),
        "description": data.get("description") or "",
        "default_branch": data.get("default_branch", "main"),
        "html_url": data.get("html_url", ""),
        "stars": data.get("stargazers_count", 0),
        "forks": data.get("forks_count", 0),
        "language": lang if isinstance(lang, str) else (lang or ""),
        "created_at": data.get("created_at", ""),
        "updated_at": data.get("updated_at", ""),
    }


def list_repo_branches(owner: str, repo: str,
                       limit: int = 30,
                       settings: Optional[Settings] = None) -> list:
    """List branches for ``owner/repo``.

    Calls the GitHub REST API (``GET /repos/{owner}/{repo}/branches``).
    """
    data = _github_api(
        f"repos/{owner}/{repo}/branches?per_page={min(max(limit, 1), 100)}",
        timeout=30,
    )
    if not isinstance(data, list):
        data = []
    branches = []
    for item in data[:limit]:
        branches.append({
            "name": item.get("name", ""),
            "protected": bool(item.get("protected", False)),
        })
    return branches


def create_pull_request(workspace: Workspace, *,
                        title: Optional[str] = None,
                        body: Optional[str] = None,
                        settings: Optional[Settings] = None) -> dict:
    """Actually open a PR via the GitHub REST API.

    Called only on explicit user action. Uses ``POST /repos/{owner}/{repo}/pulls``.
    """
    settings = settings or workspace.settings
    prep = prepare_pull_request(workspace, title=title, body=body, settings=settings)
    head = prep["head"]
    if not head:
        raise GitHubError("No current branch; create a branch first.")
    owner = settings.github_owner or ""
    repo = settings.github_repo or ""
    if not (owner and repo):
        raise GitHubError("GITHUB_OWNER / GITHUB_REPO are not configured.")
    # Support "owner:branch" fork-head syntax.
    head_ref = head if ":" in head else f"{owner}:{head}"
    data = _github_api(
        f"repos/{owner}/{repo}/pulls",
        method="POST",
        body={
            "title": prep["title"],
            "body": prep["body"],
            "head": head_ref,
            "base": prep["base"],
        },
        timeout=60,
    )
    pr_url = data.get("html_url", "") if isinstance(data, dict) else ""
    return {"pr_url": pr_url, "prepared": prep}
