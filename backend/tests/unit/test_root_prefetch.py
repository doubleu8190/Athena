"""首次决策前读取资料、两阶段查询与正文事实边界的回归测试。"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from tests.fakes.leases import lease_grant
from pydantic import ValidationError
from pydantic import BaseModel
from domain.memory import MemoryRecord
from domain.retrieval.trace_contracts import RetrievalCandidateSelection

from application.orchestration import RootWorkflow
from application.orchestration.root_workflow import _context_bundle_from_state
from application.orchestration.agents.root_agent import ConfiguredRootAgent
from application.orchestration.answer_generation import AnswerGenerationInput, AnswerGenerationService
from application.orchestration.root_decision import AnswerIntent, RootMessage
from application.runs.request_contracts import RootRequestState
from tests.unit.test_root_graph import invoke_root_graph
from application.runs.context import (
    ContextAcquisitionService, ContextBundle, ContextItem, ContextQueryOutcome,
    ContextPlanner, FileQuery, MemoryQuery, ProviderResult,
)
from configuration.settings import LLMProviderConfig, LLMRetrySettings, Settings
from infrastructure.integrations.llm.llm_provider import ConfiguredLLMProvider
from tests.unit.test_root_graph import (
    _Agent, _Events, _Provider, _TraceRepository, _build_graph, _dependencies,
)


def _request_state(message: str) -> dict:
    """构造一个无历史的已准备请求；参数为原文，返回测试图状态，无业务异常。"""
    return {
        "request": {"session_id": "new-session", "run_id": "prefetch-run",
                    "message_id": "question", "user_message": message,
                    "history": [], "attachment_refs": [], "knowledge_bases": []},
        "available_context_providers": ["memory", "knowledge", "graph", "file"],
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
async def test_original_and_rewritten_queries_are_prefetched_before_one_decision() -> None:
    """两条不同查询均在 Root 决策前检索并合并；无参数，异常即失败。"""
    calls = []

    async def retrieve(*, session_id, run_id, plan):
        """只对改写主题返回苹果，记录两次查询与等待上限。"""
        calls.append(plan)
        items = (ContextItem("memory", "喜欢吃苹果", "fruit"),) if plan.memory_query == "用户水果偏好" else ()
        return ProviderResult("memory", "succeeded", items, result_count=len(items))

    class RewritingAgent(_Agent):
        async def rewrite_query(self, query, messages, **_kwargs):
            assert query == "你知道我喜欢吃什么水果吗？"
            assert messages[-1].content == query
            return "用户水果偏好"

    agent = RewritingAgent([AnswerIntent(action="answer", kind="final", instructions="你喜欢吃苹果。")])
    deps = _dependencies(agent, provider=retrieve)
    result = await invoke_root_graph(_build_graph(deps), session_id="new-session", run_id="two-stages",
                                     message_id="question", user_message="你知道我喜欢吃什么水果吗？")
    assert result["status"] == "completed"
    assert [plan.memory_query for plan in calls] == ["你知道我喜欢吃什么水果吗？", "用户水果偏好"]
    assert [plan.provider_timeout_seconds for plan in calls] == [10.0, 10.0]
    outcomes = [outcome for outcome in agent.inputs[0].context_queries if outcome.query.provider == "memory"]
    assert [(outcome.stage, outcome.status) for outcome in outcomes] == [("prefetch", "empty"), ("prefetch", "succeeded")]
    assert agent.inputs[0].context_items[0].content == "喜欢吃苹果"
    assert len(agent.inputs) == 1
    assert result["root_turn_count"] == 1


@pytest.mark.asyncio
async def test_query_rewrite_failure_falls_back_to_original_query(monkeypatch) -> None:
    calls = []
    degradation_events = []

    from application.orchestration import root_workflow

    monkeypatch.setattr(
        root_workflow.logger,
        "warning",
        lambda event, **fields: degradation_events.append((event, fields)),
    )

    async def retrieve(*, session_id, run_id, plan):
        calls.append(plan.memory_query)
        return ProviderResult("memory", "succeeded", result_count=0)

    class FailingRewriteAgent(_Agent):
        async def rewrite_query(self, *_args, **_kwargs):
            raise TimeoutError("rewrite timed out")

    agent = FailingRewriteAgent([AnswerIntent(action="answer", kind="final", instructions="完成。")])
    deps = _dependencies(agent, provider=retrieve)
    result = await invoke_root_graph(
        _build_graph(deps), session_id="new-session", run_id="rewrite-fallback",
        message_id="question", user_message="请查找我的偏好",
    )

    assert result["status"] == "completed"
    assert calls == ["请查找我的偏好"]
    assert len(agent.inputs) == 1
    assert degradation_events == [
        ("root_query_rewrite_degraded", {"reason": "secondary_timeout"})
    ]


@pytest.mark.asyncio
async def test_identical_rewritten_query_is_prefetched_only_once() -> None:
    calls = []

    async def retrieve(*, session_id, run_id, plan):
        calls.append(plan.memory_query)
        return ProviderResult("memory", "succeeded", result_count=0)

    class RewritingAgent(_Agent):
        async def rewrite_query(self, query, *_args, **_kwargs):
            return query

    deps = _dependencies(RewritingAgent([]), provider=retrieve)
    await RootWorkflow(deps).prefetch_context(_request_state("相同的问题"))

    assert calls == ["相同的问题"]


def test_memory_context_revalidation_default_timeout_is_ten_seconds() -> None:
    parameter = inspect.signature(
        ContextAcquisitionService.validate_memory_context
    ).parameters["timeout_seconds"]

    assert parameter.default == 10.0


@pytest.mark.asyncio
async def test_configured_secondary_timeout_falls_back_to_original_prefetch(monkeypatch) -> None:
    from infrastructure.integrations.llm import llm_provider

    query_calls = []

    async def slow_secondary(_messages):
        await asyncio.sleep(0.05)
        return AIMessage(content="rewritten query")

    class Model:
        def __init__(self, model_name):
            self.model_name = model_name

        def with_structured_output(self, schema, *, include_raw=False):
            assert schema.__name__ == "QueryRewriteOutput"
            assert include_raw is False
            return RunnableLambda(slow_secondary)

    def create_model(options, *, sdk_max_retries=None):
        return Model(options.primary.model)

    monkeypatch.setattr(llm_provider, "_create_model", create_model)
    provider = ConfiguredLLMProvider(
        Settings(
            _env_file=None,
            llm_providers=[
                LLMProviderConfig(provider="ollama", model="main"),
                LLMProviderConfig(provider="ollama", model="light"),
            ],
            llm_retry=LLMRetrySettings(max_attempts=1, timeout_ms=1000, min_delay_ms=0),
            llm_secondary_retry=LLMRetrySettings(max_attempts=1, timeout_ms=5, min_delay_ms=0),
        )
    )

    async def retrieve(*, session_id, run_id, plan):
        query_calls.append(plan.memory_query)
        return ProviderResult("memory", "succeeded", result_count=0)

    deps = _dependencies(_Agent([]), provider=retrieve)
    deps.agent = ConfiguredRootAgent(provider)
    state = _request_state("请查询我的偏好")

    await RootWorkflow(deps).prefetch_context(state)

    assert query_calls == ["请查询我的偏好"]


@pytest.mark.asyncio
async def test_secondary_structured_validation_error_has_invalid_output_reason(monkeypatch) -> None:
    from infrastructure.integrations.llm import llm_provider
    from application.orchestration import root_workflow

    class RewriteOutput(BaseModel):
        rewritten_query: str

    async def invalid_structured_result(_messages):
        return RewriteOutput.model_validate({})

    class Model:
        def with_structured_output(self, _schema, *, include_raw=False):
            return RunnableLambda(afunc=invalid_structured_result, func=lambda _messages: None)

    monkeypatch.setattr(
        llm_provider,
        "_create_model",
        lambda _options, **_kwargs: Model(),
    )
    degradation_events = []
    monkeypatch.setattr(
        root_workflow.logger,
        "warning",
        lambda event, **fields: degradation_events.append((event, fields)),
    )
    provider = ConfiguredLLMProvider(
        Settings(
            _env_file=None,
            llm_providers=[
                LLMProviderConfig(provider="ollama", model="main"),
                LLMProviderConfig(provider="ollama", model="light"),
            ],
            llm_retry=LLMRetrySettings(max_attempts=1, timeout_ms=1000, min_delay_ms=0),
            llm_secondary_retry=LLMRetrySettings(max_attempts=1, timeout_ms=1000, min_delay_ms=0),
        )
    )
    query_calls = []

    async def retrieve(*, session_id, run_id, plan):
        query_calls.append(plan.memory_query)
        return ProviderResult("memory", "succeeded", result_count=0)

    deps = _dependencies(_Agent([]), provider=retrieve)
    deps.agent = ConfiguredRootAgent(provider)
    await RootWorkflow(deps).prefetch_context(_request_state("原始问题"))

    assert query_calls == ["原始问题"]
    assert degradation_events == [
        ("root_query_rewrite_degraded", {"reason": "invalid_structured_output"})
    ]


@pytest.mark.asyncio
async def test_authorized_attachments_are_prefetched_with_both_queries() -> None:
    calls = []

    async def retrieve(*, session_id, run_id, plan):
        calls.append(plan)
        return ProviderResult(
            "file", "succeeded",
            (ContextItem("file", "附件中的报告内容", "attachment-1"),),
            result_count=1,
        )

    class RewritingAgent(_Agent):
        async def rewrite_query(self, *_args, **_kwargs):
            return "报告结论"

    deps = _dependencies(RewritingAgent([]))
    deps.available_context_providers = ("file",)
    deps.context_acquisition = ContextAcquisitionService(
        {"file": _Provider(retrieve)}, _TraceRepository(), _Events()
    )
    state = _request_state("总结这个附件")
    state["request"]["attachment_refs"] = [
        {
            "id": "attachment-1", "filename": "report.pdf", "mime_type": "application/pdf",
            "size_bytes": 10, "status": "ready",
        }
    ]

    update = await RootWorkflow(deps).prefetch_context(state)

    assert [plan.file_query for plan in calls] == ["总结这个附件", "报告结论"]
    assert all(plan.file_ids == ("attachment-1",) for plan in calls)
    assert all(isinstance(outcome.query, FileQuery) for outcome in map(ContextQueryOutcome.model_validate, update["context_queries"]))
    assert update["context_bundle"]["items"][0]["content"] == "附件中的报告内容"


@pytest.mark.asyncio
async def test_prefetch_confirms_only_final_window_and_deduplicates_memory_access() -> None:
    accessed = []
    selections_by_run = {}
    retrieval_run = 0

    class MemoryAccess:
        def __init__(self):
            self.items = {}

        async def get(self, memory_id):
            return self.items.get(memory_id)

        async def list_fact_group_by_identity(self, _identity):
            return []

        async def record_access(self, memory_ids):
            accessed.extend(memory_ids)

    class RecordingTrace(_TraceRepository):
        async def record_context(self, **_kwargs):
            nonlocal retrieval_run
            retrieval_run += 1
            return f"retrieval-{retrieval_run}"

        async def update_context_selection(self, *, run_id, selections):
            selections_by_run[run_id] = selections
            return True

    memory = MemoryAccess()

    async def retrieve(*, session_id, run_id, plan):
        source_id = "raw-memory" if plan.memory_query == "原始问题" else "rewritten-memory"
        content = "原始结果内容" if source_id == "raw-memory" else "改写结果内容"
        return ProviderResult(
            "memory", "succeeded",
            (ContextItem("memory", content, source_id, {"category": "preference"}),),
            result_count=1,
        )

    class RewritingAgent(_Agent):
        async def rewrite_query(self, *_args, **_kwargs):
            return "改写问题"

    deps = _dependencies(RewritingAgent([]))
    deps.available_context_providers = ("memory",)
    deps.context_planner = ContextPlanner(max_items=1)
    deps.context_acquisition = ContextAcquisitionService(
        {"memory": _Provider(retrieve, memory)}, RecordingTrace(), _Events(),
        memory_repository=memory,
    )
    state = _request_state("原始问题")

    update = await RootWorkflow(deps).prefetch_context(state)

    assert [item["source_id"] for item in update["context_bundle"]["items"]] == [
        "rewritten-memory"
    ]
    assert accessed == ["rewritten-memory"]
    assert selections_by_run["retrieval-1"][0].context_selected is False
    assert selections_by_run["retrieval-1"][0].filter_reason == "context_window_budget"
    assert selections_by_run["retrieval-2"][0].context_selected is True


@pytest.mark.asyncio
async def test_prefetch_duplicate_memory_candidate_is_accessed_once_across_batches() -> None:
    accessed = []
    selections_by_run = {}

    class MemoryAccess:
        async def record_access(self, memory_ids):
            accessed.extend(memory_ids)

    class RecordingTrace(_TraceRepository):
        async def update_context_selection(self, *, run_id, selections):
            selections_by_run[run_id] = selections
            return True

    deps = _dependencies(_Agent([]))
    deps.context_acquisition = ContextAcquisitionService(
        {}, RecordingTrace(), _Events(), memory_repository=MemoryAccess()
    )
    workflow = RootWorkflow(deps)
    duplicate = RetrievalCandidateSelection(
        provider="memory", source_id="same-memory", source_ids=("same-memory",),
        context_selected=True,
    )

    await workflow._confirm_prefetch_selections(
        [("original-run", (duplicate,)), ("rewritten-run", (duplicate,))],
        ContextBundle(
            items=(ContextItem("memory", "相同事实", "same-memory"),),
            provider_results=(),
        ),
        request_state=RootRequestState(session_id="session-1", run_id="run-1"),
    )

    assert accessed == ["same-memory"]
    assert selections_by_run["original-run"][0].context_selected is False
    assert selections_by_run["rewritten-run"][0].context_selected is True


@pytest.mark.asyncio
async def test_permission_change_updates_selection_for_every_prefetch_trace() -> None:
    updates_by_run = {}
    retrieval_id = 0

    class MemoryAccess:
        def __init__(self):
            self.items = {}

        async def get(self, memory_id):
            return self.items.get(memory_id)

        async def list_fact_group_by_identity(self, _identity):
            return []

        async def record_access(self, _memory_ids):
            return None

    class RecordingTrace(_TraceRepository):
        async def record_context(self, **_kwargs):
            nonlocal retrieval_id
            retrieval_id += 1
            return f"trace-{retrieval_id}"

        async def update_context_selection(self, *, run_id, selections):
            updates_by_run[run_id] = selections
            return True

    class MemorySettings:
        use_memories = True

        async def get(self, session_id):
            return type(
                "SettingsValue",
                (),
                {"session_id": session_id, "use_memories": self.use_memories, "project_id": None},
            )()

    async def retrieve(*, session_id, run_id, plan):
        source_id = f"memory-{plan.memory_query}"
        return ProviderResult(
            "memory", "succeeded",
            (ContextItem("memory", f"事实 {plan.memory_query}", source_id, {"category": "preference"}),),
            result_count=1,
        )

    class RewritingAgent(_Agent):
        async def rewrite_query(self, *_args, **_kwargs):
            return "改写问题"

    memory = MemoryAccess()
    deps = _dependencies(RewritingAgent([]))
    deps.available_context_providers = ("memory",)
    deps.context_acquisition = ContextAcquisitionService(
        {"memory": _Provider(retrieve, memory)}, RecordingTrace(), _Events(),
        memory_repository=memory,
    )
    settings = MemorySettings()
    deps.memory_settings = settings
    workflow = RootWorkflow(deps)
    state = _request_state("原始问题")
    state.update(await workflow.prefetch_context(state))
    bundle = _context_bundle_from_state(state["context_bundle"])
    assert len(bundle.selection_batches) == 2

    settings.use_memories = False
    effective = await workflow._effective_context_bundle("new-session", bundle)

    assert effective.items == ()
    assert set(updates_by_run) == {"trace-1", "trace-2"}
    assert all(
        not selection.context_selected
        and selection.filter_reason == "session_memory_read_disabled"
        for selections in updates_by_run.values()
        for selection in selections
    )


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
    deps.available_context_providers = ("knowledge",)
    state = _request_state("项目说明是什么？")
    state["available_context_providers"] = ["knowledge", "graph", "file"]
    state["request"]["knowledge_bases"] = [
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
async def test_prefetch_passes_empty_knowledge_scope_to_provider() -> None:
    """无就绪知识库时由提供方处理空范围，且不回退到全库。"""
    calls = []

    async def retrieve(*, session_id, run_id, plan):
        calls.append(plan)
        return ProviderResult(
            "knowledge", "skipped", error_message="no_ready_knowledge_bases"
        )

    deps = _dependencies(_Agent([]))
    deps.context_acquisition = ContextAcquisitionService(
        {"knowledge": _Provider(retrieve)}, _TraceRepository(), _Events()
    )
    deps.available_context_providers = ("knowledge",)
    state = _request_state("查资料")
    state["request"]["knowledge_bases"] = [
        {
            "id": "processing",
            "name": "处理中",
            "document_count": 1,
            "ready_document_count": 0,
        }
    ]

    update = await RootWorkflow(deps).prefetch_context(state)
    outcomes = {item["query"]["provider"]: item for item in update["context_queries"]}
    assert len(calls) == 1
    assert calls[0].providers == ("knowledge",)
    assert calls[0].knowledge_base_ids == ()
    assert outcomes["knowledge"]["status"] == "skipped"
    assert outcomes["knowledge"]["error_message"] == "no_ready_knowledge_bases"


@pytest.mark.asyncio
async def test_failed_rewritten_lookup_retains_original_material_without_double_access() -> None:
    """改写 query 失败时保留原 query 材料，且不重复确认访问；无参数。"""
    accessed = []

    class MemoryAccess:
        async def get(self, memory_id):
            if memory_id == "fruit":
                return MemoryRecord(
                    "fruit", "苹果", {"category": "preference"}
                )
            return None

        async def list_fact_group_by_identity(self, _identity):
            return []

        async def record_access(self, ids):
            """记录本批真正选中的记忆 ID。"""
            accessed.extend(ids)

    async def retrieve(*, session_id, run_id, plan):
        """原 query 命中；改写 query 故障。"""
        if plan.memory_query == "查偏好":
            return ProviderResult(
                "memory",
                "succeeded",
                (
                    ContextItem(
                        "memory",
                        "苹果",
                        "fruit",
                        {"category": "preference"},
                    ),
                ),
                result_count=1,
            )
        raise RuntimeError("lookup unavailable")

    class RewritingAgent(_Agent):
        async def rewrite_query(self, _query, _messages, **_kwargs):
            return "改写偏好"

    deps = _dependencies(RewritingAgent([]), provider=retrieve)
    deps.context_acquisition = ContextAcquisitionService(
        {"memory": _Provider(retrieve)}, _TraceRepository(), _Events(), memory_repository=MemoryAccess()
    )
    state = _request_state("查偏好")
    state["available_context_providers"] = ["memory"]
    workflow = RootWorkflow(deps)
    state.update(await workflow.prefetch_context(state))
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
        execution_lease=lease_grant("root_execution", "r"),
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
