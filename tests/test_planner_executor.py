"""Unit tests for the Planner Engine and Task Executor."""
import pytest
from backend.agent import Agent, TaskRuntime
from backend.workspace import Workspace
from backend.ai_provider import LocalProvider
import shutil

@pytest.fixture
def temp_workspace(tmp_path) -> Workspace:
    from backend.config import Settings
    settings = Settings(workspace_root=str(tmp_path), database_path=str(tmp_path / "test.db"))
    ws = Workspace("test_task_planner", root=tmp_path, settings=settings)
    return ws


def test_planner_engine_generates_structured_steps(temp_workspace):
    ws = temp_workspace
    agent = Agent("test_task_planner", "Add module docstrings to python files missing them", settings=ws.settings)
    rt = agent.rt
    rt.workspace = ws

    # Deterministic LocalProvider plan
    plan = agent.provider.plan(agent.description, "")
    assert len(plan.steps) > 0

    # Structure them
    rt.plan_steps = [
        {"id": i + 1, "description": step, "status": "pending", "retries": 0}
        for i, step in enumerate(plan.steps)
    ]

    assert rt.plan_steps[0]["status"] == "pending"
    assert rt.plan_steps[0]["retries"] == 0
    assert "docstring" in rt.plan_steps[0]["description"].lower() or "find" in rt.plan_steps[0]["description"].lower()


@pytest.mark.asyncio
async def test_task_executor_recoverable_failure_and_retry(temp_workspace):
    ws = temp_workspace
    agent = Agent("test_task_planner", "Add module docstrings", settings=ws.settings)
    rt = agent.rt
    rt.workspace = ws

    # Initialize DB tables for memory persistence
    import aiosqlite
    async with aiosqlite.connect(ws.settings.database_path) as conn:
        await conn.executescript(
            "CREATE TABLE IF NOT EXISTS task_memory (task_id TEXT PRIMARY KEY, task_context TEXT, repo_context TEXT, analysis_summary TEXT, plan_steps TEXT, updated_at TEXT);"
        )
        await conn.commit()

    step = {"id": 1, "description": "Run testing verification", "status": "pending", "retries": 0}
    rt.plan_steps = [step]

    # Mock tool selection to fail with a recoverable exception (e.g. ValueError) on the first call, then succeed on retry!
    call_count = 0
    def mock_execute_step_tools(step, rt, ws, file_objs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ValueError("Recoverable verification failure")
        # second call succeeds
        return

    agent._execute_step_tools = mock_execute_step_tools

    # Execute plan steps
    agent._execute_plan_steps(rt, ws, [])

    assert call_count == 2
    assert rt.plan_steps[0]["status"] == "success"
    assert rt.plan_steps[0]["retries"] == 1


@pytest.mark.asyncio
async def test_task_executor_unrecoverable_failure_blocks_retry(temp_workspace):
    ws = temp_workspace
    agent = Agent("test_task_planner", "Add module docstrings", settings=ws.settings)
    rt = agent.rt
    rt.workspace = ws

    # Initialize DB tables
    import aiosqlite
    async with aiosqlite.connect(ws.settings.database_path) as conn:
        await conn.executescript(
            "CREATE TABLE IF NOT EXISTS task_memory (task_id TEXT PRIMARY KEY, task_context TEXT, repo_context TEXT, analysis_summary TEXT, plan_steps TEXT, updated_at TEXT);"
        )
        await conn.commit()

    step = {"id": 1, "description": "Run destructive command", "status": "pending", "retries": 0}
    rt.plan_steps = [step]

    # Mock tool selection to fail with an unsafe "blocked" exception
    def mock_execute_step_tools(step, rt, ws, file_objs):
        raise ValueError("Command was blocked by safety policy")

    agent._execute_step_tools = mock_execute_step_tools

    with pytest.raises(ValueError, match="blocked"):
        agent._execute_plan_steps(rt, ws, [])

    # Unsafe operation should fail immediately, and not retry
    assert rt.plan_steps[0]["status"] == "failed"
    assert rt.plan_steps[0]["retries"] == 0


@pytest.mark.asyncio
async def test_tool_execution_dispatching(temp_workspace):
    ws = temp_workspace
    agent = Agent("test_task_planner", "Verify tools", settings=ws.settings)
    rt = agent.rt
    rt.workspace = ws

    # Test file read/write tools
    res_write = agent._run_local_tool("write_file", {"path": "hello.py", "content": "print('hello')\n"}, ws, rt)
    assert res_write["success"] is True

    res_read = agent._run_local_tool("read_file", {"path": "hello.py"}, ws, rt)
    assert "print('hello')" in res_read["content"]

    # Test search files tool
    res_search = agent._run_local_tool("search_files", {"pattern": "*.py"}, ws, rt)
    assert len(res_search) > 0
    assert any(x["path"] == "hello.py" for x in res_search)
