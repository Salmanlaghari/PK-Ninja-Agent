"""Tests for the GitHub REST API integration (v1.4.0).

These verify that the GitHub functions work WITHOUT the ``gh`` CLI, by
talking to the GitHub REST API via ``urllib.request``. We mock
``urllib.request.urlopen`` so no real network calls are made.
"""
import io
import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest


class _FakeResp:
    """A minimal stand-in for an HTTPResponse returned by urlopen."""

    def __init__(self, payload, status=200):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        self._buf = io.BytesIO(payload.encode("utf-8"))
        self.status = status

    def read(self):
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._buf.close()
        return False


@contextmanager
def _fake_urlopen(payload, status=200):
    """Patch urllib.request.urlopen to yield a fixed payload."""
    with patch("urllib.request.urlopen", return_value=_FakeResp(payload, status)) as m:
        yield m


# ---------------------------------------------------------------------------
# _github_api helper
# ---------------------------------------------------------------------------

def test_github_api_raises_without_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    from github import _github_api, GitHubError
    with pytest.raises(GitHubError, match="No GitHub token"):
        _github_api("user/repos")


def test_github_api_returns_parsed_json(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test123")
    from github import _github_api
    with _fake_urlopen([{"login": "octocat"}]):
        result = _github_api("user/repos")
    assert result == [{"login": "octocat"}]


def test_github_api_raises_on_http_error(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test123")
    import urllib.error
    from github import _github_api, GitHubError

    def _raise(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url, 404, "Not Found", {}, io.BytesIO(b'{"message":"not found"}')
        )

    with patch("urllib.request.urlopen", side_effect=_raise):
        with pytest.raises(GitHubError, match="HTTP 404"):
            _github_api("repos/x/y")


# ---------------------------------------------------------------------------
# list_user_repos
# ---------------------------------------------------------------------------

def test_list_user_repos_no_gh_cli(monkeypatch):
    """list_user_repos must work even though gh CLI is absent."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test123")
    payload = [
        {"name": "hello", "full_name": "octocat/hello", "private": False,
         "description": "A demo", "default_branch": "main",
         "updated_at": "2024-01-01T00:00:00Z",
         "html_url": "https://github.com/octocat/hello"},
        {"name": "secret", "full_name": "octocat/secret", "private": True,
         "description": None, "default_branch": "master",
         "updated_at": "2024-02-01T00:00:00Z",
         "html_url": "https://github.com/octocat/secret"},
    ]
    from github import list_user_repos
    with _fake_urlopen(payload):
        repos = list_user_repos(limit=10)
    assert len(repos) == 2
    assert repos[0]["name"] == "hello"
    assert repos[0]["full_name"] == "octocat/hello"
    assert repos[0]["private"] is False
    assert repos[1]["private"] is True
    assert repos[1]["description"] == ""


def test_list_user_repos_respects_limit(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test123")
    payload = [{"name": f"r{i}", "full_name": f"u/r{i}", "private": False,
                "description": "", "default_branch": "main",
                "updated_at": "", "html_url": ""} for i in range(50)]
    from github import list_user_repos
    with _fake_urlopen(payload):
        repos = list_user_repos(limit=5)
    assert len(repos) == 5


# ---------------------------------------------------------------------------
# list_repo_issues (filters out PRs)
# ---------------------------------------------------------------------------

def test_list_repo_issues_filters_prs(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test123")
    payload = [
        {"number": 1, "title": "Real issue", "state": "open",
         "user": {"login": "alice"}, "created_at": "", "updated_at": "",
         "html_url": "https://github.com/x/y/issues/1", "labels": []},
        {"number": 2, "title": "Actually a PR", "state": "open",
         "user": {"login": "bob"}, "pull_request": {},
         "created_at": "", "updated_at": "",
         "html_url": "https://github.com/x/y/pull/2", "labels": []},
    ]
    from github import list_repo_issues
    with _fake_urlopen(payload):
        issues = list_repo_issues("x", "y")
    assert len(issues) == 1
    assert issues[0]["number"] == 1
    assert issues[0]["title"] == "Real issue"
    assert issues[0]["author"] == "alice"


# ---------------------------------------------------------------------------
# list_repo_prs
# ---------------------------------------------------------------------------

def test_list_repo_prs(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test123")
    payload = [
        {"number": 42, "title": "Add feature", "state": "open",
         "user": {"login": "carol"}, "head": {"ref": "feature"},
         "base": {"ref": "main"}, "draft": True, "additions": 10,
         "deletions": 2, "created_at": "", "updated_at": "",
         "html_url": "https://github.com/x/y/pull/42"},
    ]
    from github import list_repo_prs
    with _fake_urlopen(payload):
        prs = list_repo_prs("x", "y")
    assert len(prs) == 1
    assert prs[0]["number"] == 42
    assert prs[0]["head_branch"] == "feature"
    assert prs[0]["base_branch"] == "main"
    assert prs[0]["is_draft"] is True
    assert prs[0]["additions"] == 10


# ---------------------------------------------------------------------------
# get_repo_details
# ---------------------------------------------------------------------------

def test_get_repo_details(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test123")
    payload = {
        "name": "hello", "full_name": "octocat/hello", "private": False,
        "description": "A demo", "default_branch": "main",
        "html_url": "https://github.com/octocat/hello",
        "stargazers_count": 99, "forks_count": 12, "language": "Python",
        "created_at": "2020-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }
    from github import get_repo_details
    with _fake_urlopen(payload):
        details = get_repo_details("octocat", "hello")
    assert details["name"] == "hello"
    assert details["stars"] == 99
    assert details["language"] == "Python"
    assert details["default_branch"] == "main"


# ---------------------------------------------------------------------------
# list_repo_branches
# ---------------------------------------------------------------------------

def test_list_repo_branches(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test123")
    payload = [
        {"name": "main", "protected": True, "commit": {}},
        {"name": "dev", "protected": False, "commit": {}},
    ]
    from github import list_repo_branches
    with _fake_urlopen(payload):
        branches = list_repo_branches("x", "y")
    assert len(branches) == 2
    assert branches[0]["name"] == "main"
    assert branches[0]["protected"] is True
    assert branches[1]["protected"] is False


# ---------------------------------------------------------------------------
# repo_info (uses REST API, not gh)
# ---------------------------------------------------------------------------

def test_repo_info_via_rest(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test123")
    monkeypatch.setenv("GITHUB_OWNER", "octocat")
    monkeypatch.setenv("GITHUB_REPO", "hello")
    from config import get_settings
    get_settings.cache_clear()
    payload = {
        "full_name": "octocat/hello", "default_branch": "main",
        "private": False, "description": "A demo",
        "html_url": "https://github.com/octocat/hello",
        "clone_url": "https://github.com/octocat/hello.git",
    }
    from github import repo_info
    with _fake_urlopen(payload):
        info = repo_info()
    assert info.full_name == "octocat/hello"
    assert info.default_branch == "main"
    assert info.private is False
    get_settings.cache_clear()
