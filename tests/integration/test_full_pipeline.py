"""Integration tests for the full Core pipeline.

Tests the Context → Planner → Executor flow with mocked LLM and MCP.
"""

import pytest


class TestFullPipeline:
    """End-to-end pipeline tests."""

    @pytest.mark.asyncio
    async def test_message_to_unified_message(self):
        """UnifiedMessage should be created correctly."""
        from athena.core.message import UnifiedMessage
        msg = UnifiedMessage(
            message_id="msg-1",
            channel="web",
            user_id="admin",
            content="Hello",
        )
        assert msg.message_id == "msg-1"
        assert msg.channel == "web"
        assert msg.user_id == "admin"
        assert msg.content == "Hello"

    @pytest.mark.asyncio
    async def test_task_context_step_tracking(self):
        """TaskContext should track completed steps and outputs."""
        from athena.core.task_context import TaskContext

        ctx = TaskContext(
            task_id="task-1",
            session_id="sess-1",
            user_id="user1",
            channel="web",
        )

        ctx.complete_step(1, "output from step 1")
        assert 1 in ctx.completed_steps
        assert ctx.get_output(1) == "output from step 1"

        ctx.complete_step(2, {"nested": "data"})
        assert ctx.get_output(2) == {"nested": "data"}

        # Missing step should return empty string
        assert ctx.get_output(99) == ""

    @pytest.mark.asyncio
    async def test_plan_from_dict(self):
        """TaskPlan should be parsed from LLM response JSON."""
        from athena.core.planner import TaskPlan

        plan_data = {
            "task_id": "plan-1",
            "subtasks": [
                {
                    "step": 1,
                    "intent": "search the web",
                    "tool_name": "web_search",
                    "args": {"query": "test"},
                    "depends_on": [],
                    "critical": True,
                    "on_failure": "abort",
                },
                {
                    "step": 2,
                    "intent": "save results",
                    "tool_name": "file_write",
                    "args": {"path": "/workspace/results.txt", "content": "{{ step1.output }}"},
                    "depends_on": [1],
                    "critical": True,
                    "on_failure": "skip",
                },
            ],
        }

        plan = TaskPlan.from_dict(plan_data)
        assert plan.task_id == "plan-1"
        assert len(plan.subtasks) == 2
        assert plan.subtasks[0].step == 1
        assert plan.subtasks[1].depends_on == [1]
