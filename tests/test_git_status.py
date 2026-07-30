"""Git status helper works inside an initialized workspace."""
import subprocess

from workspace import Workspace


def _init_repo(workspace: Workspace):
    subprocess.run(["git", "init", "-q"], cwd=str(workspace.root), check=True)
    subprocess.run(["git", "config", "user.email", "test@ninja"], cwd=str(workspace.root), check=True)
    subprocess.run(["git", "config", "user.name", "Ninja Test"], cwd=str(workspace.root), check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=str(workspace.root), check=True)


def test_git_status_empty(workspace):
    _init_repo(workspace)
    assert workspace.git_status() == ""
    assert workspace.git_changed_files() == []


def test_git_status_shows_new_file(workspace):
    _init_repo(workspace)
    workspace.write_file("new.py", "x = 1")
    status = workspace.git_status()
    assert "new.py" in status
    assert "new.py" in workspace.git_changed_files()


def test_git_diff_shows_changes(workspace):
    _init_repo(workspace)
    workspace.write_file("a.py", "x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=str(workspace.root), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(workspace.root), check=True)
    workspace.write_file("a.py", "x = 2\n")
    diff = workspace.git_diff()
    assert "-x = 1" in diff
    assert "+x = 2" in diff


def test_create_branch_and_commit(workspace):
    _init_repo(workspace)
    workspace.write_file("a.py", "x = 1\n")
    workspace.git_add_all()
    workspace.git_commit("init")
    res = workspace.create_branch("feature/x")
    assert res.success
    assert workspace.git_current_branch() == "feature/x"


def test_invalid_branch_name_rejected(workspace):
    _init_repo(workspace)
    import pytest
    from workspace import WorkspaceError
    with pytest.raises(WorkspaceError):
        workspace.create_branch("bad name with spaces")


def test_current_branch_none_on_detached_or_empty(workspace):
    _init_repo(workspace)
    # Fresh repo with no commits: rev-parse returns 'HEAD' -> None.
    assert workspace.git_current_branch() is None
