"""Tests for individual graph node implementations."""

import pytest
from unittest.mock import MagicMock



# ── initialize_node ──────────────────────────────────────────────────────

class TestInitializeNode:
    @pytest.mark.asyncio
    async def test_initializes_from_config(self):
        from athena.core.graph.nodes.initialize import initialize_node

        config = {
            "configurable": {
                "session_id": "sess-1",
                "user_id": "user-1",
                "channel": "web",
                "session_context": MagicMock(),
            }
        }
        state = {"messages": []}

        result = await initialize_node(state, config)
        assert result["status"] == "planning"
        assert result["task_plan"] is None
        assert result["plan_error"] is None
        assert result["step_outputs"] == {}
        assert result["completed_steps"] == []
        assert result["circuit_breaker_count"] == 0

    @pytest.mark.asyncio
    async def test_deduces_user_from_session_context(self):
        from athena.core.graph.nodes.initialize import initialize_node

        session_ctx = MagicMock()
        session_ctx.user_id = "ctx-user"
        session_ctx.channel = "telegram"

        config = {
            "configurable": {
                "session_id": "sess-2",
                "session_context": session_ctx,
            }
        }
        state = {"messages": []}

        result = await initialize_node(state, config)
        assert result["user_id"] == "ctx-user"
        assert result["channel"] == "telegram"


# ── collect_node ─────────────────────────────────────────────────────────

class TestCollectNode:
    @pytest.mark.asyncio
    async def test_collects_success(self):
        from athena.core.graph.nodes.collect import collect_node

        state = {
            "subtask_result": {
                "step": 1,
                "status": "success",
                "output": "raw data",
                "normalized_output": {"key": "value"},
            },
        }
        config = {"configurable": {}}

        result = await collect_node(state, config)
        assert result["completed_steps"] == [1]
        assert result["step_outputs"] == {"step1": {"key": "value"}}
        assert result["subtask_result"] is None

    @pytest.mark.asyncio
    async def test_collects_skipped(self):
        from athena.core.graph.nodes.collect import collect_node

        state = {
            "subtask_result": {
                "step": 2,
                "status": "skipped",
            },
        }
        config = {"configurable": {}}

        result = await collect_node(state, config)
        assert result["completed_steps"] == [2]

    @pytest.mark.asyncio
    async def test_collects_abort(self):
        from athena.core.graph.nodes.collect import collect_node

        state = {
            "subtask_result": {
                "step": 3,
                "status": "abort",
                "error": "something broke",
            },
        }
        config = {"configurable": {}}

        result = await collect_node(state, config)
        assert result["failed_steps"] == [3]

    @pytest.mark.asyncio
    async def test_collects_user_rejected(self):
        from athena.core.graph.nodes.collect import collect_node

        state = {
            "subtask_result": {
                "step": 1,
                "status": "user_rejected",
            },
        }
        config = {"configurable": {}}

        result = await collect_node(state, config)
        assert result["failed_steps"] == [1]

    @pytest.mark.asyncio
    async def test_no_subtask_result(self):
        from athena.core.graph.nodes.collect import collect_node

        state = {"subtask_result": None}
        config = {"configurable": {}}

        result = await collect_node(state, config)
        assert result == {}


# ── handle_failure_node ──────────────────────────────────────────────────

class TestHandleFailureNode:
    @pytest.mark.asyncio
    async def test_abort_strategy(self):
        from athena.core.graph.nodes.handle_failure import handle_failure_node

        state = {
            "subtask_result": {
                "step": 1,
                "status": "failed",
                "error": "test error",
                "on_failure": "abort",
                "critical": True,
            },
            "circuit_breaker_count": 0,
        }
        config = {"configurable": {}}

        result = await handle_failure_node(state, config)
        assert result["subtask_result"]["status"] == "abort"
        assert result["circuit_breaker_count"] == 1

    @pytest.mark.asyncio
    async def test_skip_strategy_non_critical(self):
        from athena.core.graph.nodes.handle_failure import handle_failure_node

        state = {
            "subtask_result": {
                "step": 1,
                "status": "failed",
                "on_failure": "skip",
                "critical": False,
            },
            "circuit_breaker_count": 0,
        }
        config = {"configurable": {}}

        result = await handle_failure_node(state, config)
        assert result["subtask_result"]["status"] == "skipped"
        assert result.get("circuit_breaker_count", 0) == 0

    @pytest.mark.asyncio
    async def test_skip_strategy_critical_upgrades_to_abort(self):
        from athena.core.graph.nodes.handle_failure import handle_failure_node

        state = {
            "subtask_result": {
                "step": 1,
                "status": "failed",
                "on_failure": "skip",
                "critical": True,
            },
            "circuit_breaker_count": 0,
        }
        config = {"configurable": {}}

        result = await handle_failure_node(state, config)
        assert result["subtask_result"]["status"] == "abort"
        assert result["circuit_breaker_count"] == 1

    @pytest.mark.asyncio
    async def test_fallback_with_static_fallback_tool(self):
        from athena.core.graph.nodes.handle_failure import handle_failure_node

        state = {
            "subtask_result": {
                "step": 1,
                "status": "failed",
                "on_failure": "fallback",
                "fallback_tool": {"tool_name": "backup_tool"},
                "rendered_args": {"path": "/tmp/x"},
            },
            "circuit_breaker_count": 0,
        }
        config = {
            "configurable": {
                "tool_registry": MagicMock(),
                "mcp_client": MagicMock(),
            }
        }

        result = await handle_failure_node(state, config)
        assert result["subtask_result"]["status"] == "fallback"

    @pytest.mark.asyncio
    async def test_unknown_strategy_becomes_abort(self):
        from athena.core.graph.nodes.handle_failure import handle_failure_node

        state = {
            "subtask_result": {
                "step": 1,
                "status": "failed",
                "on_failure": "unknown_strategy",
            },
            "circuit_breaker_count": 1,
        }
        config = {"configurable": {}}

        result = await handle_failure_node(state, config)
        assert result["subtask_result"]["status"] == "abort"
        assert result["circuit_breaker_count"] == 2


# ── finalize_node ────────────────────────────────────────────────────────

class TestFinalizeNode:
    @pytest.mark.asyncio
    async def test_finalize_completed(self):
        from athena.core.graph.nodes.finalize import finalize_node

        session_ctx = MagicMock()
        context_mgr = MagicMock()
        context_mgr.config.sqlite_db_path = ":memory:"

        state = {
            "task_plan": {"task_id": "task-1", "subtasks": []},
            "completed_steps": [1, 2, 3],
            "failed_steps": [],
            "circuit_breaker_count": 0,
        }
        config = {
            "configurable": {
                "context_manager": context_mgr,
                "session_context": session_ctx,
            }
        }

        # Should not raise
        result = await finalize_node(state, config)
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_finalize_circuit_broken(self):
        from athena.core.graph.nodes.finalize import finalize_node

        session_ctx = MagicMock()
        context_mgr = MagicMock()
        context_mgr.config.sqlite_db_path = ":memory:"

        state = {
            "task_plan": {"task_id": "task-2", "subtasks": []},
            "completed_steps": [],
            "failed_steps": [1, 2, 3],
            "circuit_breaker_count": 3,
        }
        config = {
            "configurable": {
                "context_manager": context_mgr,
                "session_context": session_ctx,
            }
        }

        result = await finalize_node(state, config)
        assert result["status"] == "failed"
