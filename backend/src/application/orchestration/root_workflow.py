"""Root run workflow independent from LangGraph.

This module owns the application steps required to prepare a request, acquire
context, execute an agent and materialize a worker plan.  The LangGraph
adapter only supplies state and connects these steps into a graph.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping

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
from .plan_materialization import (
    coerce_plan,
    plan_dict,
    plan_generation_failure,
    scope_execution_plan,
)
from .plan_versions import deferred_version_review_decision
from .protocols import RootWorkflowPort
from .serialization import as_dict, json_value
from .worker_tools import list_worker_tools, worker_tool_manifest

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
    """Extract user-facing text from the Worker result envelope."""
    if isinstance(value, Mapping):
        for key in ("output", "content", "value"):
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
) -> tuple[ReviewDecision, ReviewContext | None]:
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
        return decision, None
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
    # 延后版本边界可由持久计划确定，避免无必要的模型调用。
    decision = deferred_version_review_decision(context, state)
    if decision is None:
        decision = await deps.agent.review(context, request=request)
    if not isinstance(decision, ReviewDecision):
        raise RuntimeError("root_review_decision_invalid")
    return decision, context


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
        candidate = await deps.agent.review(blocked_context, request=request)
        if isinstance(candidate, ReviewDecision):
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
        update = await deps.request_preparation.prepare(request=request, state=state)
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
                    request=request,
                )
            )
        if request["attachment_ids"] and "attachment_refs" not in request:
            request["attachment_refs"] = json_value(
                await deps.request_preparation.load_attachments(
                    session_id=request["session_id"],
                    attachment_ids=request["attachment_ids"],
                    request=request,
                )
            )
        return {"request": json_value(request), **extra}

    async def understand_task(self, state: RootGraphState) -> dict[str, Any]:
        deps = self.dependencies
        request = state.get("request", {})
        result = await deps.task_understanding.understand(
            user_message=request.get("user_message", ""),
            history=request.get("history", []),
            attachment_refs=request.get("attachment_refs", []),
            knowledge_bases=await deps.request_preparation.load_knowledge_bases(
                request
            ),
        )
        payload = as_dict(result)
        payload["task_spec"] = as_dict(payload.get("task"))
        return {"understanding": payload}

    async def clarification_response(self, state: RootGraphState) -> dict[str, Any]:
        understanding = state.get("understanding", {})
        task_spec = understanding.get("task_spec", {})
        text = (
            understanding.get("clarification_question")
            or task_spec.get("clarification_question")
            or "请补充任务目标。"
        )
        return {
            "response": {"content": str(text), "kind": "clarification"},
            "status": "completed",
        }

    async def plan_context(self, state: RootGraphState) -> dict[str, Any]:
        deps = self.dependencies
        task = state.get("understanding", {}).get("task_spec", {})
        request = state.get("request", {})
        planned = deps.context_planner.plan(
            task, requested_file_ids=request.get("attachment_ids", [])
        )
        return {"context_plan": as_dict(planned)}

    async def acquire_context(self, state: RootGraphState) -> dict[str, Any]:
        deps = self.dependencies
        from application.runs.context import ContextPlan

        request = state.get("request", {})
        raw_plan = state.get("context_plan", {})
        context_plan = ContextPlan(**raw_plan)
        value = await deps.context_acquisition.retrieve_context(
            session_id=request.get("session_id", ""),
            task=state.get("understanding", {}).get("task_spec", {}),
            plan=context_plan,
            run_id=request.get("run_id"),
        )
        return {"context_bundle": as_dict(value)}

    async def prepare_harness_input(self, state: RootGraphState) -> dict[str, Any]:
        request = state.get("request", {})
        task = state.get("understanding", {}).get("task_spec", {})
        worker_tools = (
            await worker_tool_manifest(self.dependencies.tool_service)
            if isinstance(task, Mapping) and task.get("mode") == "plan"
            else ()
        )
        return {
            "harness_input": {
                "request": {
                    "session_id": request.get("session_id", ""),
                    "run_id": request.get("run_id", ""),
                },
                "messages": list(request.get("history", []))
                + [{"role": "user", "content": request.get("user_message", "")}],
                "task": task,
                "context": state.get("context_bundle", {}),
                "available_worker_tools": list(worker_tools),
            }
        }

    async def run_root_execution_loop(self, state: RootGraphState) -> dict[str, Any]:
        deps = self.dependencies
        payload = as_dict(
            await deps.agent(harness_input=state.get("harness_input", {}))
        )
        route = payload.get("next_route") or payload.get("route")
        task = state.get("understanding", {}).get("task_spec", {})
        if task.get("mode") == "plan" and not payload.get("response", {}).get("error"):
            route = "materialize_execution_plan"
        if payload.get("plan") or route in {
            "plan",
            "plan_requested",
            "materialize_execution_plan",
        }:
            route = "materialize_execution_plan"
        else:
            route = "assemble_final_response"
        return {
            "response": payload.get("response", payload),
            "execution": payload.get("execution", {}),
            "next_route": route,
            "plan": payload.get("plan", {}),
            "deferred_plan_versions": json_value(
                payload.get("deferred_plan_versions", {})
            ),
        }

    async def materialize_execution_plan(self, state: RootGraphState) -> dict[str, Any]:
        """规范化并提交首版 Plan；命令仓储在同一事务中登记运行视图事件。"""
        deps = self.dependencies
        request = state.get("request", {})
        task = state.get("understanding", {}).get("task_spec", {})
        raw = state.get("plan") or None
        if not raw:
            return plan_generation_failure()
        plan, tasks = coerce_plan(raw, request=request, task=task)
        run_id = str(request.get("run_id") or "").strip()
        worker_tools = set(await list_worker_tools(deps.tool_service))
        plan, tasks = scope_execution_plan(
            plan,
            tasks,
            task_spec=task,
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
        await deps.orchestration_command_handler.submit_plan(
            command,
            plan,
            tasks,
            legal_tools=worker_tools,
        )
        return {"plan": plan_dict(plan, tasks), "status": "running"}

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
        decision, context = await _load_or_create_review_decision(
            deps, identity, request, state
        )
        decision, receipt = await _apply_review_decision_with_fallback(
            deps, identity, decision, context, request
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
