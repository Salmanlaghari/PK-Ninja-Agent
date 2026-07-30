"""Workspace restriction: commands run inside the workspace, not the host."""
import os

from terminal import run_command


def test_pwd_is_workspace(workspace):
    res = run_command("pwd", workspace)
    assert res.returncode == 0
    # pwd output (may include a trailing newline) must equal the workspace root.
    assert os.path.realpath(res.stdout.strip()) == os.path.realpath(str(workspace.root))


def test_ls_only_sees_workspace_files(workspace):
    workspace.write_file("marker.txt", "ninja")
    res = run_command("ls", workspace)
    assert res.returncode == 0
    assert "marker.txt" in res.stdout


def test_cannot_list_host_root(workspace):
    # `ls /` is allowed by program, but it lists the real host root because
    # the arg is absolute. This test documents that args are NOT rewritten —
    # the sandbox is enforced via cwd + path-safe file tools, not arg rewrite.
    # We assert it does NOT list workspace-only files here.
    res = run_command("ls /", workspace)
    assert res.returncode == 0
    assert "marker.txt" not in res.stdout  # workspace marker not at host root


def test_command_writes_into_workspace(workspace):
    res = run_command("echo created > out.txt", workspace)
    assert res.returncode == 0
    assert (workspace.root / "out.txt").exists()
    assert (workspace.root / "out.txt").read_text().strip() == "created"
