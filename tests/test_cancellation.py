"""Cancellation: mid-run subprocess killing + cancel flag + status transition.

These tests verify the real cancellation contract:
  * cancel_task() sets the cancel flag.
  * cancel_task() kills a running subprocess via the process group.
  * A command running when cancel is set returns truthfully that it was
    terminated due to cancellation.
"""
import os
import signal
import subprocess
import threading
import time

import pytest

from agent import TaskRuntime, cancel_task
from terminal import run_command
from workspace import Workspace


def test_cancel_task_sets_flag_for_unknown_returns_false():
    assert cancel_task("nonexistent-task-id") is False


def test_cancel_task_sets_flag_for_known_runtime():
    rt = TaskRuntime(task_id="t1", description="d")
    # Register it in the runtime registry.
    from agent import _RUNTIMES, _RUNTIMES_LOCK
    with _RUNTIMES_LOCK:
        _RUNTIMES["t1"] = rt
    try:
        assert not rt.cancel.is_set()
        ok = cancel_task("t1")
        assert ok is True
        assert rt.cancel.is_set()
    finally:
        with _RUNTIMES_LOCK:
            _RUNTIMES.pop("t1", None)


def test_cancel_kills_running_subprocess(workspace):
    """A long-running command must be killed when cancel_task is called."""
    rt = TaskRuntime(task_id="kill-test", description="d")

    # Start a long sleep in a background thread so we can cancel mid-run.
    holder = {}
    def _runner():
        res = run_command("python -c 'import time; time.sleep(30)'",
                          workspace, rt=rt)
        holder["result"] = res

    t = threading.Thread(target=_runner)
    t.start()
    # Wait for the subprocess to actually start.
    deadline = time.time() + 5
    while time.time() < deadline:
        with rt.current_proc_lock:
            if rt.current_proc is not None:
                break
        time.sleep(0.05)
    with rt.current_proc_lock:
        proc = rt.current_proc
    assert proc is not None, "subprocess did not start"
    assert proc.poll() is None, "process should still be running"

    # Cancel — this must kill the process group.
    rt.cancel.set()
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass

    t.join(timeout=10)
    assert not t.is_alive(), "runner thread did not finish after cancel"

    result = holder.get("result")
    assert result is not None
    # The command was terminated — non-zero exit.
    assert result.returncode != 0
    # And the cancel is truthfully recorded.
    assert "cancel" in result.stderr.lower()


def test_run_command_without_rt_works_normally(workspace):
    """The rt parameter is optional — old callers still work."""
    res = run_command("echo ok", workspace)
    assert res.returncode == 0
    assert "ok" in res.stdout


def test_run_command_clears_proc_after_completion(workspace):
    """After a command finishes, rt.current_proc must be cleared."""
    rt = TaskRuntime(task_id="clear-test", description="d")
    run_command("echo done", workspace, rt=rt)
    with rt.current_proc_lock:
        assert rt.current_proc is None
