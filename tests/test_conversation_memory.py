"""Unit tests for the Conversation Memory system."""
import pytest
from backend.main import db_save_task_memory, db_get_task_memory, init_db
from backend.agent import Agent
import asyncio

@pytest.mark.asyncio
async def test_save_and_get_task_memory():
    await init_db()
    task_id = "test_memory_task_123"

    # Save memory
    await db_save_task_memory(
        task_id=task_id,
        task_context='{"user_preferences": "be precise"}',
        repo_context='{"main_entry": "main.py"}',
        analysis_summary='Found main.py and verified structure',
        plan_steps='[{"name": "step1", "status": "success"}]'
    )

    # Retrieve memory
    mem = await db_get_task_memory(task_id)
    assert mem is not None
    assert mem["task_id"] == task_id
    assert mem["analysis_summary"] == "Found main.py and verified structure"
    assert "precise" in mem["task_context"]
    assert "success" in mem["plan_steps"]
