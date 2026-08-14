"""Unit tests for Git branch management and file staging operations (Phase 6)."""
import os
import pytest
from fastapi.testclient import TestClient
from workspace import Workspace, WorkspaceError


@pytest.fixture
def client_with_task():
    from backend.main import app, init_db
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.run_until_complete(init_db())
    with TestClient(app) as c:
        # Create a task to initialize the workspace
        r = c.post("/api/tasks", json={"description": "test git workflow"})
        task_id = r.json()["task_id"]
        # Make it a git repo manually for testing
        import time
        time.sleep(0.5)
        from agent import get_runtime
        rt = get_runtime(task_id)
        ws = rt.workspace
        ws._git(["init"])
        # Configure test user
        ws._git(["config", "user.email", "ninja@pk.dev"])
        ws._git(["config", "user.name", "PK Ninja"])
        yield c, task_id, ws


def test_git_branch_listing_and_checkout(client_with_task):
    client, task_id, ws = client_with_task

    # Create dummy initial commit
    ws.write_file("init.txt", "initial file")
    ws.git_add_all()
    ws.git_commit("Initial commit")

    # List branches
    r = client.get(f"/api/git/branches?task_id={task_id}")
    assert r.status_code == 200
    assert "master" in r.json()["current"] or "main" in r.json()["current"]

    # Checkout new branch
    r_co = client.post("/api/git/checkout", json={"task_id": task_id, "branch": "feat/my-new-branch", "create": True})
    assert r_co.status_code == 200
    assert r_co.json()["success"] is True
    assert r_co.json()["branch"] == "feat/my-new-branch"

    # Verify current branch is updated
    r_list = client.get(f"/api/git/branches?task_id={task_id}")
    assert r_list.json()["current"] == "feat/my-new-branch"


def test_git_file_staging_and_unstaging(client_with_task):
    client, task_id, ws = client_with_task

    # Dummy initial commit to avoid git errors
    ws.write_file("init.txt", "initial")
    ws.git_add_all()
    ws.git_commit("Initial")

    # Create a changed file
    ws.write_file("hello.txt", "hello changed")

    # Verify initially unstaged
    assert "hello.txt" in ws.git_changed_files()
    assert not ws.git_diff(staged=True)

    # Stage file
    r_stage = client.post("/api/git/stage", json={"task_id": task_id, "path": "hello.txt"})
    assert r_stage.status_code == 200
    assert r_stage.json()["success"] is True

    # Verify staged diff
    assert "hello.txt" in ws.git_changed_files()
    assert ws.git_diff(staged=True) != ""

    # Unstage file
    r_unstage = client.post("/api/git/unstage", json={"task_id": task_id, "path": "hello.txt"})
    assert r_unstage.status_code == 200
    assert r_unstage.json()["success"] is True

    # Verify unstaged again
    assert not ws.git_diff(staged=True)


def test_git_discard_changes(client_with_task):
    client, task_id, ws = client_with_task

    ws.write_file("stable.txt", "original content")
    ws.git_add_all()
    ws.git_commit("Stable commit")

    # Modify it
    ws.write_file("stable.txt", "modified content")
    assert "modified content" in ws.read_file("stable.txt")

    # Discard changes
    r_disc = client.post("/api/git/discard", json={"task_id": task_id, "path": "stable.txt"})
    assert r_disc.status_code == 200
    assert r_disc.json()["success"] is True

    # Verify reverted
    assert "original content" in ws.read_file("stable.txt")


def test_git_security_traversal_protection(client_with_task):
    client, task_id, ws = client_with_task

    # Attempt path traversal on stage
    r = client.post("/api/git/stage", json={"task_id": task_id, "path": "../main.py"})
    assert r.status_code == 400
    assert "escapes workspace" in r.text or "Parent references" in r.text
