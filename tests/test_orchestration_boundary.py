"""编排与 LangGraphRuntime 之间的边界行为测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from athena.infrastructure.postgre.database import Database


class _UnsupportedLLM:
    async def ainvoke(self, messages):
        return type("Response", (), {"content": "ok"})()


@pytest.mark.asyncio
async def test_runtime_orchestrate_runs_full_pipeline(tmp_path) -> None:
    from athena.runtime.langgraph_runtime import LangGraphRuntime
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

    from athena.runtime.orchestration import (
        PlanDispatcher,
        PlanMaterializer,
        PlanResultSynthesizer,
        StructuredLLMService,
    )
    from athena.runtime.orchestration.worker import WorkerExecutor

    structured = StructuredLLMService(fake_llm)
    from athena.runtime.orchestration.events import OrchestrationEventPublisher

    runtime._plan_materializer = PlanMaterializer(
        database, OrchestrationEventPublisher(runtime._events)
    )
    runtime._worker_executor = _Worker()
    runtime._plan_dispatcher = PlanDispatcher(
        database,
        runtime._worker_executor,
        OrchestrationEventPublisher(runtime._events),
    )
    runtime._orchestration_events = OrchestrationEventPublisher(runtime._events)
    runtime._plan_result_synthesizer = PlanResultSynthesizer(fake_llm)

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
async def test_new_approval_round_uses_current_turn_batch_id() -> None:
    from athena.runtime.execution_loop.prepare_tools import prepare_tool_batch

    created: list[dict] = []

    class _ApprovalManager:
        async def request_approval(self, **kwargs):
            created.append(kwargs)
            return SimpleNamespace(id=kwargs["approval_id"])

    class _Tools:
        def require_approval(self, name: str) -> bool:
            return True

        def get_risk_level(self, name: str) -> str:
            return "high"

    runtime = SimpleNamespace(_tool_manager=_Tools(), _approval_manager=_ApprovalManager())

    await prepare_tool_batch(
        {
            "recoverable": {
                "turn_count": 1,
                "pending_tool_calls": [{"id": "call-1", "name": "write_file", "args": {}}],
            }
        },
        None,
        graph_runtime=runtime,
        session_id="session-1",
        run_id="run-1",
    )
    await prepare_tool_batch(
        {
            "recoverable": {
                "turn_count": 2,
                "pending_tool_calls": [{"id": "call-2", "name": "write_file", "args": {}}],
            }
        },
        None,
        graph_runtime=runtime,
        session_id="session-1",
        run_id="run-1",
    )

    assert [item["approval_batch_id"] for item in created] == [
        "run-1:tool-batch:1",
        "run-1:tool-batch:2",
    ]


@pytest.mark.asyncio
async def test_main_graph_has_orchestration_branch() -> None:
    """主图必须包含计划物化、计划执行和编排汇总节点。"""

    from unittest.mock import MagicMock

    from athena.runtime.langgraph_runtime import LangGraphRuntime
    from athena.runtime.agent_graph import build_graph

    runtime = LangGraphRuntime.__new__(LangGraphRuntime)
    runtime._session_context_service = MagicMock()
    runtime._agent_execution_service = MagicMock()
    runtime._event_publisher = None

    graph = build_graph(runtime)
    nodes = set(graph.get_graph().nodes)
    assert {
            "prepare_request_and_persist_message",
            "understand_task",
            "plan_context",
            "acquire_context",
            "clarification_response",
            "prepare_harness_input",
        "agent_loop",
        "materialize_execution_plan",
        "run_planned_orchestration",
        "build_orchestration_response",
        "close_execution_stream",
        "post_process_and_build_result",
        "assemble_final_response",
    } <= nodes
    assert not {
        "process_attachments",
        "handle_attachment_failure",
        "prepare_and_persist_request",
        "handle_file_failure",
        "decide_memory",
        "prepare_context",
        "materialize_plan",
        "execute_plan",
        "synthesize_orchestration",
        "close_plan_stream",
        "post_process_turn",
        "finalize_response",
        "plan_orchestration",
    } & nodes


def test_first_agent_turn_has_three_routes() -> None:
    from athena.runtime.execution_loop.graph import _route_after_llm

    assert _route_after_llm({"execution": {"recoverable": {"final_content": "回答"}}}) == "finish_execution"
    assert _route_after_llm({"execution": {"recoverable": {"pending_tool_calls": [{"name": "x"}]}}}) == "prepare_tool_batch"
    assert _route_after_llm({"execution": {"derived": {"route": "plan_requested"}}}) == "plan_requested"


def test_non_retryable_llm_error_finishes_without_another_llm_call() -> None:
    from athena.runtime.execution_loop.graph import _route_after_llm

    assert _route_after_llm(
        {"response": {"error": "invalid submit_plan"}, "execution": {"derived": {"retryable": False}}}
    ) == "finish_execution"


def test_retryable_llm_error_can_retry_within_budgets() -> None:
    from athena.runtime.execution_loop.graph import _route_after_llm

    assert _route_after_llm(
        {
            "response": {"error": "temporary failure"},
            "execution": {
                "recoverable": {"turn_count": 1, "retry_count": 1, "max_turns": 3, "max_retries": 2},
                "derived": {"retryable": True},
            }
        }
    ) == "llm_call"


def test_technical_llm_failure_does_not_enter_graph_retry() -> None:
    """Provider 内部重试耗尽后，Graph 不应再次放大调用次数。"""

    from athena.runtime.execution_loop.graph import _route_after_llm

    assert _route_after_llm(
        {
            "execution": {
                "recoverable": {"turn_count": 1, "retry_count": 0, "max_turns": 3, "max_retries": 2},
                "derived": {"retryable": False, "llm_result_status": "technical_failure"},
            }
        }
    ) == "finish_execution"


def test_semantic_llm_failure_carries_feedback_for_next_call() -> None:
    """业务结果错误应保留下一轮 LLM 可见的校正反馈。"""

    from athena.core.harness.turn_executor import LlmTurnOutcome

    outcome = LlmTurnOutcome(
        messages=[],
        error="LLM 返回空响应（无内容且无工具调用）",
        llm_result_status="semantic_retry",
        llm_retry_reason="empty_response",
        retry_feedback="上一次响应为空，请重新生成。",
        retryable=True,
    )

    assert outcome.llm_result_status == "semantic_retry"
    assert outcome.retry_feedback == "上一次响应为空，请重新生成。"


def test_invalid_plan_submission_is_retryable_within_budget() -> None:
    from athena.core.harness.turn_executor import TurnExecutor, _LlmTurnContext

    executor = TurnExecutor.__new__(TurnExecutor)
    executor._answer_stream = SimpleNamespace(version=0, offset=0)
    context = _LlmTurnContext(
        session_id="session-1",
        run_id="run-1",
        system_prompt="",
        turn_count=1,
        retry_count=0,
        max_turns=3,
        max_retries=1,
        parent_run_id=None,
        stream_started=False,
        compressed=[],
        llm_call_id="run-1:llm:2",
        started_at=0.0,
        next_turn=2,
    )

    outcome = executor._plan_outcome(
        context,
        message_dicts=[],
        content="",
        plan_calls=[{"name": "submit_plan", "args": {"invalid": True}}],
        tool_calls=[{"name": "submit_plan", "args": {"invalid": True}}],
    )

    assert outcome.llm_result_status == "semantic_retry"
    assert outcome.retryable is True
    assert outcome.retry_count == 1
    assert outcome.retry_feedback


def test_plan_submission_is_exposed_on_every_top_level_turn() -> None:
    from athena.core.harness.turn_executor import TurnExecutor
    from athena.runtime.orchestration import PLAN_SUBMISSION_TOOL_NAME

    executor = TurnExecutor.__new__(TurnExecutor)
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
