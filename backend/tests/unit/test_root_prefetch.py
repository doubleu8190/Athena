"""首次决策前读取资料、两阶段查询与正文事实边界的回归测试。"""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from application.orchestration import RootWorkflow
from application.orchestration.answer_generation import AnswerGenerationInput, AnswerGenerationService
from application.orchestration.root_decision import AnswerIntent, ContextRequest, RootMessage
from application.orchestration.langgraph import invoke_root_graph
from application.runs.context import (
    ContextAcquisitionService, ContextItem, ContextQueryOutcome,
    MemoryQuery, ProviderResult,
)
from configuration.settings import Settings
from tests.unit.test_root_graph import (
    _Agent, _Events, _Provider, _TraceRepository, _build_graph, _dependencies,
)


def _request_state(message: str) -> dict:
    """构造一个无历史的已准备请求；参数为原文，返回测试图状态，无业务异常。"""
    return {
        "request": {"session_id": "new-session", "run_id": "prefetch-run",
                    "message_id": "question", "user_message": message,
                    "history": [], "attachment_refs": []},
        "available_context_providers": ["memory", "knowledge", "graph", "file"],
        "knowledge_bases": [],
        "root_turn_count": 0,
    }


@pytest.mark.asyncio
async def test_prefetch_hit_reaches_first_decision_and_answer_without_directed_lookup() -> None:
    """明确的苹果事实在首次决策前读取并进入正文；无参数，异常即测试失败。"""
    calls = []

    async def retrieve(*, session_id, run_id, plan):
        """记录真实执行的计划并返回历史记忆，不改变决策状态。"""
        calls.append(plan)
        return ProviderResult("memory", "succeeded", (ContextItem("memory", "喜欢吃苹果", "old-memory"),), result_count=1)

    agent = _Agent([AnswerIntent(action="answer", kind="final", instructions="你喜欢吃苹果。")])
    deps = _dependencies(agent, provider=retrieve, root_max_turns=1)
    result = await invoke_root_graph(_build_graph(deps), session_id="new-session", run_id="prefetch-run",
                                     message_id="question", user_message="你知道我喜欢吃什么水果吗？")
    assert result["status"] == "completed"
    assert len(calls) == len(agent.inputs) == 1
    assert calls[0].memory_query == "你知道我喜欢吃什么水果吗？"
    assert calls[0].provider_timeout_seconds == 10.0
    assert agent.inputs[0].remaining_turns == 1
    assert agent.inputs[0].context_items[0].source_id == "old-memory"
    assert deps.answer_generation.inputs[0].context_items == agent.inputs[0].context_items
    assert deps.answer_generation.inputs[0].context_queries == agent.inputs[0].context_queries
    assert result["root_turn_count"] == 1


@pytest.mark.asyncio
async def test_prefetch_miss_allows_rewritten_lookup_and_retains_both_stages() -> None:
    """原 query 未命中后实际执行改写查询；无参数，异常即测试失败。"""
    calls = []

    async def retrieve(*, session_id, run_id, plan):
        """只对改写主题返回苹果，记录两次查询与等待上限。"""
        calls.append(plan)
        items = (ContextItem("memory", "喜欢吃苹果", "fruit"),) if plan.memory_query == "用户水果偏好" else ()
        return ProviderResult("memory", "succeeded", items, result_count=len(items))

    agent = _Agent([
        ContextRequest(action="context", queries=(MemoryQuery(provider="memory", query="用户水果偏好"),)),
        AnswerIntent(action="answer", kind="final", instructions="你喜欢吃苹果。"),
    ])
    deps = _dependencies(agent, provider=retrieve)
    result = await invoke_root_graph(_build_graph(deps), session_id="new-session", run_id="two-stages",
                                     message_id="question", user_message="你知道我喜欢吃什么水果吗？")
    assert result["status"] == "completed"
    assert [plan.provider_timeout_seconds for plan in calls] == [10.0, None]
    outcomes = [outcome for outcome in agent.inputs[1].context_queries if outcome.query.provider == "memory"]
    assert [(outcome.stage, outcome.status) for outcome in outcomes] == [("prefetch", "empty"), ("directed", "succeeded")]
    assert agent.inputs[1].context_items[0].content == "喜欢吃苹果"
    assert result["root_turn_count"] == 2


@pytest.mark.asyncio
async def test_prefetch_only_reads_ready_knowledge_bases_and_never_graph_or_files() -> None:
    """只将就绪知识库 ID 绑定原 query；无参数，异常即测试失败。"""
    calls = []

    async def retrieve(*, session_id, run_id, plan):
        """记录资料范围并返回知识片段。"""
        calls.append(plan)
        return ProviderResult("knowledge", "succeeded", (ContextItem("knowledge", "项目说明", "doc"),), result_count=1)

    deps = _dependencies(_Agent([]))
    deps.context_acquisition = ContextAcquisitionService(
        {"knowledge": _Provider(retrieve)}, _TraceRepository(), _Events()
    )
    state = _request_state("项目说明是什么？")
    state["available_context_providers"] = ["knowledge", "graph", "file"]
    state["knowledge_bases"] = [
        {"id": "ready", "name": "就绪", "document_count": 1, "ready_document_count": 1},
        {"id": "processing", "name": "处理中", "document_count": 1, "ready_document_count": 0},
    ]
    update = await RootWorkflow(deps).prefetch_context(state)
    assert len(calls) == 1
    assert calls[0].providers == ("knowledge",)
    assert calls[0].knowledge_base_ids == ("ready",)
    assert update["context_bundle"]["items"][0]["content"] == "项目说明"
    assert not update["planning_context_updated"]
    assert "root_turn_count" not in update and "root_decision" not in update


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["  ", "长" * 600])
async def test_prefetch_query_truncation_preserves_original_message(message: str) -> None:
    """参数 message 覆盖空白与长消息；原文不变，空白不构造假 query。"""
    calls = []

    async def retrieve(*, session_id, run_id, plan):
        """记录预检索实际 query，返回空结果。"""
        calls.append(plan.memory_query)
        return ProviderResult("memory", "succeeded")

    deps = _dependencies(_Agent([]), provider=retrieve)
    state = _request_state(message)
    await RootWorkflow(deps).prefetch_context(state)
    assert calls == ([message.strip()[:500]] if message.strip() else [])
    assert state["request"]["user_message"] == message


@pytest.mark.asyncio
async def test_prefetch_empty_knowledge_scope_is_skipped_without_reading_provider() -> None:
    """无就绪知识库时不发生全库回退；无参数，异常即测试失败。"""
    deps = _dependencies(_Agent([]))
    update = await RootWorkflow(deps).prefetch_context(_request_state("查资料"))
    outcomes = {item["query"]["provider"]: item for item in update["context_queries"]}
    assert outcomes["knowledge"]["status"] == "skipped"
    assert outcomes["knowledge"]["error_message"] == "no_ready_knowledge_bases"


@pytest.mark.asyncio
async def test_failed_directed_lookup_retains_prefetch_material_without_double_access() -> None:
    """第二批失败保留第一批材料，旧材料不重复确认访问；无参数。"""
    accessed = []

    class MemoryAccess:
        async def record_access(self, ids):
            """记录本批真正选中的记忆 ID。"""
            accessed.extend(ids)

    async def retrieve(*, session_id, run_id, plan):
        """原 query 命中；定向 query 故障。"""
        if plan.memory_query == "查偏好":
            return ProviderResult("memory", "succeeded", (ContextItem("memory", "苹果", "fruit"),), result_count=1)
        raise RuntimeError("lookup unavailable")

    deps = _dependencies(_Agent([]), provider=retrieve)
    deps.context_acquisition = ContextAcquisitionService(
        {"memory": _Provider(retrieve)}, _TraceRepository(), _Events(), memory_repository=MemoryAccess()
    )
    state = _request_state("查偏好")
    state["available_context_providers"] = ["memory"]
    workflow = RootWorkflow(deps)
    state.update(await workflow.prefetch_context(state))
    state["root_decision"] = ContextRequest(action="context", queries=(MemoryQuery(provider="memory", query="补充偏好"),)).model_dump(mode="json")
    state.update(await workflow.acquire_context(state))
    assert state["context_bundle"]["items"][0]["content"] == "苹果"
    assert state["context_queries"][-1]["status"] == "failed"
    assert accessed == ["fruit"]


def test_outcomes_round_trip_stage_and_reject_missing_stage() -> None:
    """阶段为必须的持久化信息；无参数，验证 JSON 往返和损坏拒绝。"""
    outcome = ContextQueryOutcome(stage="prefetch", query=MemoryQuery(provider="memory", query="偏好"), status="timeout", result_count=0)
    assert ContextQueryOutcome.model_validate_json(outcome.model_dump_json()) == outcome
    with pytest.raises(ValidationError):
        ContextQueryOutcome.model_validate({"query": {"provider": "memory", "query": "偏好"}, "status": "empty", "result_count": 0})


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_prefetch_configuration_rejects_invalid_timeout(timeout: float) -> None:
    """参数 timeout 必须有限且大于零，否则配置校验失败。"""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, context_prefetch_timeout_seconds=timeout)


def test_answer_model_receives_failure_summary_within_existing_input_budget() -> None:
    """正文获得查询失败而非凭空推断无记忆；无参数。"""
    class TokenCounter:
        def count_text_tokens(self, text):
            """对测试字符串按字符计数，返回整数。"""
            return len(text)

    service = AnswerGenerationService(TokenCounter(), _Events(), prompt="按事实回答", input_budget_tokens=4000)
    inputs = AnswerGenerationInput(
        session_id="s", run_id="r", message_id="m", kind="clarification", instructions="说明暂时无法确认",
        messages=(RootMessage(role="user", content="我喜欢什么水果？"),),
        context_queries=(ContextQueryOutcome(stage="prefetch", query=MemoryQuery(provider="memory", query="水果"), status="timeout", result_count=0),),
    )
    facts = json.loads(service._fit_messages(inputs)[1]["content"].split("\n", 1)[1])
    assert facts["context_queries"][0]["status"] == "timeout"
    assert facts["context_queries"][0]["stage"] == "prefetch"


@pytest.mark.asyncio
async def test_cancelled_prefetch_does_not_call_decision_or_answer() -> None:
    """入口采集被取消时不继续模型调用；无参数，取消必须向上传播。"""
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def retrieve(*, session_id, run_id, plan):
        """模拟可取消的通道调用，退出时记录清理。"""
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    agent = _Agent([])
    deps = _dependencies(agent, provider=retrieve)
    pending = asyncio.create_task(invoke_root_graph(
        _build_graph(deps), session_id="s", run_id="cancel-prefetch", message_id="m", user_message="水果"
    ))
    await asyncio.wait_for(started.wait(), timeout=1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert cleaned.is_set()
    assert agent.inputs == deps.answer_generation.inputs == []


@pytest.mark.asyncio
async def test_unrecoverable_prefetch_error_uses_failure_edge() -> None:
    """损坏的预检索状态经错误边结束，不能使用空资料继续决策；无参数。"""
    agent = _Agent([])
    deps = _dependencies(agent)
    workflow = RootWorkflow(deps)

    async def fail(state):
        """模拟不可恢复的应用契约错误。"""
        raise ValueError("damaged_prefetch_state")

    workflow.prefetch_context = fail
    from application.orchestration.langgraph import build_root_graph

    graph = build_root_graph(workflow=workflow, dependencies=deps)
    result = await invoke_root_graph(graph, session_id="s", run_id="failed-prefetch", message_id="m", user_message="水果")
    assert result["status"] == "failed"
    assert result["error"]["node"] == "prefetch_context"
    assert agent.inputs == deps.answer_generation.inputs == []
