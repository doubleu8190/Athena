"""Root run workflow independent from LangGraph.

This module owns the application steps required to prepare a request, acquire
context, execute an agent and materialize a worker plan.  The LangGraph
adapter only supplies state and connects these steps into a graph.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

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
from .root_decision import (
    Answer,
    ContextRequest,
    PlanDraft,
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
    ContextQueryOutcome,
    ProviderResult,
)
from application.runs.context.planner import InvalidContextResource
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
from .serialization import as_dict, json_value
from .worker_tools import worker_tool_manifest

if TYPE_CHECKING:
    from .langgraph.graphs.state import RootGraphState


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
    resume_value = state.get("resume_value", {})
    wait_context = state.get("root_wait", {})
    if not isinstance(resume_value, Mapping) or not isinstance(wait_context, Mapping):
        raise RuntimeError("root_review_resume_identity_missing")
    if resume_value.get("type") != "round_completed":
        raise RuntimeError("root_review_resume_type_invalid")
    identity_fields = ("run_id", "plan_id", "round_id", "plan_version")
    identities_match = all(
        name in resume_value
        and name in wait_context
        and resume_value.get(name) == wait_context.get(name)
        for name in identity_fields
    )
    if not identities_match:
        raise RuntimeError("root_review_resume_identity_mismatch")
    request = state.get("request", {})
    if not isinstance(request, Mapping) or request.get("run_id") != resume_value.get(
        "run_id"
    ):
        raise RuntimeError("root_review_run_identity_mismatch")
    plan_version = resume_value["plan_version"]
    if not isinstance(plan_version, int) or isinstance(plan_version, bool):
        raise RuntimeError("root_review_plan_version_invalid")
    round_id = str(resume_value["round_id"])
    return _ReviewIdentity(
        run_id=str(resume_value["run_id"]),
        plan_id=str(resume_value["plan_id"]),
        round_id=round_id,
        plan_version=plan_version,
        interrupt_id=(
            str(resume_value["interrupt_id"])
            if resume_value.get("interrupt_id")
            else None
        ),
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


def _validate_review_tools(
    decision: ReviewDecision, available_tools: set[str]
) -> None:
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
            "root_review_requests_unavailable_tools: "
            + ", ".join(unavailable_tools)
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
            ProviderResult(**item)
            for item in value.get("provider_results", ())
            if isinstance(item, Mapping)
        ),
        providers_succeeded=tuple(value.get("providers_succeeded", ())),
        providers_failed=tuple(value.get("providers_failed", ())),
        truncated=bool(value.get("truncated", False)),
    )


def _merge_context_window(
    previous: ContextBundle,
    current: ContextBundle,
    *,
    max_items: int,
    max_tokens: int,
    token_counter: Callable[[str], int],
) -> ContextBundle:
    """优先保留新资料，再用配置模型的 tokenizer 限制窗口大小。"""
    selected: list[ContextItem] = []
    seen_sources: set[tuple[str, str]] = set()
    seen_content: set[tuple[str, str]] = set()
    tokens = 0
    truncated = current.truncated
    for item in (*current.items, *previous.items):
        if item.source_id:
            identity = (item.provider, item.source_id)
            if identity in seen_sources:
                continue
            seen_sources.add(identity)
        else:
            identity = (item.provider, item.content)
            if identity in seen_content:
                continue
            seen_content.add(identity)
        item_tokens = token_counter(item.content)
        if len(selected) >= max_items or tokens + item_tokens > max_tokens:
            truncated = True
            continue
        selected.append(item)
        tokens += item_tokens
    return ContextBundle(
        items=tuple(selected),
        provider_results=current.provider_results,
        providers_succeeded=current.providers_succeeded,
        providers_failed=current.providers_failed,
        truncated=truncated,
    )


def _filter_context_queries(
    queries: tuple[ContextQuery, ...],
    available_providers: set[str],
) -> tuple[tuple[ContextQuery, ...], list[ContextQueryOutcome]]:
    """过滤未装配的通道，并为 Root 保留明确的跳过原因。"""
    available = tuple(
        query for query in queries if query.provider in available_providers
    )
    skipped = [
        ContextQueryOutcome(
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
) -> list[ContextQueryOutcome]:
    """将 Provider 结果收敛为 Root 下一轮使用的轻量状态。"""
    results = {result.provider: result for result in bundle.provider_results}
    outcomes: list[ContextQueryOutcome] = []
    for query in queries:
        result = results.get(query.provider)
        if result is None:
            outcomes.append(
                ContextQueryOutcome(
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
                query=query,
                status=status,
                result_count=result.result_count,
                error_message=error_message,
            )
        )
    return outcomes


async def _retrieve_context_batch(
    deps: RootWorkflowDependencies,
    *,
    queries: tuple[ContextQuery, ...],
    request: Mapping[str, Any],
    attachment_ids: tuple[str, ...],
    knowledge_base_ids: tuple[str, ...],
) -> tuple[ContextBundle, list[ContextQueryOutcome], ContextPlan | None]:
    """校验资源范围并执行一批已筛选的上下文查询。"""
    try:
        plan = deps.context_planner.plan(
            queries,
            attachment_ids=attachment_ids,
            knowledge_base_ids=knowledge_base_ids,
        )
    except InvalidContextResource as error:
        return (
            ContextBundle(items=(), provider_results=()),
            [
                ContextQueryOutcome(
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
        session_id=str(request["session_id"]),
        run_id=str(request["run_id"]),
        invocation_id=uuid4().hex,
        plan=plan,
    )
    return bundle, _context_query_outcomes(queries, bundle), plan


class RootWorkflow(RootWorkflowPort):
    """Execute Root application steps without depending on LangGraph."""

    def __init__(self, dependencies: RootWorkflowDependencies) -> None:
        self.dependencies = dependencies

    async def prepare_request(self, state: RootGraphState) -> dict[str, Any]:
        deps = self.dependencies
        request = dict(state.get("request", {}))
        request.setdefault("session_id", "")
        request.setdefault("run_id", "")
        request.setdefault("user_message", "")
        request["attachment_ids"] = list(
            dict.fromkeys(request.get("attachment_ids", []))
        )
        update = await deps.request_preparation.prepare(request=request)
        extra: dict[str, Any] = {}
        if isinstance(update, Mapping):
            request.update(json_value(update.get("request", update)))
            extra = {
                key: json_value(value)
                for key, value in update.items()
                if key != "request"
            }
        if "history" not in request:
            request["history"] = json_value(
                await deps.request_preparation.load_history(
                    session_id=request["session_id"],
                    exclude_message_id=str(request["message_id"]),
                )
            )
        if request["attachment_ids"] and "attachment_refs" not in request:
            request["attachment_refs"] = json_value(
                await deps.request_preparation.load_attachments(
                    session_id=request["session_id"],
                    attachment_ids=request["attachment_ids"],
                )
            )
        knowledge_bases = await deps.request_preparation.load_knowledge_bases()
        worker_tools = await worker_tool_manifest(deps.tool_service)
        return {
            "request": json_value(request),
            "knowledge_bases": json_value(knowledge_bases),
            "worker_tools": json_value(worker_tools),
            "available_context_providers": list(
                deps.available_context_providers
            ),
            "root_turn_count": int(state.get("root_turn_count", 0)),
            "context_bundle": json_value(
                state.get(
                    "context_bundle",
                    {
                        "items": [],
                        "provider_results": [],
                        "truncated": False,
                    },
                )
            ),
            "context_queries": list(state.get("context_queries", [])),
            **extra,
        }

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
        request = state["request"]
        turn_count = state.get("root_turn_count", 0)
        remaining_turns = deps.root_max_turns - turn_count
        if remaining_turns < 1:
            return _root_budget_exhausted(turn_count)

        current_message_id = str(request["message_id"])
        messages = [
            RootMessage(role=item["role"], content=item["content"])
            for item in request.get("history", [])
            if isinstance(item, Mapping)
            and item.get("message_id") != current_message_id
        ]
        messages.append(
            RootMessage(role="user", content=str(request.get("user_message", "")))
        )
        bundle = _context_bundle_from_state(state.get("context_bundle", {}))
        inputs = RootAgentInput(
            session_id=str(request["session_id"]),
            run_id=str(request["run_id"]),
            message_id=str(request["message_id"]),
            messages=tuple(messages),
            attachments=tuple(request.get("attachment_refs", ())),
            knowledge_bases=tuple(state.get("knowledge_bases", ())),
            worker_tools=tuple(state.get("worker_tools", ())),
            available_context_providers=tuple(
                state.get("available_context_providers", ())
            ),
            context_items=bundle.items,
            context_queries=tuple(
                ContextQueryOutcome.model_validate(item)
                for item in state.get("context_queries", ())
            ),
            remaining_turns=remaining_turns,
        )
        decision = ROOT_DECISION_ADAPTER.validate_python(
            await deps.agent.decide(inputs)
        )
        next_turn_count = turn_count + 1
        serialized_decision = (
            decision.model_dump(mode="json", exclude_unset=True)
            if isinstance(decision, PlanDraft)
            else as_dict(decision)
        )
        update: dict[str, Any] = {
            "root_decision": serialized_decision,
            "root_turn_count": next_turn_count,
        }
        if isinstance(decision, Answer):
            update["response"] = {
                "content": decision.content,
                "kind": decision.kind,
            }
            update["status"] = "completed"
        elif (
            isinstance(decision, ContextRequest)
            and next_turn_count >= deps.root_max_turns
        ):
            update.update(_root_budget_exhausted(next_turn_count))
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
        deps = self.dependencies
        request = state["request"]
        decision = ROOT_DECISION_ADAPTER.validate_python(state.get("root_decision", {}))
        if not isinstance(decision, ContextRequest):
            raise ValueError("root_context_request_missing")

        attachment_ids = tuple(
            str(item["id"])
            for item in request.get("attachment_refs", ())
            if isinstance(item, Mapping) and item.get("id")
        )
        knowledge_base_ids = tuple(
            str(item["id"])
            for item in state.get("knowledge_bases", ())
            if isinstance(item, Mapping) and item.get("id")
        )
        available_queries, outcomes = _filter_context_queries(
            decision.queries,
            set(state.get("available_context_providers", ())),
        )
        new_bundle = ContextBundle(items=(), provider_results=())
        if available_queries:
            new_bundle, query_outcomes, context_plan = await _retrieve_context_batch(
                deps,
                queries=available_queries,
                request=request,
                attachment_ids=attachment_ids,
                knowledge_base_ids=knowledge_base_ids,
            )
            outcomes.extend(query_outcomes)
        else:
            context_plan = None

        previous_bundle = _context_bundle_from_state(
            state.get("context_bundle", {})
        )
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
        merged_bundle = _merge_context_window(
            previous_bundle,
            new_bundle,
            max_items=max_items,
            max_tokens=max_tokens,
            token_counter=deps.context_planner.count_tokens,
        )
        previous_outcomes = tuple(
            ContextQueryOutcome.model_validate(item)
            for item in state.get("context_queries", ())
        )
        return {
            "context_bundle": as_dict(merged_bundle),
            "context_queries": [
                outcome.model_dump(mode="json")
                for outcome in (*previous_outcomes, *outcomes)
            ],
        }

    async def materialize_execution_plan(self, state: RootGraphState) -> dict[str, Any]:
        """验证并提交首版计划，再写回可恢复的运行状态。

        参数：``state``（``RootGraphState``）包含请求、计划决策和系统工具目录。
        返回：提交后的计划、延期版本及运行状态。
        异常：草稿、资源范围或计划命令无效时向图节点传播异常。
        """
        deps = self.dependencies
        request = state.get("request", {})
        decision = ROOT_DECISION_ADAPTER.validate_python(state.get("root_decision", {}))
        if not isinstance(decision, PlanDraft):
            raise ValueError("root_plan_draft_missing")
        # 将显式版本拆分放在计划应用阶段，避免 Agent 解析器隐式改变其输出。
        current_values, deferred_versions = partition_plan_versions(
            decision.model_dump(mode="json", exclude_unset=True)
        )
        current_draft = PlanDraft.model_validate(current_values)
        plan, tasks = coerce_plan(current_draft, request=request)
        run_id = str(request.get("run_id") or "").strip()
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
        plan = state.get("plan", {})
        if deps.plan_loader is None:
            return {
                "waiting": True,
                "status": "waiting_for_plan",
                "root_wait": {
                    "type": "round_wait",
                    "run_id": str(state.get("request", {}).get("run_id", "")),
                    "plan_id": str(plan.get("plan_id", "")),
                },
            }
        loaded = await deps.plan_loader(plan_id=plan.get("plan_id"))
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
        wait_context.setdefault(
            "run_id", str(state.get("request", {}).get("run_id", ""))
        )
        wait_context.setdefault("plan_id", str(plan.get("plan_id", "")))
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
        request = state.get("request", {})
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
        """Render the results already loaded by the completed-plan wait step."""
        plan = state.get("plan", {})
        results = state.get("plan_results", {})
        if not isinstance(results, Mapping):
            results = {}
        return {
            "response": {
                "content": render_plan_results(
                    results,
                    plan.get("tasks", []),
                )
            },
            "waiting": False,
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
