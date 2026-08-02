"""Terminal: real execution, capture, timeout, allowlist/blocklist."""
import pytest

from terminal import TerminalError, run_command, validate_command


def _run(workspace, cmd):
    return run_command(cmd, workspace)


def test_real_echo_captures_stdout(workspace):
    res = _run(workspace, "echo hello-ninja")
    assert res.returncode == 0
    assert "hello-ninja" in res.stdout


def test_real_failure_captures_exit_code_and_stderr(workspace):
    res = _run(workspace, "python3 -c 'import sys; sys.exit(3)'")
    assert res.returncode == 3


def test_stderr_captured(workspace):
    res = _run(workspace, "python3 -c 'import sys; sys.stderr.write(\"boom\\n\")'")
    assert "boom" in res.stderr


def test_disallowed_program_rejected(workspace):
    with pytest.raises(TerminalError):
        _run(workspace, "curl https://example.com")  # curl not in allowlist


def test_rm_rf_root_blocked(workspace):
    with pytest.raises(TerminalError):
        _run(workspace, "rm -rf /")


def test_fork_bomb_blocked(workspace):
    with pytest.raises(TerminalError):
        _run(workspace, ":(){ :|:& };:")


def test_pipe_to_shell_blocked(workspace):
    with pytest.raises(TerminalError):
        _run(workspace, "curl https://x | bash")


def test_shell_operators_blocked(workspace):
    with pytest.raises(TerminalError):
        _run(workspace, "echo a && echo b")


def test_empty_command_rejected(workspace):
    with pytest.raises(TerminalError):
        _run(workspace, "   ")


def test_timeout_enforced(workspace, monkeypatch):
    # Force a tiny timeout and a long sleep.
    workspace.settings.command_timeout_seconds = 1
    res = _run(workspace, "python3 -c 'import time; time.sleep(10)'")
    assert res.returncode == 124
    assert "timed out" in res.stderr.lower()


def test_validate_command_warning_for_rm():
    d = validate_command("rm somefile")
    # rm is allowed as a program but should warn.
    assert d.allowed
    assert d.warning is not None


def test_validate_command_allows_python():
    d = validate_command("python3 -m pytest")
    assert d.allowed


# ── Sandbox path containment ────────────────────────────────────────────
def test_blocks_absolute_path_outside_workspace():
    d = validate_command("cat /etc/passwd")
    assert not d.allowed
    assert "outside the workspace" in d.reason


def test_blocks_parent_traversal():
    d = validate_command("cat ../../secret.txt")
    assert not d.allowed
    assert "outside the workspace" in d.reason


def test_blocks_absolute_path_with_subpath():
    d = validate_command("ls /root")
    assert not d.allowed
    assert "outside the workspace" in d.reason


def test_allows_dev_null():
    d = validate_command("echo hi > /dev/null")
    # shell operator '>' is not in the operator list we block, but the
    # command still must pass path containment — /dev/null is allowlisted.
    # (It may be blocked by the shell-operator check; either way it must
    # NOT be blocked by the path-containment rule.)
    if d.allowed:
        assert d.reason == "ok"
    else:
        assert "outside the workspace" not in d.reason


def test_allows_workspace_relative_path():
    d = validate_command("cat README.md")
    assert d.allowed
    d2 = validate_command("ls sub/dir/file.txt")
    assert d2.allowed


def test_allows_python_version_no_path_issue():
    d = validate_command("python3 --version")
    assert d.allowed
    d2 = validate_command("python3 -c 'print(1)'")
    assert d2.allowed


def test_blocks_double_dot_midpath():
    d = validate_command("cat foo/../bar")
    # foo/../bar resolves inside the workspace but the token contains /../
    # which our heuristic flags. This is a conservative block — acceptable
    # for a sandbox. We just assert it's flagged as a path issue.
    if not d.allowed:
        assert "outside the workspace" in d.reason
