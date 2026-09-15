"""编排与 LangGraphRuntime 之间的边界行为测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from athena.infrastructure.sqlite.database import Database


class _UnsupportedLLM:
    async def ainvoke(self, messages):
        return type("Response", (), {"content": "ok"})()


@pytest.mark.asyncio
async def test_runtime_orchestrate_runs_full_pipeline(tmp_path) -> None:
    from athena.runtime.graph_runtime import LangGraphRuntime
    from athena.runtime.orchestration import ExecutionPlan, TaskSpec

    database = Database(str(tmp_path / "runtime.db"))
    await database.connect()

    session = await database.sessions.create("session-1", "Orchestrate")
    plan = ExecutionPlan(
        plan_id="plan-1",
        root_run_id="root-1",
        goal="检查模块",
        tasks=[
            TaskSpec(
                task_id="task-1",
                plan_id="plan-1",
                title="检查模块",
                objective="返回模块检查结果",
                expected_output={"type": "object"},
            )
        ],
    )

    fake_llm = AsyncMock()
    fake_llm.ainvoke.return_value = type("Response", (), {"content": "汇总结果"})()
    fake_llm.fallback_providers = []

    class _Worker:
        async def execute(self, **kwargs):
            from athena.runtime.orchestration import WorkerResult

            return WorkerResult(
                plan_id=kwargs["task"].plan_id,
                task_id=kwargs["task"].task_id,
                run_id=kwargs["run_id"],
                status="completed",
                output={"summary": "ok"},
            )

    runtime = LangGraphRuntime.__new__(LangGraphRuntime)
    runtime._db = database
    runtime._tool_manager = type("M", (), {"list_names": lambda self: []})()
    runtime._agent_store = AsyncMock()
    runtime._events = AsyncMock()
    runtime._llm = fake_llm
    runtime._settings = type("S", (), {"max_turns_per_run": 1, "retry_budget": 1})()
    runtime._session_stop_signals = {}

    from athena.runtime.orchestration import Dispatcher, Planner, StructuredLLMService, Synthesizer
    from athena.runtime.orchestration.worker import WorkerExecutor

    structured = StructuredLLMService(fake_llm)
    from athena.runtime.orchestration.events import OrchestrationEventPublisher

    runtime._orchestration_planner = Planner(
        database, OrchestrationEventPublisher(runtime._events)
    )
    runtime._orchestration_worker = _Worker()
    runtime._orchestration_dispatcher = Dispatcher(
        database,
        runtime._orchestration_worker,
        OrchestrationEventPublisher(runtime._events),
    )
    runtime._orchestration_events = OrchestrationEventPublisher(runtime._events)
    runtime._orchestration_synthesizer = Synthesizer(fake_llm)

    materialized = await runtime.materialize_plan(
        session_id="session-1",
        root_run_id="root-1",
        user_goal="检查模块",
        submission={
            "plan_id": "plan-1",
            "tasks": [
                {
                    "task_id": "task-1",
                    "title": "检查模块",
                    "objective": "返回模块检查结果",
                }
            ],
        },
    )
    result = await runtime.execute_planned_orchestration(
        plan_payload=materialized.model_dump(mode="json"),
        session_id="session-1",
    )

    assert result["content"] == "汇总结果"
    assert result["plan_id"] == "plan-1"
    assert "task-1" in result["results"]
    await database.close()


@pytest.mark.asyncio
async def test_main_graph_has_orchestration_branch() -> None:
    """主图必须包含 Planner 分支、计划执行和编排汇总节点。"""

    from unittest.mock import MagicMock

    from athena.runtime.graph_runtime import LangGraphRuntime
    from athena.runtime.langgraph_graph import build_graph

    runtime = LangGraphRuntime.__new__(LangGraphRuntime)
    runtime._request_service = MagicMock()
    runtime._execution_service = MagicMock()
    runtime._memory_service = MagicMock()
    runtime._file_runtime = MagicMock()
    runtime._file_parse_semaphore = None
    runtime._file_embedding_semaphore = None

    graph = build_graph(runtime)
    nodes = set(graph.get_graph().nodes)
    assert {
        "agent_loop",
        "materialize_plan",
        "execute_plan",
        "synthesize_orchestration",
    } <= nodes
    assert "plan_orchestration" not in nodes


def test_first_agent_turn_has_three_routes() -> None:
    from athena.runtime.execution_loop.graph import _route_after_llm

    assert _route_after_llm({"execution": {"final_content": "回答"}}) == "finish_execution"
    assert _route_after_llm({"execution": {"pending_tool_calls": [{"name": "x"}]}}) == "execute_tool_batch"
    assert _route_after_llm({"execution": {"route": "plan_requested"}}) == "plan_requested"


def test_non_retryable_llm_error_finishes_without_another_llm_call() -> None:
    from athena.runtime.execution_loop.graph import _route_after_llm

    assert _route_after_llm(
        {"execution": {"error": "invalid submit_plan", "retryable": False}}
    ) == "finish_execution"


def test_retryable_llm_error_can_retry_within_budgets() -> None:
    from athena.runtime.execution_loop.graph import _route_after_llm

    assert _route_after_llm(
        {
            "execution": {
                "error": "temporary failure",
                "retryable": True,
                "turn_count": 1,
                "retry_count": 1,
                "max_turns": 3,
                "max_retries": 2,
            }
        }
    ) == "llm_call"


def test_plan_submission_is_exposed_on_every_top_level_turn() -> None:
    from athena.core.harness.turn_executor import HarnessTurnExecutor
    from athena.runtime.orchestration import PLAN_SUBMISSION_TOOL_NAME

    executor = HarnessTurnExecutor.__new__(HarnessTurnExecutor)
    executor._tool_manager = type(
        "Tools",
        (),
        {"get_langchain_tools": lambda self, names=None: []},
    )()

    first_turn = executor._get_llm_tools(
        tool_names=None, parent_run_id=None
    )
    later_turn = executor._get_llm_tools(
        tool_names=None, parent_run_id=None
    )
    worker_turn = executor._get_llm_tools(
        tool_names=None, parent_run_id="parent-run-1"
    )

    assert [tool.name for tool in first_turn] == [PLAN_SUBMISSION_TOOL_NAME]
    assert [tool.name for tool in later_turn] == [PLAN_SUBMISSION_TOOL_NAME]
    assert worker_turn == []
