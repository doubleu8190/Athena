"""Tests verifying the restructured graph_runtime can be built and imported."""

import asyncio
import pytest

from athena.runtime.graph_runtime import DEFAULT_SYSTEM_PROMPT
from athena.runtime.langgraph_graph import build_graph, invoke_graph
from athena.runtime.sub_agent import SubAgentManager, SubAgentResult
from athena.runtime.services.memory_service import MemoryService, _recent_history_by_turns, _MemoryRetrievalPlan
from athena.runtime.services.request_service import RequestService
from athena.runtime.services.execution_service import ExecutionService


class _RecordingGraph:
    def __init__(self):
        self.state = None
        self.config = None

    async def ainvoke(self, state, *, config):
        self.state = state
        self.config = config
        return {"result": {"content": "ok"}}


class TestModuleStructure:
    """Verify the new service-oriented module structure is intact."""

    def test_memory_service_exists(self):
        assert MemoryService is not None
        assert callable(MemoryService.build_memory_request)

    def test_request_service_exists(self):
        assert RequestService is not None
        assert hasattr(RequestService, "_build_harness_messages")

    def test_execution_service_exists(self):
        assert ExecutionService is not None
        assert callable(ExecutionService.result_payload)

    def test_sub_agent_extracted(self):
        assert SubAgentManager is not None
        assert SubAgentResult is not None

    def test_recent_history_keeps_six_turns(self):
        history = [{"role": "system", "content": "summary"}]
        for index in range(1, 8):
            history.extend([
                {"role": "user", "content": f"user-{index}"},
                {"role": "assistant", "content": f"assistant-{index}"},
                {"role": "tool", "content": f"tool-{index}"},
            ])
        recent = _recent_history_by_turns(history)
        assert recent[0] == {"role": "system", "content": "summary"}
        users = [item["content"] for item in recent if item["role"] == "user"]
        assert users == ["user-2", "user-3", "user-4", "user-5", "user-6", "user-7"]

    def test_memory_retrieval_plan_validates(self):
        plan = _MemoryRetrievalPlan(query="test query", task="test task", limit=5)
        assert plan.query == "test query"
        assert plan.limit == 5

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
