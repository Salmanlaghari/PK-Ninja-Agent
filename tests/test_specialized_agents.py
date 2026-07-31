"""Tests for the 7 specialized agents and a full end-to-end coordinator run.

Every test uses a real temp workspace (via the existing ``workspace`` fixture)
and the offline local provider, so nothing is faked and no network/key is
required. The tests assert that each agent produces real, structured results
and that security (path containment, sandboxed commands) is preserved.
"""
import os
import threading

import pytest

from agents.base import AgentContext, AgentMessage, AgentRole, MessageRole
from agents.coordinator import AgentCoordinator
from agents.registry import get_registry

# Importing the package registers all 7 agents.
import agents  # noqa: F401
from agents.planner_agent import PlannerAgent
from agents.repository_agent import RepositoryAgent
from agents.coding_agent import CodingAgent
from agents.terminal_agent import TerminalAgent
from agents.testing_agent import TestingAgent
from agents.git_agent import GitAgent
from agents.review_agent import ReviewAgent

from ai_provider import get_provider
from config import get_settings


# ── Helpers ───────────────────────────────────────────────────────────────────
def _msg(recipient, content="go", sender=AgentRole.coordinator):
    return AgentMessage(sender=sender, recipient=recipient, content=content,
                        role=MessageRole.coordinator)


def _ctx(workspace, description="Document the project in the README.",
         provider=None, cancel=None, emit=None, **extra):
    s = get_settings()
    return AgentContext(
        task_id="spec-test",
        description=description,
        workspace=workspace,
        settings=s,
        provider=provider if provider is not None else get_provider(s),
        emit=emit if emit is not None else (lambda etype, msg, **d: None),
        cancel=cancel or threading.Event(),
        **extra,
    )


# ── Planner ───────────────────────────────────────────────────────────────────
def test_planner_provides_deterministic_plan_without_api_key(workspace):
    agent = PlannerAgent()
    ctx = _ctx(workspace, description="Add a README and docstrings to the project.")
    res = agent.run(ctx, _msg(AgentRole.planner))

    assert res.success is True
    assert res.next_agent == AgentRole.repository
    # The plan is structured with id/description/status/retries.
    assert len(ctx.plan) >= 3
    step = ctx.plan[0]
    assert {"id", "description", "status", "retries"} <= set(step.keys())
    # Keyword-driven: a README task adds a documentation step.
    assert any("documentation" in s["description"].lower() for s in ctx.plan)


def test_planner_plan_steps_are_unique_and_ordered(workspace):
    agent = PlannerAgent()
    ctx = _ctx(workspace, description="fix a bug in the parser")
    agent.run(ctx, _msg(AgentRole.planner))
    ids = [s["id"] for s in ctx.plan]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))


# ── Repository ────────────────────────────────────────────────────────────────
def test_repository_agent_indexes_and_lists_files(workspace):
    workspace.create_file("alpha.py", "def foo():\n    pass\n")
    workspace.create_file("beta.md", "# Beta\n")
    agent = RepositoryAgent()
    ctx = _ctx(workspace, description="understand the codebase")
    res = agent.run(ctx, _msg(AgentRole.repository))

    assert res.success is True
    assert res.next_agent == AgentRole.coding
    # The repository agent should have discovered real files in the workspace.
    discovered = ctx.scratch["repository"]["candidates"]
    assert "alpha.py" in discovered or "alpha.py" in [f["path"] for f in ctx.relevant_files]


def test_repository_agent_empty_workspace_succeeds_honestly(workspace):
    agent = RepositoryAgent()
    ctx = _ctx(workspace)
    res = agent.run(ctx, _msg(AgentRole.repository))
    assert res.success is True
    assert ctx.relevant_files == []


# ── Coding ────────────────────────────────────────────────────────────────────
def test_coding_agent_creates_readme_for_documentation_task(workspace):
    agent = CodingAgent()
    ctx = _ctx(workspace, description="Document the project in the README.")
    res = agent.run(ctx, _msg(AgentRole.coding))

    assert res.success is True
    assert res.next_agent == AgentRole.terminal
    assert len(ctx.edits) >= 1
    # A real README.md was created inside the (contained) workspace.
    paths = [e["path"] for e in ctx.edits]
    assert "README.md" in paths
    assert (workspace.root / "README.md").exists()
    content = (workspace.root / "README.md").read_text()
    assert "Project" in content


def test_coding_agent_rejects_path_traversal(workspace):
    """Security: the coding agent must not write outside the workspace."""
    from workspace import WorkspaceError

    agent = CodingAgent()
    ctx = _ctx(workspace, description="Document the project in the README.")
    # Manually feed an escape attempt through the same safe_path the agent uses.
    with pytest.raises(WorkspaceError):
        workspace.safe_path("../../etc/evil.txt")


def test_coding_agent_respects_cancellation(workspace):
    agent = CodingAgent()
    cancel = threading.Event()
    cancel.set()
    ctx = _ctx(workspace, cancel=cancel)
    res = agent.run(ctx, _msg(AgentRole.coding))
    assert res.success is False
    assert "cancel" in res.summary.lower()


# ── Terminal ──────────────────────────────────────────────────────────────────
def test_terminal_agent_runs_real_py_compile_on_python_edit(workspace):
    workspace.create_file("good.py", "x = 1\n")
    agent = TerminalAgent()
    ctx = _ctx(workspace, description="implement a feature")
    ctx.edits = [{"path": "good.py", "action": "write", "bytes": 7}]
    res = agent.run(ctx, _msg(AgentRole.terminal))

    assert res.success is True
    assert res.next_agent == AgentRole.testing
    results = ctx.scratch["terminal"]["results"]
    assert results, "terminal agent ran no commands"
    # Every result came from a real command (has a returncode).
    assert all("returncode" in r for r in results)


def test_terminal_agent_no_python_edits_skips_commands(workspace):
    agent = TerminalAgent()
    ctx = _ctx(workspace, description="document the readme")
    ctx.edits = [{"path": "README.md", "action": "create", "bytes": 10}]
    res = agent.run(ctx, _msg(AgentRole.terminal))
    assert res.success is True
    assert res.summary == "No command required."


def test_terminal_agent_rejects_unsafe_command_via_workspace(workspace):
    """Security sanity: the terminal layer blocks commands escaping the workspace."""
    from terminal import TerminalError, run_command

    with pytest.raises(TerminalError):
        run_command("cat /etc/passwd", workspace)


# ── Testing ───────────────────────────────────────────────────────────────────
def test_testing_agent_passes_when_code_compiles(workspace):
    workspace.create_file("ok.py", "def add(a, b):\n    return a + b\n")
    agent = TestingAgent()
    ctx = _ctx(workspace, description="add a feature")
    ctx.edits = [{"path": "ok.py"}]
    res = agent.run(ctx, _msg(AgentRole.testing))

    assert res.success is True
    assert res.next_agent == AgentRole.review
    assert ctx.test_report.get("success") is True


def test_testing_agent_fails_and_requests_coding_fix(workspace):
    workspace.create_file("broken.py", "def broken(:\n    pass\n")
    agent = TestingAgent()
    ctx = _ctx(workspace, description="fix the broken code")
    ctx.edits = [{"path": "broken.py"}]
    res = agent.run(ctx, _msg(AgentRole.testing))

    assert res.success is False
    assert res.next_agent == AgentRole.coding
    assert ctx.test_report.get("success") is False
    # The failure analysis identifies the failing file.
    assert "broken.py" in str(ctx.test_report) or "broken.py" in str(res.data)


# ── Review ────────────────────────────────────────────────────────────────────
def test_review_agent_flags_syntax_error_as_blocking(workspace):
    workspace.create_file("bad.py", "def broken(:\n    pass\n")
    agent = ReviewAgent()
    ctx = _ctx(workspace, description="fix the code")
    ctx.edits = [{"path": "bad.py"}]
    res = agent.run(ctx, _msg(AgentRole.review))

    assert res.success is False
    assert res.next_agent == AgentRole.coding
    assert len(ctx.review.get("errors", [])) >= 1


def test_review_agent_passes_clean_code(workspace):
    workspace.create_file("clean.py", "\"\"\"A clean module.\"\"\"\n\ndef f():\n    return 1\n")
    agent = ReviewAgent()
    ctx = _ctx(workspace, description="add a feature")
    ctx.edits = [{"path": "clean.py"}]
    res = agent.run(ctx, _msg(AgentRole.review))

    assert res.success is True
    assert res.next_agent == AgentRole.git
    assert ctx.review.get("errors") == []


# ── Git ───────────────────────────────────────────────────────────────────────
def test_git_agent_skips_non_repo_workspace(workspace):
    agent = GitAgent()
    ctx = _ctx(workspace, description="add a feature")
    res = agent.run(ctx, _msg(AgentRole.git))
    assert res.success is True
    assert res.data["git"] is False


def test_git_agent_commits_changes_in_real_repo(workspace):
    """Initialize a real git repo, make a change, and let the Git agent commit."""
    from terminal import run_command
    # git init inside the workspace (sandboxed).
    run_command("git init", workspace)
    run_command("git config user.email test@example.com", workspace)
    run_command("git config user.name Test", workspace)
    workspace.create_file("newfile.py", "x = 1\n")

    agent = GitAgent()
    ctx = _ctx(workspace, description="add a new feature file")
    res = agent.run(ctx, _msg(AgentRole.git))

    assert res.success is True
    # On the commit path the data carries a real branch name.
    assert res.data["branch"].startswith("pk-ninja/")
    assert res.data["changed"] == ["newfile.py"]
    assert res.data["pushed"] is False  # no credentials in tests
    # The branch was recorded in scratch for the integration layer.
    assert ctx.scratch["git"]["branch"] == res.data["branch"]
    # A real commit exists now.
    log_res = run_command("git log --oneline -1", workspace)
    assert log_res.returncode == 0
    assert "PK Ninja Agent" in log_res.stdout


# ── Full end-to-end coordinator run ───────────────────────────────────────────
def test_coordinator_full_run_on_real_repo(workspace):
    """All 7 agents run in sequence on a real git-initialized workspace."""
    from terminal import run_command
    run_command("git init", workspace)
    run_command("git config user.email test@example.com", workspace)
    run_command("git config user.name Test", workspace)
    workspace.create_file("app.py", "def main():\n    print('hi')\n")

    events = []
    ctx = _ctx(workspace, description="Document the project in the README.",
               emit=lambda etype, msg, **d: events.append((etype, msg)))
    coord = AgentCoordinator()
    res = coord.execute(ctx)

    assert res.success is True
    assert res.data["state"] == "done"
    # The coordinator populated the shared context via the real agents.
    assert len(ctx.plan) >= 1
    assert len(ctx.edits) >= 1
    # Real events streamed through the existing EventType vocabulary.
    assert events
    # The README was actually created on disk by the Coding agent.
    assert (workspace.root / "README.md").exists()
    # The Git agent committed the work on a real branch.
    assert ctx.scratch.get("git", {}).get("branch", "").startswith("pk-ninja/")


def test_all_seven_agents_are_registered():
    reg = get_registry()
    for role in (AgentRole.planner, AgentRole.repository, AgentRole.coding,
                 AgentRole.terminal, AgentRole.testing, AgentRole.git,
                 AgentRole.review):
        assert reg.get(role) is not None, f"{role.value} not registered"
