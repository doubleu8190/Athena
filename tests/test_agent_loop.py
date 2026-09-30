"""Tests verifying the restructured graph_runtime can be built and imported."""

import asyncio
import pytest

from athena.runtime.langgraph_runtime import DEFAULT_SYSTEM_PROMPT
from athena.runtime.agent_graph import build_graph, invoke_graph
from athena.runtime.services.session_context_service import SessionContextService
from athena.runtime.services.agent_execution_service import AgentExecutionService


class _RecordingGraph:
    def __init__(self):
        self.state = None
        self.config = None

    async def ainvoke(self, state, *, config):
        self.state = state
        self.config = config
        return {"response": {"result": {"content": "ok"}}}


class TestModuleStructure:
    """Verify the new service-oriented module structure is intact."""

    def test_task_understanding_module_exists(self):
        from athena.runtime.task_understanding import TaskUnderstandingService

        assert TaskUnderstandingService is not None
        assert callable(TaskUnderstandingService.understand)

    def test_session_context_service_exists(self):
        assert SessionContextService is not None
        assert hasattr(SessionContextService, "_build_harness_messages")

    def test_agent_execution_service_exists(self):
        assert AgentExecutionService is not None
        assert callable(AgentExecutionService.build_result_payload)

    def test_default_system_prompt_accessible(self):
        assert DEFAULT_SYSTEM_PROMPT is not None
        assert len(DEFAULT_SYSTEM_PROMPT) > 0


@pytest.mark.asyncio
async def test_invoke_graph_uses_run_id_as_thread_id():
    graph = _RecordingGraph()
    stop_signal = asyncio.Event()
    result = await invoke_graph(
        graph,
        session_id="session-1",
        run_id="run-1",
        user_message="hello",
        stop_signal=stop_signal,
    )
    assert result == {"content": "ok"}
    assert graph.config["configurable"]["thread_id"] == "run-1"
    assert graph.config["configurable"]["stop_signal"] is stop_signal


@pytest.mark.asyncio
async def test_invoke_graph_requires_run_id():
    with pytest.raises(ValueError, match="run_id is required"):
        await invoke_graph(_RecordingGraph(), session_id="session-1", user_message="hello")
