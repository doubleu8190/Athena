"""Root run workflow independent from LangGraph.

This module owns the application steps required to prepare a request, acquire
context, execute an agent and materialize a worker plan.  The LangGraph
adapter only supplies state and connects these steps into a graph.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from shared.logging import get_logger
from domain.orchestration import (
    CommandReceipt,
    OrchestrationCommand,
    OrchestrationCommandType,
    PlanCommandRejected,
    ReviewAction,
    ReviewDecision,
    ReviewContext,
)

from .dependencies import RootWorkflowDependencies
from .graph_state import (
    RootAnswerInputState,
    RootPlanIdentity,
    RootResumeIdentity,
    RootWaitIdentity,
)
from .root_decision import (
    ROOT_REDECISION_ADAPTER,
    AnswerIntent,
    ContextRequest,
    PlanDraft,
    PlanIntent,
    PlanningFeedback,
    RootPlanInput,
    ROOT_PLAN_ADAPTER,
    PlanTaskDraft,
    ROOT_DECISION_ADAPTER,
    RootAgentInput,
    RootMessage,
    RootWorkerTool,
)
from application.runs.context.contracts import (
    ContextBundle,
    ContextItem,
    ContextPlan,
    ContextQuery,
    EpisodeQuery,
    ContextQueryOutcome,
    KnowledgeQuery,
    MemoryQuery,
    ProviderResult,
    RetrievalStage,
)
from application.runs.context.planner import InvalidContextResource
from application.runs.request_contracts import RootKnowledgeBase, RootRequestState
from domain.retrieval.trace_contracts import (
    RetrievalCandidateSelection,
    RetrievalCandidateTrace,
    RetrievalChannelTrace,
)
from shared.contracts.retrieval import (
    RetrievalCandidateSelectionSchema,
    RetrievalCandidateTraceSchema,
    RetrievalChannelTraceSchema,
)
from .plan_materialization import (
    coerce_plan,
    plan_dict,
    scope_execution_plan,
)
from .plan_versions import (
    deferred_version_review_decision,
    partition_plan_versions,
)
from .protocols import RootWorkflowPort
from .serialization import as_dict
from shared.serialization import json_value
from .worker_tools import worker_tool_manifest

if TYPE_CHECKING:
    from .langgraph.graphs.state import RootGraphState

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _ReviewIdentity:
    """固定一次 Round Review 命令的持久化身份。"""

    run_id: str
    plan_id: str
    round_id: str
    plan_version: int
    interrupt_id: str | None
    idempotency_key: str


def _result_text(value: Any) -> str:
    """从 Worker 结果中提取适合展示给用户的文本。

    Args:
        value: Worker 返回的结果对象；可以是嵌套映射、字符串或空值。

    Returns:
        最内层的标准输出、文本内容或错误说明；没有可展示内容时返回空字符串。
    """
    if isinstance(value, Mapping):
        # 命令工具的结构化结果把实际输出放在 stdout 中，优先提取它以免丢失执行结果。
        for key in ("stdout", "output", "content", "value"):
            if key in value:
                rendered = _result_text(value[key])
                if rendered:
                    return rendered
        error = value.get("error")
        if isinstance(error, Mapping):
            return str(error.get("message") or error.get("code") or "")
        return str(error or "")
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def render_plan_results(
    results: Mapping[str, Any], tasks: list[Mapping[str, Any]] | None = None
) -> str:
    if not results:
        return "计划已完成，但没有可展示的任务结果。"
    titles = {
        str(task.get("task_id")): str(task.get("title", ""))
        for task in tasks or []
        if isinstance(task, Mapping) and task.get("task_id")
    }
    lines = []
    for task_id, value in results.items():
        label = titles.get(str(task_id)) or str(task_id)
        text = _result_text(value)
        lines.append(
            f"{label}：{text}" if text else f"{label}：未返回可展示的文本结果。"
        )
    return "\n".join(lines)


async def _load_or_create_review_decision(
    deps: RootWorkflowDependencies,
    identity: _ReviewIdentity,
    request: Mapping[str, Any],
    state: Mapping[str, Any],
) -> tuple[
    ReviewDecision,
    ReviewContext | None,
    tuple[RootWorkerTool, ...],
]:
    """复用已保存决策，或从固定 Round 快照生成新决策。
    Args:
        deps: Root 工作流依赖。
        identity: 已校验的 Run、Plan、Round 和版本身份。
        request: 当前用户请求元数据。
        state: 用于确定性延后版本审查的图状态。
    Returns:
        审查决策和新决策所依据的上下文；复用已保存决策时上下文为空。
    Raises:
        RuntimeError: 上下文仓储、上下文身份或 Agent 决策无效时。
    """
    handler = deps.orchestration_command_handler
    if handler is None:
        raise RuntimeError("root_review_command_handler_not_configured")
    decision = await handler.load_review_decision(
        scope_key=identity.run_id,
        idempotency_key=identity.idempotency_key,
        plan_id=identity.plan_id,
        plan_version=identity.plan_version,
        round_id=identity.round_id,
    )
    if decision is not None:
        if not isinstance(decision, ReviewDecision):
            raise RuntimeError("root_review_decision_invalid")
        return decision, None, ()
    if deps.review_context_repository is None:
        raise RuntimeError("root_review_context_repository_not_configured")
    context = await deps.review_context_repository.load_review_context(
        identity.round_id
    )
    if (
        context.round_id != identity.round_id
        or context.plan_id != identity.plan_id
        or context.plan_version != identity.plan_version
    ):
        raise RuntimeError("root_review_context_identity_mismatch")
    worker_tools = tuple(
        RootWorkerTool.model_validate(item)
        for item in await worker_tool_manifest(deps.tool_service)
    )
    # 延后版本边界可由持久计划确定，避免无必要的模型调用。
    decision = deferred_version_review_decision(context, state)
    if decision is None:
        decision = await deps.agent.review(
            context,
            worker_tools=worker_tools,
            request=request,
        )
    if not isinstance(decision, ReviewDecision):
        raise RuntimeError("root_review_decision_invalid")
    return decision, context, worker_tools


def _review_command(
    identity: _ReviewIdentity, decision: ReviewDecision
) -> OrchestrationCommand:
    """将审查动作绑定到唯一 Round 和幂等键。"""
    command_type = {
        ReviewAction.CONTINUE: OrchestrationCommandType.CONTINUE_PLAN,
        ReviewAction.REPLAN: OrchestrationCommandType.REVISE_PLAN,
        ReviewAction.FINISH: OrchestrationCommandType.FINISH_PLAN,
    }[decision.action]
    return OrchestrationCommand(
        message_id=f"root-review:{identity.round_id}",
        command_type=command_type,
        idempotency_key=identity.idempotency_key,
        correlation_id=identity.run_id,
        occurred_at=datetime.now(timezone.utc),
        plan_id=identity.plan_id,
        scope_key=identity.run_id,
        payload={"action": decision.action.value, "reason": decision.reason},
        plan_version=identity.plan_version,
        round_id=identity.round_id,
        causation_id=identity.interrupt_id,
    )


def _review_identity(state: Mapping[str, Any]) -> _ReviewIdentity:
    """校验 Resume、Root Wait 与当前 Run 的身份一致。
    Args:
        state: Root 图的当前状态。
    Returns:
        供幂等命令和上下文查询使用的固定身份。
    Raises:
        RuntimeError: 恢复类型、身份字段或计划版本不合法时。
    """
    try:
        resume_value = RootResumeIdentity.model_validate(state.get("resume_value", {}))
        wait_context = RootWaitIdentity.model_validate(state.get("root_wait", {}))
    except ValidationError as error:
        raise RuntimeError("root_review_resume_identity_missing") from error
    identity_fields = ("run_id", "plan_id", "round_id", "plan_version")
    identities_match = all(
        getattr(resume_value, name) == getattr(wait_context, name)
        for name in identity_fields
    )
    if not identities_match:
        raise RuntimeError("root_review_resume_identity_mismatch")
    request = _request_state(state)
    if request.run_id != resume_value.run_id:
        raise RuntimeError("root_review_run_identity_mismatch")
    plan_version = resume_value.plan_version
    round_id = resume_value.round_id
    return _ReviewIdentity(
        run_id=resume_value.run_id,
        plan_id=resume_value.plan_id,
        round_id=round_id,
        plan_version=plan_version,
        interrupt_id=resume_value.interrupt_id or None,
        idempotency_key=f"root-review:{round_id}",
    )


async def _apply_review_decision_with_fallback(
    deps: RootWorkflowDependencies,
    identity: _ReviewIdentity,
    decision: ReviewDecision,
    context: ReviewContext | None,
    request: Mapping[str, Any],
    worker_tools: tuple[RootWorkerTool, ...],
) -> tuple[ReviewDecision, CommandReceipt]:
    """应用审查动作，并对阻塞的 Continue 执行一次有界恢复。
    Args:
        deps: Root 工作流依赖。
        identity: 需要完成审查的 Round 身份。
        decision: 首次或已持久化的审查决策。
        context: 可用于追问 Root 的固定审查上下文。
        request: 当前用户请求元数据。
    Returns:
        最终采用的决策及命令回执。
    Raises:
        RuntimeError: 命令处理器未配置时。
        PlanCommandRejected: 非依赖阻塞的命令拒绝继续向上传递。
    """
    handler = deps.orchestration_command_handler
    if handler is None:
        raise RuntimeError("root_review_command_handler_not_configured")
    available_tools = {tool.name for tool in worker_tools}
    if context is not None:
        _validate_review_tools(decision, available_tools)
    try:
        receipt = await handler.apply_review_decision(
            _review_command(identity, decision), decision
        )
        return decision, receipt
    except PlanCommandRejected as error:
        if (
            error.code != "BLOCKED_DEPENDENCIES"
            or decision.action != ReviewAction.CONTINUE
        ):
            raise
    replacement: ReviewDecision | None = None
    if context is not None:
        blocked_context = replace(
            context,
            user_constraints=(
                *context.user_constraints,
                "Continue 返回 BLOCKED_DEPENDENCIES。请改为 REPLAN 并解除失败依赖，"
                "或 FINISH 结束；不得再次选择 CONTINUE。",
            ),
        )
        candidate = await deps.agent.review(
            blocked_context,
            worker_tools=worker_tools,
            request=request,
        )
        if isinstance(candidate, ReviewDecision):
            _validate_review_tools(candidate, available_tools)
            replacement = candidate
    if replacement is None or replacement.action == ReviewAction.CONTINUE:
        replacement = ReviewDecision(
            ReviewAction.FINISH,
            "依赖失败后 Root 未提供可运行修订，按有界 Review 预算结束 Plan。",
            final_status="failed",
        )
    try:
        receipt = await handler.apply_review_decision(
            _review_command(identity, replacement), replacement
        )
    except PlanCommandRejected as error:
        if error.code != "BLOCKED_DEPENDENCIES":
            raise
        replacement = ReviewDecision(
            ReviewAction.FINISH,
            "Root 修订后仍没有可运行任务，按有界 Review 预算结束 Plan。",
            final_status="failed",
        )
        receipt = await handler.apply_review_decision(
            _review_command(identity, replacement), replacement
        )
    return replacement, receipt


def _validate_review_tools(decision: ReviewDecision, available_tools: set[str]) -> None:
    """拒绝 Review/Revision 中系统当前没有提供的 Worker 工具。"""
    if decision.action != ReviewAction.REPLAN or decision.revision is None:
        return
    requested_tools: set[str] = set()
    for task in decision.revision.new_tasks:
        draft = PlanTaskDraft.model_validate(task)
        requested_tools.update(draft.allowed_tools)
    unavailable_tools = sorted(requested_tools - available_tools)
    if unavailable_tools:
        raise ValueError(
            "root_review_requests_unavailable_tools: " + ", ".join(unavailable_tools)
        )


def _root_budget_exhausted(turn_count: int) -> dict[str, Any]:
    """构造 Root 轮数耗尽时统一使用的失败状态。"""
    error = {
        "code": "root_budget_exhausted",
        "message": f"Root 已用完允许的 {turn_count} 次决策轮数。",
    }
    return {
        "root_turn_count": turn_count,
        "error": error,
        "response": {"error": error},
        "status": "failed",
    }


def _context_bundle_from_state(value: Mapping[str, Any]) -> ContextBundle:
    """把 checkpoint 中的 JSON 对象恢复成当前参考窗口。"""
    return ContextBundle(
        items=tuple(
            ContextItem(**item)
            for item in value.get("items", ())
            if isinstance(item, Mapping)
        ),
        provider_results=tuple(
            _provider_result_from_state(item)
            for item in value.get("provider_results", ())
            if isinstance(item, Mapping)
        ),
        providers_succeeded=tuple(value.get("providers_succeeded", ())),
        providers_failed=tuple(value.get("providers_failed", ())),
        truncated=bool(value.get("truncated", False)),
        retrieval_run_id=value.get("retrieval_run_id"),
        context_selection=tuple(
            RetrievalCandidateSelectionSchema.model_validate(item).to_domain()
            for item in value.get("context_selection", ())
            if isinstance(item, Mapping)
        ),
    )


def _request_state(state: Mapping[str, Any]) -> RootRequestState:
    """将 checkpoint 中的请求字典恢复为有类型的请求内容。"""
    return RootRequestState.model_validate(state.get("request", {}))


def _provider_result_from_state(value: Mapping[str, Any]) -> ProviderResult:
    """恢复采集服务 checkpoint 中的模块摘要和候选类型。"""
    values = dict(value)
    values["candidates"] = tuple(
        RetrievalCandidateTraceSchema.model_validate(item).to_domain()
        for item in values.get("candidates", ())
        if isinstance(item, Mapping)
    )
    values["channels"] = tuple(
        RetrievalChannelTraceSchema.model_validate(item).to_domain()
        for item in values.get("channels", ())
        if isinstance(item, Mapping)
    )
    return ProviderResult(**values)


def _merge_context_window(
    previous: ContextBundle,
    current: ContextBundle,
    *,
    max_items: int,
    max_tokens: int,
    memory_token_budget: int | None = None,
    token_counter: Callable[[str], int],
) -> ContextBundle:
    """优先保留新资料，再用配置模型的 tokenizer 限制窗口大小。"""
    if memory_token_budget is None:
        memory_token_budget = max_tokens
    selected: list[ContextItem] = []
    seen_sources: set[tuple[str, str]] = set()
    seen_content: set[tuple[str, str]] = set()
    selections: dict[tuple[str, str], RetrievalCandidateSelection] = {}
    tokens = 0
    memory_tokens = 0
    truncated = current.truncated
    all_items = (*current.items, *previous.items)
    for index, item in enumerate(all_items):
        key = _context_item_identity(item)
        is_current = index < len(current.items)
        has_group_identity = item.provider == "memory" and isinstance(
            item.metadata.get("memory_group_key"), str
        )
        if item.source_id or has_group_identity:
            identity = _context_item_identity(item)
            if identity in seen_sources:
                if is_current and key not in selections:
                    selections[key] = RetrievalCandidateSelection(
                        provider=item.provider,
                        source_id=_selection_source_id(item),
                        source_ids=_selection_source_ids(item),
                        context_selected=False,
                        filter_reason="duplicate_in_context_window",
                    )
                continue
            seen_sources.add(identity)
        else:
            identity = (item.provider, item.content)
            if identity in seen_content:
                if is_current and key not in selections:
                    selections[key] = RetrievalCandidateSelection(
                        provider=item.provider,
                        source_id=_selection_source_id(item),
                        source_ids=_selection_source_ids(item),
                        context_selected=False,
                        filter_reason="duplicate_in_context_window",
                    )
                continue
            seen_content.add(identity)
        item_tokens = token_counter(item.content)
        is_memory = item.provider in {"memory", "memory_index", "episode"}
        exceeds_memory_budget = (
            is_memory and memory_tokens + item_tokens > memory_token_budget
        )
        if (
            len(selected) >= max_items
            or tokens + item_tokens > max_tokens
            or exceeds_memory_budget
        ):
            truncated = True
            if is_current:
                selections[key] = RetrievalCandidateSelection(
                    provider=item.provider,
                    source_id=_selection_source_id(item),
                    source_ids=_selection_source_ids(item),
                    context_selected=False,
                    filter_reason=(
                        "context_max_items"
                        if len(selected) >= max_items
                        else (
                            "memory_token_budget"
                            if exceeds_memory_budget
                            else "context_token_budget"
                        )
                    ),
                )
            continue
        selected.append(item)
        tokens += item_tokens
        if is_memory:
            memory_tokens += item_tokens
        if is_current:
            selections[key] = RetrievalCandidateSelection(
                provider=item.provider,
                source_id=_selection_source_id(item),
                source_ids=_selection_source_ids(item),
                context_selected=True,
            )
    return ContextBundle(
        items=tuple(selected),
        provider_results=current.provider_results,
        providers_succeeded=current.providers_succeeded,
        providers_failed=current.providers_failed,
        truncated=truncated,
        retrieval_run_id=current.retrieval_run_id,
        context_selection=tuple(selections.values()),
    )


def _enforce_context_window_budget(
    bundle: ContextBundle,
    *,
    max_items: int,
    max_tokens: int,
    memory_token_budget: int,
    token_counter: Callable[[str], int],
) -> ContextBundle:
    """核验后若记忆组变大，仍按完整 ContextItem 重做窗口预算。"""
    selected: list[ContextItem] = []
    tokens = 0
    memory_tokens = 0
    dropped: dict[tuple[str, str], str] = {}
    for item in bundle.items:
        item_tokens = token_counter(item.content)
        if len(selected) >= max_items:
            dropped[(item.provider, _selection_source_id(item))] = "context_max_items"
            continue
        is_memory = item.provider in {"memory", "memory_index", "episode"}
        if is_memory and memory_tokens + item_tokens > memory_token_budget:
            dropped[(item.provider, _selection_source_id(item))] = "memory_token_budget"
            continue
        if tokens + item_tokens > max_tokens:
            dropped[(item.provider, _selection_source_id(item))] = (
                "context_token_budget"
            )
            continue
        selected.append(item)
        tokens += item_tokens
        if is_memory:
            memory_tokens += item_tokens

    selections = tuple(
        (
            replace(
                selection,
                context_selected=False,
                filter_reason=dropped[(selection.provider, selection.source_id)],
            )
            if selection.context_selected
            and (selection.provider, selection.source_id) in dropped
            else selection
        )
        for selection in bundle.context_selection
    )
    return replace(
        bundle,
        items=tuple(selected),
        truncated=bundle.truncated or bool(dropped),
        context_selection=selections,
    )


def _context_item_identity(item: ContextItem) -> tuple[str, str]:
    group_key = item.metadata.get("memory_group_key")
    if item.provider == "memory" and isinstance(group_key, str) and group_key:
        return item.provider, group_key
    if item.source_id:
        return item.provider, item.source_id
    return item.provider, item.content


def _filter_memory_context_scope(
    bundle: ContextBundle,
    *,
    session_id: str,
    project_id: str | None,
    enabled: bool,
) -> ContextBundle:
    """每次模型调用前按最新会话绑定重新过滤记忆和任务回顾。"""
    retained: list[ContextItem] = []
    rejected_source_ids: set[tuple[str, str]] = set()
    for item in bundle.items:
        if item.provider not in {"memory", "memory_index", "episode"}:
            retained.append(item)
            continue
        allowed = enabled
        if allowed and item.provider == "memory_index":
            allowed = (
                item.metadata.get("session_id") == session_id
                and item.metadata.get("project_id") == project_id
            )
        elif allowed and item.provider == "episode":
            episode_project_id = item.metadata.get("project_id")
            allowed = episode_project_id is None or episode_project_id == project_id
        elif allowed and item.provider == "memory":
            fact_value = item.metadata.get(
                "memory_fact_identity", item.metadata.get("fact")
            )
            fact_scope = (
                fact_value
                if isinstance(fact_value, dict)
                else (
                    fact_value.model_dump(mode="python")
                    if hasattr(fact_value, "model_dump")
                    else None
                )
            )
            if fact_scope is not None:
                scope_kind = fact_scope.get("scope_kind")
                scope_id = fact_scope.get("scope_id")
                allowed = (
                    (scope_kind == "global" and scope_id == "global")
                    or (scope_kind == "session" and scope_id == session_id)
                    or (
                        project_id is not None
                        and scope_kind == "project"
                        and scope_id == project_id
                    )
                )
            else:
                category = item.metadata.get("category", item.metadata.get("type"))
                allowed = (
                    category in {"preference", "profile"}
                    or item.metadata.get("session_id") == session_id
                )
        if allowed:
            retained.append(item)
        else:
            rejected_source_ids.add((item.provider, item.source_id or ""))

    reason = (
        "session_memory_read_disabled"
        if not enabled
        else "memory_scope_binding_changed"
    )
    selections = tuple(
        (
            replace(
                selection,
                context_selected=False,
                source_ids=(),
                filter_reason=reason,
            )
            if selection.context_selected
            and (selection.provider, selection.source_id) in rejected_source_ids
            else selection
        )
        for selection in bundle.context_selection
    )
    if len(retained) == len(bundle.items) and selections == bundle.context_selection:
        return bundle
    return replace(
        bundle,
        items=tuple(retained),
        context_selection=selections,
    )


def _has_context_progress(previous: ContextBundle, current: ContextBundle) -> bool:
    """判断采集后是否新增或替换了非空资料。"""
    previous_content = {
        _context_item_identity(item): item.content.strip()
        for item in previous.items
        if item.content.strip()
    }
    return any(
        item.content.strip()
        and previous_content.get(_context_item_identity(item)) != item.content.strip()
        for item in current.items
    )


def _selection_source_id(item: ContextItem) -> str:
    if item.source_id:
        return item.source_id
    digest = sha256(item.content.encode("utf-8")).hexdigest()[:24]
    return f"inline_{digest}"


def _selection_source_ids(item: ContextItem) -> tuple[str, ...]:
    """展开事实组的真实成员 ID，供选择轨迹和访问统计共用。"""
    raw_ids = item.metadata.get("memory_member_ids")
    member_ids = (
        tuple(value for value in raw_ids if isinstance(value, str) and value)
        if isinstance(raw_ids, (tuple, list))
        else ()
    )
    source_id = _selection_source_id(item)
    if item.provider != "memory":
        return ()
    return tuple(dict.fromkeys((source_id, *member_ids)))


def _filter_context_queries(
    queries: tuple[ContextQuery, ...],
    available_providers: tuple[str, ...],
    stage: RetrievalStage,
) -> tuple[tuple[ContextQuery, ...], list[ContextQueryOutcome]]:
    """过滤未装配的通道，并为 Root 保留明确的跳过原因。"""
    available = tuple(
        query for query in queries if query.provider in available_providers
    )
    skipped = [
        ContextQueryOutcome(
            stage=stage,
            query=query,
            status="skipped",
            result_count=0,
            error_message="context_provider_unavailable",
        )
        for query in queries
        if query.provider not in available_providers
    ]
    return available, skipped


def _context_query_outcomes(
    queries: tuple[ContextQuery, ...],
    bundle: ContextBundle,
    stage: RetrievalStage,
) -> list[ContextQueryOutcome]:
    """将 Provider 结果收敛为 Root 下一轮使用的轻量状态。"""
    results = {result.provider: result for result in bundle.provider_results}
    outcomes: list[ContextQueryOutcome] = []
    for query in queries:
        result = results.get(query.provider)
        if result is None:
            outcomes.append(
                ContextQueryOutcome(
                    stage=stage,
                    query=query,
                    status="failed",
                    result_count=0,
                    error_message="context_provider_result_missing",
                )
            )
            continue
        status = result.status
        if status == "succeeded" and result.result_count == 0:
            status = "empty"
        if status not in {"succeeded", "empty", "failed", "timeout", "skipped"}:
            status = "failed"
        error_message = (result.error_message or "")[:500] or None
        outcomes.append(
            ContextQueryOutcome(
                stage=stage,
                query=query,
                status=cast(
                    Literal["succeeded", "empty", "failed", "timeout", "skipped"],
                    status,
                ),
                result_count=result.result_count,
                error_message=error_message,
            )
        )
    return outcomes


async def _retrieve_context_batch(
    deps: RootWorkflowDependencies,
    *,
    queries: tuple[ContextQuery, ...],
    request: RootRequestState,
    attachment_ids: tuple[str, ...],
    knowledge_base_ids: tuple[str, ...],
    stage: RetrievalStage,
    provider_timeout_seconds: float | None,
    skipped_outcomes: tuple[ContextQueryOutcome, ...] = (),
) -> tuple[ContextBundle, list[ContextQueryOutcome], ContextPlan | None]:
    """校验资源范围并执行一批已筛选的上下文查询。"""
    try:
        plan = deps.context_planner.plan(
            queries,
            attachment_ids=attachment_ids,
            knowledge_base_ids=knowledge_base_ids,
        )
        plan = plan.with_updates(provider_timeout_seconds=provider_timeout_seconds)
        logger.debug(
            "context_plan_generated",
            extra={
                "plan": as_dict(plan),
                "stage": stage,
                "session_id": request.session_id,
                "run_id": request.run_id,
                "message_id": request.message_id,
            },
        )
    except InvalidContextResource as error:
        return (
            ContextBundle(items=(), provider_results=()),
            [
                ContextQueryOutcome(
                    stage=stage,
                    query=query,
                    status="failed",
                    result_count=0,
                    error_message=str(error)[:500],
                )
                for query in queries
            ],
            None,
        )
    bundle = await deps.context_acquisition.retrieve_context(
        session_id=request.session_id,
        run_id=request.run_id,
        message_id=request.message_id,
        invocation_id=uuid4().hex,
        stage=stage,
        plan=plan,
        skipped_outcomes=skipped_outcomes,
    )
    return bundle, _context_query_outcomes(queries, bundle, stage), plan


class RootWorkflow(RootWorkflowPort):
    """Execute Root application steps without depending on LangGraph."""

    def __init__(self, dependencies: RootWorkflowDependencies) -> None:
        self.dependencies = dependencies

    @staticmethod
    def _conversation_messages(state: Mapping[str, Any]) -> tuple[RootMessage, ...]:
        """从请求与历史构造 Root/计划/正文共同使用的对话。"""
        request = _request_state(state)
        current_message_id = request.message_id
        messages = [
            RootMessage(role=item.role, content=item.content)
            for item in request.history
            if item.message_id != current_message_id
        ]
        messages.append(RootMessage(role="user", content=request.user_message))
        return tuple(messages)

    async def _agent_input(
        self,
        state: RootGraphState,
        *,
        remaining_turns: int,
    ) -> RootAgentInput:
        """从当前 checkpoint 构造统一的 Root 决策输入。"""
        request = _request_state(state)
        bundle = await self._effective_context_bundle(
            request.session_id,
            _context_bundle_from_state(state.get("context_bundle", {})),
        )
        feedback_value = state.get("planning_feedback")
        worker_tools = await worker_tool_manifest(self.dependencies.tool_service)
        available_context_providers = cast(
            tuple[Literal["memory", "episode", "file", "knowledge", "graph"], ...],
            self.dependencies.available_context_providers,
        )
        return RootAgentInput(
            session_id=request.session_id,
            run_id=request.run_id,
            message_id=request.message_id,
            messages=RootWorkflow._conversation_messages(state),
            attachments=request.attachment_refs,
            knowledge_bases=request.knowledge_bases,
            worker_tools=worker_tools,
            available_context_providers=available_context_providers,
            context_items=bundle.items,
            context_queries=tuple(
                ContextQueryOutcome.model_validate(item)
                for item in state.get("context_queries", ())
            ),
            remaining_turns=remaining_turns,
            planning_feedback=(
                PlanningFeedback.model_validate(feedback_value)
                if feedback_value is not None
                else None
            ),
            planning_context_updated=bool(state.get("planning_context_updated", False)),
        )

    async def _effective_context_bundle(
        self, session_id: str, bundle: ContextBundle
    ) -> ContextBundle:
        """每次模型调用前应用读取开关并复核记忆仍然有效。"""
        settings_port = self.dependencies.memory_settings
        settings = (
            await settings_port.get(session_id) if settings_port is not None else None
        )
        effective = _filter_memory_context_scope(
            bundle,
            session_id=session_id,
            project_id=None if settings is None else settings.project_id,
            enabled=settings is None or settings.use_memories,
        )
        if (
            effective.items != bundle.items
            or effective.context_selection != bundle.context_selection
        ):
            await self.dependencies.context_acquisition.persist_revalidated_selection(
                effective
            )
        if settings is not None and not settings.use_memories:
            return effective
        validated = await self.dependencies.context_acquisition.validate_memory_context(
            effective
        )
        planner = self.dependencies.context_planner
        effective = _enforce_context_window_budget(
            validated,
            max_items=planner.max_items,
            max_tokens=planner.max_tokens,
            memory_token_budget=planner.memory_token_budget,
            token_counter=planner.count_tokens,
        )
        if (
            effective.items != bundle.items
            or effective.context_selection != bundle.context_selection
        ):
            await self.dependencies.context_acquisition.persist_revalidated_selection(
                effective
            )
        return effective

    async def prepare_request(self, state: RootGraphState) -> dict[str, Any]:
        deps = self.dependencies
        request_state = _request_state(state)
        updated_request = await deps.request_preparation.prepare(request=request_state)

        if not updated_request.history:
            history = await deps.request_preparation.load_history(
                session_id=updated_request.session_id,
                exclude_message_id=updated_request.message_id,
                before_sequence=updated_request.run_sequence,
            )
            updated_request = RootRequestState.model_validate(
                {
                    **updated_request.model_dump(mode="python"),
                    "history": history,
                }
            )
        updated_request = updated_request.model_copy(
            update={
                "knowledge_bases": (
                    await deps.request_preparation.load_knowledge_bases()
                )
            }
        )

        return {
            "request": updated_request.model_dump(mode="json"),
        }

    async def prefetch_context(self, state: RootGraphState) -> dict[str, Any]:
        """用原始问题预检索记忆、可用任务回顾和就绪知识库，不占决策轮数。

        参数：state 是请求准备后的 RootGraphState，包含原文与资源目录。
        返回：dict，更新同一资料窗口和带阶段的查询摘要。
        异常：资源契约错误向上传播；取消时传播 CancelledError。
        """
        request = _request_state(state)
        query = request.user_message.strip()[:500]
        if not query:
            return {}

        ready_ids = tuple(
            item.id for item in request.knowledge_bases if item.ready_document_count > 0
        )
        queries: list[ContextQuery] = []
        if "memory" in self.dependencies.available_context_providers:
            queries.append(MemoryQuery(provider="memory", query=query))
        if "episode" in self.dependencies.available_context_providers:
            queries.append(EpisodeQuery(provider="episode", query=query))
        if "knowledge" in self.dependencies.available_context_providers:
            queries.append(
                KnowledgeQuery(
                    provider="knowledge", query=query, knowledge_base_ids=ready_ids
                )
            )

        return await self._acquire_context_queries(
            state,
            queries=tuple(queries),
            stage="prefetch",
            provider_timeout_seconds=self.dependencies.context_prefetch_timeout_seconds,
        )

    async def decide_root_action(self, state: RootGraphState) -> dict[str, Any]:
        """调用 Root 一次并保存决策及本次轮数。
        Args:
            state: 已准备请求、资源目录和当前检索窗口的图状态。
        Returns:
            新决策、更新后的轮数以及适用的响应或错误。
        Raises:
            ValidationError: checkpoint 中保存的输入摘要不符合 Root 契约。
            ValueError: Root 无法修复无效决策时。
        """
        deps = self.dependencies
        turn_count = state.get("root_turn_count", 0)
        remaining_turns = deps.root_max_turns - turn_count
        if remaining_turns < 1:
            logger.warning(f"Root 已用完允许的 {deps.root_max_turns} 次决策轮数。")
            return _root_budget_exhausted(turn_count)
        logger.info(f"Root 决策轮数 {turn_count}，剩余 {remaining_turns} 轮。")

        inputs = await self._agent_input(state, remaining_turns=remaining_turns)
        decision = ROOT_DECISION_ADAPTER.validate_python(
            await deps.agent.decide(inputs)
        )
        next_turn_count = turn_count + 1
        serialized_decision = as_dict(decision)
        update: dict[str, Any] = {
            "root_decision": serialized_decision,
            "root_turn_count": next_turn_count,
        }
        if isinstance(decision, AnswerIntent):
            update["answer_input"] = RootAnswerInputState(
                source="decision",
                kind=decision.kind,
                instructions=decision.instructions,
            ).model_dump(mode="json")
            update.update(
                planning_feedback=None,
                planning_context_updated=False,
            )
        elif (
            isinstance(decision, PlanIntent)
            and inputs.planning_feedback
            and not inputs.planning_context_updated
        ):
            return {
                **update,
                "error": {
                    "code": "root_planning_without_new_information",
                    "message": "规划反馈后没有获得新资料，不能重复生成同一计划。",
                },
                "status": "failed",
            }
        elif (
            isinstance(decision, ContextRequest)
            and next_turn_count >= deps.root_max_turns
        ):
            update.update(_root_budget_exhausted(next_turn_count))
        return update

    async def redecide_root_action(self, state: RootGraphState) -> dict[str, Any]:
        """采集完成后按三结果契约作一次再决策。"""
        deps = self.dependencies
        turn_count = int(state.get("root_turn_count", 0))
        remaining_turns = deps.root_max_turns - turn_count
        if remaining_turns < 1:
            logger.warning(f"Root 已用完允许的 {deps.root_max_turns} 次重新决策轮数。")
            return _root_budget_exhausted(turn_count)
        logger.info(f"Root 重新决策轮数 {turn_count}，剩余 {remaining_turns} 轮。")
        inputs = await self._agent_input(state, remaining_turns=remaining_turns)

        decision = ROOT_REDECISION_ADAPTER.validate_python(
            await deps.agent.redecide(inputs)
        )
        next_turn_count = turn_count + 1
        update: dict[str, Any] = {
            "root_decision": as_dict(decision),
            "root_turn_count": next_turn_count,
        }
        if isinstance(decision, AnswerIntent):
            update["answer_input"] = RootAnswerInputState(
                source="decision",
                kind=decision.kind,
                instructions=decision.instructions,
            ).model_dump(mode="json")
            update.update(
                planning_feedback=None,
                planning_context_updated=False,
            )
        elif inputs.planning_feedback and not inputs.planning_context_updated:
            return {
                **update,
                "error": {
                    "code": "root_planning_without_new_information",
                    "message": "规划反馈后没有获得新资料，不能重复生成同一计划。",
                },
                "status": "failed",
            }
        return update

    async def acquire_context(self, state: RootGraphState) -> dict[str, Any]:
        """执行 Root 请求的检索并更新有界参考资料窗口。
        Args:
            state: 包含 ContextRequest、资源目录及旧资料窗口的图状态。
        Returns:
            新资料窗口和本轮各查询的轻量结果摘要。
        Raises:
            CancelledError: Run 被取消时由采集服务传播。
            ValueError: 图状态中的决策不再是有效 ContextRequest 时。
        """
        decision = ROOT_DECISION_ADAPTER.validate_python(state.get("root_decision", {}))
        if not isinstance(decision, ContextRequest):
            raise ValueError("root_context_request_missing")
        return await self._acquire_context_queries(
            state, queries=decision.queries, stage="directed"
        )

    async def _acquire_context_queries(
        self,
        state: RootGraphState,
        *,
        queries: tuple[ContextQuery, ...],
        stage: RetrievalStage,
        provider_timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """共用两阶段的范围校验、采集、窗口合并与选择确认。

        参数：state 提供请求与旧窗口；queries 为具体查询；stage 由程序指定；
        provider_timeout_seconds 仅在预检索设置。
        返回：dict，包含新的窗口、累积摘要和规划进展。
        异常：取消时传播 CancelledError；损坏的状态契约向上传播。
        """
        deps = self.dependencies
        request_state = _request_state(state)

        attachment_ids = tuple(item.id for item in request_state.attachment_refs)
        knowledge_base_ids = tuple(item.id for item in request_state.knowledge_bases)
        available_queries, unavailable_outcomes = _filter_context_queries(
            queries,
            self.dependencies.available_context_providers,
            stage,
        )
        skipped = list(unavailable_outcomes)
        new_bundle = ContextBundle(items=(), provider_results=())
        if available_queries:
            new_bundle, query_outcomes, context_plan = await _retrieve_context_batch(
                deps,
                queries=available_queries,
                request=request_state,
                attachment_ids=attachment_ids,
                knowledge_base_ids=knowledge_base_ids,
                stage=stage,
                provider_timeout_seconds=provider_timeout_seconds,
                skipped_outcomes=tuple(skipped),
            )
            skipped.extend(query_outcomes)
        else:
            context_plan = None
            await deps.context_acquisition.publish_skipped_provider_outcomes(
                tuple(skipped),
                session_id=request_state.session_id,
                run_id=request_state.run_id,
                stage=stage,
            )

        previous_bundle = _context_bundle_from_state(state.get("context_bundle", {}))
        max_items = (
            context_plan.max_items
            if context_plan is not None
            else deps.context_planner.max_items
        )
        max_tokens = (
            context_plan.max_tokens
            if context_plan is not None
            else deps.context_planner.max_tokens
        )
        memory_token_budget = (
            context_plan.memory_token_budget
            if context_plan is not None
            else deps.context_planner.memory_token_budget
        )
        merged_bundle = _merge_context_window(
            previous_bundle,
            new_bundle,
            max_items=max_items,
            max_tokens=max_tokens,
            memory_token_budget=memory_token_budget,
            token_counter=deps.context_planner.count_tokens,
        )
        memory_settings = (
            await deps.memory_settings.get(request_state.session_id)
            if deps.memory_settings is not None
            else None
        )
        merged_bundle = _filter_memory_context_scope(
            merged_bundle,
            session_id=request_state.session_id,
            project_id=(
                None if memory_settings is None else memory_settings.project_id
            ),
            enabled=memory_settings is None or memory_settings.use_memories,
        )
        merged_bundle = await deps.context_acquisition.validate_memory_context(
            merged_bundle
        )
        merged_bundle = _enforce_context_window_budget(
            merged_bundle,
            max_items=max_items,
            max_tokens=max_tokens,
            memory_token_budget=memory_token_budget,
            token_counter=deps.context_planner.count_tokens,
        )
        context_progress = _has_context_progress(previous_bundle, merged_bundle)
        await deps.context_acquisition.confirm_selection(
            run_id=new_bundle.retrieval_run_id,
            session_id=request_state.session_id,
            agent_run_id=request_state.run_id,
            selections=merged_bundle.context_selection,
            stage=stage,
        )
        previous_outcomes = tuple(
            ContextQueryOutcome.model_validate(item)
            for item in state.get("context_queries", ())
        )
        return {
            "context_bundle": as_dict(merged_bundle),
            "planning_context_updated": (
                bool(state.get("planning_context_updated", False))
                or (stage == "directed" and context_progress)
            ),
            "context_queries": [
                outcome.model_dump(mode="json")
                for outcome in (*previous_outcomes, *skipped)
            ],
        }

    async def generate_plan(self, state: RootGraphState) -> dict[str, Any]:
        """根据轻量计划意图生成草稿或返回必要信息缺口。"""
        deps = self.dependencies
        request_state = _request_state(state)
        decision = ROOT_DECISION_ADAPTER.validate_python(state.get("root_decision", {}))
        if not isinstance(decision, PlanIntent):
            return {"error": {"code": "root_plan_intent_missing"}, "status": "failed"}
        messages = self._conversation_messages(state)
        bundle = await self._effective_context_bundle(
            request_state.session_id,
            _context_bundle_from_state(state.get("context_bundle", {})),
        )
        worker_tools = await worker_tool_manifest(self.dependencies.tool_service)

        plan_input = RootPlanInput(
            session_id=request_state.session_id,
            run_id=request_state.run_id,
            message_id=request_state.message_id,
            messages=messages,
            attachments=request_state.attachment_refs,
            knowledge_bases=request_state.knowledge_bases,
            worker_tools=worker_tools,
            context_items=bundle.items,
            context_queries=tuple(
                ContextQueryOutcome.model_validate(item)
                for item in state.get("context_queries", ())
            ),
            goal=decision.goal,
            instructions=decision.instructions,
        )
        result = ROOT_PLAN_ADAPTER.validate_python(await deps.agent.plan(plan_input))
        if isinstance(result, PlanningFeedback):
            return {
                "planning_feedback": result.model_dump(mode="json"),
                "planning_context_updated": False,
                "plan_draft": None,
                "answer_input": None,
            }
        return {
            "plan_draft": result.model_dump(mode="json"),
            "planning_feedback": None,
            "planning_context_updated": False,
            "worker_tools": [tool.model_dump(mode="json") for tool in worker_tools],
        }

    async def materialize_execution_plan(self, state: RootGraphState) -> dict[str, Any]:
        """验证并提交首版计划，再写回可恢复的运行状态。

        参数：``state``（``RootGraphState``）包含请求、计划决策和系统工具目录。
        返回：提交后的计划、延期版本及运行状态。
        异常：草稿、资源范围或计划命令无效时向图节点传播异常。
        """
        deps = self.dependencies
        request_state = _request_state(state)
        draft_value = state.get("plan_draft")
        if not isinstance(draft_value, Mapping):
            raise ValueError("root_plan_draft_missing")
        decision = PlanDraft.model_validate(draft_value)
        # 将显式版本拆分放在计划应用阶段，避免 Agent 解析器隐式改变其输出。
        current_values, deferred_versions = partition_plan_versions(
            decision.model_dump(mode="json", exclude_unset=True)
        )
        current_draft = PlanDraft.model_validate(current_values)
        plan, tasks = coerce_plan(current_draft, request=request_state)
        run_id = request_state.run_id
        worker_tools = {
            str(item["name"])
            for item in state.get("worker_tools", [])
            if isinstance(item, Mapping) and item.get("name")
        }
        plan, tasks = scope_execution_plan(
            plan,
            tasks,
            run_id=run_id,
            worker_tools=worker_tools,
        )
        if deps.orchestration_command_handler is None:
            raise RuntimeError("root_orchestration_command_handler_not_configured")
        if not run_id:
            raise ValueError("run_id is required for SubmitPlan")
        command = OrchestrationCommand(
            message_id=f"root-submit-plan:{run_id}",
            command_type=OrchestrationCommandType.SUBMIT_PLAN,
            idempotency_key=f"submit-plan:{run_id}",
            correlation_id=run_id,
            occurred_at=datetime.now(timezone.utc),
            plan_id=plan.plan_id,
            scope_key=run_id,
            plan_version=1,
            payload={"source": "root_agent"},
        )
        # Plan、Round、Worker Job 和 Outbox 仍由原命令处理器在同一事务提交。
        await deps.orchestration_command_handler.submit_plan(
            command,
            plan,
            tasks,
            legal_tools=worker_tools,
        )
        return {
            "plan": plan_dict(plan, tasks),
            "deferred_plan_versions": deferred_versions,
            "status": "running",
        }

    async def wait_for_plan_completion(self, state: RootGraphState) -> dict[str, Any]:
        deps = self.dependencies
        request_state = _request_state(state)
        plan = RootPlanIdentity.model_validate(state.get("plan", {}))
        if deps.plan_loader is None:
            return {
                "waiting": True,
                "status": "waiting_for_plan",
                "root_wait": {
                    "type": "round_wait",
                    "run_id": request_state.run_id,
                    "plan_id": plan.plan_id,
                },
            }
        loaded = await deps.plan_loader(plan_id=plan.plan_id)
        if isinstance(loaded, Mapping) and loaded.get("completed"):
            return {
                "waiting": False,
                "plan_results": json_value(loaded.get("results", {})),
            }
        wait_context = (
            dict(loaded.get("root_wait", {}))
            if isinstance(loaded, Mapping)
            and isinstance(loaded.get("root_wait"), Mapping)
            else {}
        )
        wait_context.setdefault("type", "round_wait")
        wait_context.setdefault("run_id", request_state.run_id)
        wait_context.setdefault("plan_id", plan.plan_id)
        return {
            "waiting": True,
            "status": "waiting_for_plan",
            "root_wait": wait_context,
        }

    async def review_completed_round(self, state: RootGraphState) -> dict[str, Any]:
        """恢复固定 Round 身份，取得审查决策并原子提交对应命令。
        Args:
            state: 包含 Run 请求、Root 等待身份和恢复值的图状态。
        Returns:
            已持久化审查动作、理由及命令回执。
        Raises:
            RuntimeError: 恢复身份、审查上下文、Agent 或命令处理器无效时。
            PlanCommandRejected: 非可恢复的计划状态转换被拒绝时。
        """
        deps = self.dependencies
        identity = _review_identity(state)
        handler = deps.orchestration_command_handler
        if handler is None:
            raise RuntimeError("root_review_command_handler_not_configured")
        request = _request_state(state).model_dump(mode="python")
        decision, context, worker_tools = await _load_or_create_review_decision(
            deps, identity, request, state
        )
        decision, receipt = await _apply_review_decision_with_fallback(
            deps, identity, decision, context, request, worker_tools
        )
        return {
            "review_action": decision.action.value,
            "review_reason": decision.reason,
            "review_receipt": as_dict(receipt),
        }

    async def build_plan_response(self, state: RootGraphState) -> dict[str, Any]:
        """把完成计划的任务结果整理为统一正文生成输入。"""
        answer_input = RootAnswerInputState(
            source="plan_summary",
            kind="final",
            instructions="根据用户原始目标总结已完成的任务结果，准确保留失败项、不确定性和未执行事实。",
        )
        return {
            "answer_input": answer_input.model_dump(mode="json"),
            "waiting": False,
        }

    async def generate_answer(self, state: RootGraphState) -> dict[str, Any]:
        """流式生成直接答复、澄清问题或计划完成总结。"""
        from .answer_generation import AnswerGenerationInput

        deps = self.dependencies
        request = _request_state(state)
        answer_input_value = state.get("answer_input")
        if isinstance(answer_input_value, Mapping):
            answer_input = RootAnswerInputState.model_validate(answer_input_value)
        else:
            decision = ROOT_DECISION_ADAPTER.validate_python(
                state.get("root_decision", {})
            )
            if not isinstance(decision, AnswerIntent):
                raise ValueError("root_answer_intent_missing")
            answer_input = RootAnswerInputState(
                source="decision",
                kind=decision.kind,
                instructions=decision.instructions,
            )
        messages = self._conversation_messages(state)
        bundle = await self._effective_context_bundle(
            request.session_id,
            _context_bundle_from_state(state.get("context_bundle", {})),
        )
        result = await deps.answer_generation.generate(
            AnswerGenerationInput(
                session_id=request.session_id,
                run_id=request.run_id,
                message_id=request.message_id,
                kind=answer_input.kind,
                instructions=answer_input.instructions,
                messages=tuple(messages),
                context_items=bundle.items,
                context_queries=tuple(
                    ContextQueryOutcome.model_validate(item)
                    for item in state.get("context_queries", ())
                ),
                plan_results=(
                    dict(state["plan_results"])
                    if answer_input.source == "plan_summary"
                    and isinstance(state.get("plan_results"), Mapping)
                    else None
                ),
            )
        )
        return {
            "response": result.model_dump(mode="json"),
            "status": "completed",
        }

    async def assemble_final_response(self, state: RootGraphState) -> dict[str, Any]:
        """Build the terminal result and status from the accumulated graph state."""
        response = dict(state.get("response", {}))
        if not response and state.get("error"):
            response = {"error": state.get("error")}
        result = dict(state.get("result", {}))
        if response and not result:
            result = response
        if state.get("waiting"):
            return {"result": result, "status": "waiting", "waiting": True}
        failed = bool(state.get("error") or response.get("error"))
        return {
            "result": result,
            "status": "failed" if failed else "completed",
            "waiting": False,
        }


__all__ = [
    "RootWorkflow",
    "render_plan_results",
]
