"""Shared pytest configuration.

We point sys.path at the backend/ directory so tests can import modules
without package qualifiers, and we isolate each test under a temp workspace
and DB to avoid touching the real environment.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

# Use a per-run temp dir for workspaces + DB before any settings get cached.
_TMP = Path(tempfile.mkdtemp(prefix="pk_ninja_test_"))
os.environ.setdefault("WORKSPACE_ROOT", str(_TMP / "workspaces"))
os.environ.setdefault("DATABASE_PATH", str(_TMP / "test.db"))
os.environ.setdefault("AI_PROVIDER", "local")
# Ensure no GitHub creds leak into tests.
os.environ.pop("GITHUB_TOKEN", None)
os.environ.pop("GITHUB_OWNER", None)
os.environ.pop("GITHUB_REPO", None)

# Clear the lru_cache so settings pick up the test env.
from config import get_settings  # noqa: E402
get_settings.cache_clear()


@pytest.fixture
def ws_root(tmp_path):
    """A fresh workspace root per test."""
    root = tmp_path / "ws_root"
    root.mkdir()
    return root


@pytest.fixture
def workspace(ws_root):
    from workspace import Workspace
    return Workspace("test-task-1", root=ws_root)
