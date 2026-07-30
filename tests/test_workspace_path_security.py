"""File-path security: traversal and escape attempts must be rejected."""
import pytest

from workspace import Workspace, WorkspaceError


def test_normal_relative_path_ok(workspace):
    workspace.write_file("src/main.py", "print('hi')")
    p = workspace.safe_path("src/main.py")
    assert p.exists()


def test_absolute_path_treated_as_relative(workspace):
    workspace.write_file("a.txt", "x")
    # Leading slash is stripped and treated as relative to the workspace.
    p = workspace.safe_path("/a.txt")
    assert p.exists()


def test_parent_traversal_rejected(workspace):
    with pytest.raises(WorkspaceError):
        workspace.safe_path("../escape.txt")


def test_nested_parent_traversal_rejected(workspace):
    with pytest.raises(WorkspaceError):
        workspace.safe_path("src/../../escape.txt")


def test_empty_path_rejected(workspace):
    with pytest.raises(WorkspaceError):
        workspace.safe_path("")


def test_none_path_rejected(workspace):
    with pytest.raises(WorkspaceError):
        workspace.safe_path(None)


def test_backslash_traversal_rejected(workspace):
    with pytest.raises(WorkspaceError):
        workspace.safe_path("..\\..\\escape.txt")


def test_read_outside_workspace_rejected(workspace):
    workspace.write_file("inside.txt", "ok")
    # Attempt to read /etc/passwd via an absolute path is mapped into the
    # workspace, so it resolves to <root>/etc/passwd which does not exist
    # (not the real /etc/passwd).
    with pytest.raises(WorkspaceError):
        workspace.read_file("/etc/passwd")


def test_write_cannot_escape_workspace(workspace):
    # Writing to a deeply nested path is fine if it stays inside.
    workspace.write_file("a/b/c/d.txt", "ok")
    assert (workspace.root / "a/b/c/d.txt").exists()
    # But traversal is blocked before any disk write.
    with pytest.raises(WorkspaceError):
        workspace.write_file("../outside.txt", "nope")
    assert not (workspace.root.parent / "outside.txt").exists()


def test_delete_restricted_to_workspace(workspace):
    workspace.write_file("doomed.txt", "x")
    workspace.delete_file("doomed.txt")
    with pytest.raises(WorkspaceError):
        workspace.delete_file("../../..")
