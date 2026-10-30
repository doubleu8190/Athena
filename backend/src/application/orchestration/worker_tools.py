"""Worker 独立执行期间可调用的受治理工具。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Literal, cast

from application.common.responses import ToolResponse
from application.tools.service import ToolService
from application.orchestration.lease_context import lease_grant_from_context

from application.orchestration.root_decision import RootWorkerTool
from domain.tools import (
    ToolExecutionRequest,
    ToolExecutionResult,
    ToolOperationClaimStatus,
    WorkerToolOperationLedger,
)
from domain.events import ApplicationEvent, EventDurability, EventPort, EventType

from .worker_results import WorkerResultStore


@dataclass(frozen=True, slots=True)
class _PreparedWorkerToolCall:
    """已通过权限校验、可进入审批或执行阶段的 Worker 工具调用。"""

    request: ToolExecutionRequest
    args_hash: str
    tool_call_id: str
    result_identity: str


async def worker_tool_manifest(
    tool_service: ToolService | None,
) -> tuple[RootWorkerTool, ...]:
    """返回启用的原生工具及其确定性审批策略。

    参数：
    - `tool_service`（ToolService | None）：查询工具能力并执行工具调用的应用服务；约束：允许为 None，按未提供值的分支处理。

    返回：
    - tuple[RootWorkerTool, ...]：按名称排序且已解析审批策略的 Worker 工具定义。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    if tool_service is None:
        return ()
    configured = await tool_service.list_tools()
    return tuple(
        RootWorkerTool(
            name=name,
            description=tool.description,
            parameters=dict(tool.parameters),
            risk_level=cast(Literal["low", "medium", "high"], _risk_level(tool)),
            requires_approval=_requires_approval(tool),
        )
        for name, tool in sorted(
            ((tool.name, tool) for tool in configured if _is_worker_tool(tool)),
            key=lambda item: item[0],
        )
    )


class WorkerToolExecutor:
    """通过确定性权限和风险规则执行 Task 已授权的原生工具。
    """

    def __init__(
        self,
        tool_service: ToolService,
        *,
        result_store: WorkerResultStore | None = None,
        operation_ledger: WorkerToolOperationLedger | None = None,
        events: EventPort | None = None,
    ) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `tool_service`（ToolService）：查询工具能力并执行工具调用的应用服务；约束：无额外运行时限制。
        - `result_store`（WorkerResultStore | None）：存取大型或非内联工具结果的依赖；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `operation_ledger`（WorkerToolOperationLedger | None）：记录副作用操作幂等状态的依赖；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `events`（EventPort | None）：用于读取、写入或推送的事件存储依赖；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._tool_service = tool_service
        self._result_store = result_store
        self._operation_ledger = operation_ledger
        self._events = events

    async def __call__(
        self,
        *,
        tool_call: Any,
        execution: Mapping[str, Any],
        tool_round: int,
        call_index: int,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """校验 Worker 工具权限，处理审批与幂等执行，并整理工具结果。

        参数：
        - `tool_call`（Any）：Worker 生成的工具名称、参数及审批信息；约束：无额外运行时限制。
        - `execution`（Mapping[str, Any]）：当前 Run、Task、执行代次和租约身份；约束：无额外运行时限制。
        - `tool_round`（int）：当前 Worker 工具调用轮次；约束：无额外运行时限制。
        - `call_index`（int）：当前工具轮次中的调用序号，用于构造稳定身份；约束：无额外运行时限制。
        - `_kwargs`（Any）：调用适配器额外注入的关键字参数；当前处理不读取这些值；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：包含工具状态和结果的字典；拒绝或等待审批时返回对应治理结果。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        if not isinstance(tool_call, Mapping):
            return _denied("invalid_tool_call")
        name = str(tool_call.get("name", "")).strip()
        stable_call_id = _tool_call_id(execution, tool_round, call_index)
        if name == "read_worker_result":
            if self._result_store is None:
                result = _denied(
                    "worker_result_storage_unavailable",
                    name="read_worker_result",
                )
                result.update({"id": stable_call_id, "name": name})
                return result
            await self._publish_tool_started(
                execution=execution,
                tool_call_id=stable_call_id,
                tool_name=name,
                operation_key=None,
                arguments=tool_call.get("arguments", {}),
            )
            result = await self._result_store.read(tool_call, execution)
            result.setdefault("id", stable_call_id)
            result.setdefault("name", name)
            await self._publish_tool_completed(
                execution=execution,
                tool_call_id=stable_call_id,
                tool_name=name,
                operation_key=None,
                status=str(result.get("status", "failed")),
                error=result.get("error"),
                output_length=len(str(result.get("output") or "")),
                output_preview=_safe_output_preview(result.get("output")),
            )
            return result
        prepared = await self._prepare_tool_call(
            tool_call,
            execution,
            tool_round=tool_round,
            call_index=call_index,
        )
        if isinstance(prepared, dict):
            prepared.setdefault("id", stable_call_id)
            prepared.setdefault("name", name)
            return prepared
        result = await self._execute_prepared_call(prepared)
        if isinstance(result, dict):
            result.setdefault("id", stable_call_id)
            result.setdefault("name", name)
            result.setdefault("operation_key", prepared.request.operation_key)
            return result
        return await self._format_result(prepared, result)

    async def _prepare_tool_call(
        self,
        tool_call: Mapping[str, Any],
        execution: Mapping[str, Any],
        *,
        tool_round: int,
        call_index: int,
    ) -> _PreparedWorkerToolCall | dict[str, Any]:
        """校验任务授权并生成稳定的执行身份或审批请求。

        参数：
        - `tool_call`（Mapping[str, Any]）：Worker 请求执行的工具和参数；约束：无额外运行时限制。
        - `execution`（Mapping[str, Any]）：当前执行上下文；约束：无额外运行时限制。
        - `tool_round`（int）：工具调用轮次；约束：无额外运行时限制。
        - `call_index`（int）：轮次内的调用序号；约束：无额外运行时限制。

        返回：
        - _PreparedWorkerToolCall | dict[str, Any]：已准备的工具调用，或可直接返回给 Worker 的拒绝/审批结果。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        name = str(tool_call.get("name", "")).strip()
        task = execution.get("task", {})
        task = task if isinstance(task, Mapping) else {}
        allowed_tools = {str(value) for value in task.get("allowed_tools", ())}
        if not name or name not in allowed_tools:
            return _denied("tool_not_allowed_for_task", name=name)
        config = await self._tool_service.get(name)
        if config is None or not _is_worker_tool(config):
            return _denied("tool_not_available_to_worker", name=name)
        arguments = tool_call.get("arguments", tool_call.get("args", {}))
        if not isinstance(arguments, Mapping):
            return _denied("invalid_tool_arguments", name=name)
        run_id = str(execution.get("run_id", ""))
        session_id = str(execution.get("session_id", ""))
        task_id = str(task.get("task_id", ""))
        generation = int(execution.get("execution_generation", 0))
        if not run_id or not session_id or not task_id or generation < 1:
            return _denied("worker_execution_identity_missing", name=name)
        tool_call_id = f"worker:{task_id}:{generation}:{tool_round}:{call_index}"
        # 同一调用在审批恢复和执行重试时必须得到相同参数哈希与幂等键。
        arguments_json = _strict_json(dict(arguments))
        args_hash = hashlib.sha256(arguments_json.encode("utf-8")).hexdigest()
        operation_key = _operation_key(
            run_id, task_id, generation, tool_round, call_index
        )
        logical_operation_key = _logical_operation_key(
            run_id, task_id, tool_round, call_index
        )
        approval_decision = str(tool_call.get("approval_decision", "")).lower()
        if approval_decision == "approved":
            if (
                tool_call.get("args_hash") != args_hash
                or tool_call.get("operation_key") != operation_key
                or tool_call.get("tool_call_id")
                != f"worker:{task_id}:{generation}:{tool_round}:{call_index}"
            ):
                return _denied("worker_approval_operation_mismatch", name=name)
        if _requires_approval(config) and approval_decision != "approved":
            execution_id = str(execution.get("execution_id", ""))
            worker_thread_id = str(
                execution.get("worker_thread_id") or f"task:{task_id}:exec:{generation}"
            )
            if not execution_id:
                return _denied("worker_execution_identity_missing", name=name)
            return self._approval_response(
                execution=execution,
                session_id=session_id,
                task_id=task_id,
                execution_id=execution_id,
                worker_thread_id=worker_thread_id,
                tool_call_id=tool_call_id,
                name=name,
                arguments=dict(arguments),
                risk_level=_risk_level(config),
                args_hash=args_hash,
                operation_key=operation_key,
                tool_round=tool_round,
                call_index=call_index,
            )
        request = self._execution_request(
            execution=execution,
            tool_name=name,
            arguments=dict(arguments),
            session_id=session_id,
            run_id=run_id,
            approval_decision=approval_decision or None,
            task_id=task_id,
            tool_call_id=tool_call_id,
            operation_key=operation_key,
            logical_operation_key=logical_operation_key,
        )
        return _PreparedWorkerToolCall(
            request=request,
            args_hash=args_hash,
            tool_call_id=tool_call_id,
            result_identity=(
                f"{run_id}:{task_id}:{generation}:{tool_round}:{call_index}"
            ),
        )

    def _approval_response(
        self,
        *,
        execution: Mapping[str, Any],
        session_id: str,
        task_id: str,
        execution_id: str,
        worker_thread_id: str,
        tool_call_id: str,
        name: str,
        arguments: dict[str, Any],
        risk_level: str,
        args_hash: str,
        operation_key: str,
        tool_round: int,
        call_index: int,
    ) -> dict[str, Any]:
        """构造绑定当前执行代次和参数哈希的审批请求。

        参数：
        - `execution`（Mapping[str, Any]）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。
        - `task_id`（str）：目标 Worker 任务的稳定标识；约束：无额外运行时限制。
        - `execution_id`（str）：本次 Worker 执行的稳定标识；约束：无额外运行时限制。
        - `worker_thread_id`（str）：Worker 图使用的检查点线程标识；约束：无额外运行时限制。
        - `tool_call_id`（str）：与模型工具调用消息对应的稳定标识；约束：无额外运行时限制。
        - `name`（str）：目标名称，用于标识相应配置、工具或对象；约束：无额外运行时限制。
        - `arguments`（dict[str, Any]）：调用工具或构造操作所需的参数映射；约束：无额外运行时限制。
        - `risk_level`（str）：工具操作的风险等级；约束：无额外运行时限制。
        - `args_hash`（str）：规范化工具参数计算出的摘要，用于审批绑定；约束：无额外运行时限制。
        - `operation_key`（str）：标识同一工具副作用操作的幂等键；约束：无额外运行时限制。
        - `tool_round`（int）：Worker 当前工具调用轮次，用于关联本轮调用；约束：无额外运行时限制。
        - `call_index`（int）：当前工具轮次中的调用序号，用于构造稳定身份；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：包含待审批操作身份、参数摘要和当前决定的响应映射。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        approval_id = (
            "approval:"
            + hashlib.sha256(
                f"{worker_thread_id}:{operation_key}".encode("utf-8")
            ).hexdigest()[:32]
        )
        return {
            "status": "approval_required",
            "approval_request": {
                "approval_id": approval_id,
                "session_id": session_id,
                "run_id": str(execution["run_id"]),
                "plan_id": str(execution.get("plan_id", "")),
                "task_id": task_id,
                "execution_id": execution_id,
                "worker_thread_id": worker_thread_id,
                "tool_call_id": tool_call_id,
                "tool_name": name,
                "arguments": arguments,
                "risk_level": risk_level,
                "args_hash": args_hash,
                "operation_key": operation_key,
                "tool_round": tool_round,
                "call_index": call_index,
            },
        }

    def _execution_request(
        self,
        *,
        execution: Mapping[str, Any],
        tool_name: str,
        arguments: dict[str, Any],
        session_id: str,
        run_id: str,
        approval_decision: str | None,
        task_id: str,
        tool_call_id: str,
        operation_key: str,
        logical_operation_key: str,
    ) -> ToolExecutionRequest:
        """把已校验身份转换为底层工具服务使用的请求。

        参数：
        - `execution`（Mapping[str, Any]）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。
        - `tool_name`（str）：目标工具的名称；约束：无额外运行时限制。
        - `arguments`（dict[str, Any]）：调用工具或构造操作所需的参数映射；约束：无额外运行时限制。
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。
        - `approval_decision`（str | None）：已持久化的审批决定；约束：允许为 None，按未提供值的分支处理。
        - `task_id`（str）：目标 Worker 任务的稳定标识；约束：无额外运行时限制。
        - `tool_call_id`（str）：与模型工具调用消息对应的稳定标识；约束：无额外运行时限制。
        - `operation_key`（str）：标识同一工具副作用操作的幂等键；约束：无额外运行时限制。
        - `logical_operation_key`（str）：跨执行重试保持稳定的逻辑操作键；约束：无额外运行时限制。

        返回：
        - ToolExecutionRequest：绑定执行身份、审批结果、幂等键和租约的工具执行请求。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return ToolExecutionRequest(
            tool_name=tool_name,
            arguments=arguments,
            session_id=session_id,
            run_id=run_id,
            approval_decision=approval_decision,
            plan_id=str(execution.get("plan_id", "")) or None,
            task_id=task_id,
            tool_call_id=tool_call_id,
            agent_role="worker",
            operation_key=operation_key,
            logical_operation_key=logical_operation_key,
            execution_id=str(execution.get("execution_id", "")) or None,
            lease=lease_grant_from_context(execution),
            cancellation_epoch=(
                int(execution["cancellation_epoch"])
                if execution.get("cancellation_epoch") is not None
                else None
            ),
        )

    async def _execute_prepared_call(
        self, prepared: _PreparedWorkerToolCall
    ) -> ToolExecutionResult | dict[str, Any]:
        """按幂等账本状态恢复结果或执行一次工具副作用。

        参数：
        - `prepared`（_PreparedWorkerToolCall）：已完成权限、参数和风险校验的工具调用；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - ToolExecutionResult | dict[str, Any]：封装本次操作输出或状态的 `ToolExecutionResult | dict[str, Any]` 结果对象。

        异常：
        - RuntimeError：校验条件 `self._operation_ledger is not None；ledger_claim.status == ToolOperationClaimStatus.COMPLETED；ledger_claim.result is None` 成立时（异常说明：worker_tool_operation_result_missing）。
        - RuntimeError：校验条件 `self._operation_ledger is not None；not (ledger_claim.status == ToolOperationClaimStatus.COMPLETED)；ledger_claim.status == ToolOperationClaimStatus.ACQUIRED；not await self._operation_ledger.complete(prepared.request, args_hash=prepared.args_hash, result=result)` 成立时（异常说明：worker_tool_operation_result_not_persisted）。
        - 未列出的依赖异常按当前实现传播。
        """
        if self._operation_ledger is not None:
            # 先认领幂等键，再按账本状态复用结果或执行副作用，防止重试重复操作。
            ledger_claim = await self._operation_ledger.claim(
                prepared.request, args_hash=prepared.args_hash
            )
            if ledger_claim.status == ToolOperationClaimStatus.COMPLETED:
                if ledger_claim.result is None:
                    raise RuntimeError("worker_tool_operation_result_missing")
                result = ledger_claim.result
                await self._publish_tool_completed(
                    execution=_execution_from_request(prepared.request),
                    tool_call_id=prepared.tool_call_id,
                    tool_name=prepared.request.tool_name,
                    operation_key=prepared.request.operation_key,
                    status=result.status,
                    error=result.error,
                    duration_ms=result.duration_ms,
                    output_length=len(result.output or ""),
                    output_preview=_safe_output_preview(result.output),
                )
            elif ledger_claim.status == ToolOperationClaimStatus.ACQUIRED:
                # 执行结果必须先持久化成功，之后才发布完成事件并允许后续重试复用。
                await self._publish_tool_started(
                    execution=_execution_from_request(prepared.request),
                    tool_call_id=prepared.tool_call_id,
                    tool_name=prepared.request.tool_name,
                    operation_key=prepared.request.operation_key,
                    arguments=prepared.request.arguments,
                )
                result = await self._tool_service.execute(prepared.request)
                if not await self._operation_ledger.complete(
                    prepared.request, args_hash=prepared.args_hash, result=result
                ):
                    raise RuntimeError("worker_tool_operation_result_not_persisted")
                await self._publish_tool_completed(
                    execution=_execution_from_request(prepared.request),
                    tool_call_id=prepared.tool_call_id,
                    tool_name=prepared.request.tool_name,
                    operation_key=prepared.request.operation_key,
                    status=result.status,
                    error=result.error,
                    duration_ms=result.duration_ms,
                    output_length=len(result.output or ""),
                    output_preview=_safe_output_preview(result.output),
                )
            elif ledger_claim.status == ToolOperationClaimStatus.IN_PROGRESS:
                await self._publish_tool_completed(
                    execution=_execution_from_request(prepared.request),
                    tool_call_id=prepared.tool_call_id,
                    tool_name=prepared.request.tool_name,
                    operation_key=prepared.request.operation_key,
                    status="failed",
                    error={"code": "worker_tool_operation_outcome_unknown"},
                )
                return {
                    "id": prepared.tool_call_id,
                    "name": prepared.request.tool_name,
                    "status": "failed",
                    "error": {"code": "worker_tool_operation_outcome_unknown"},
                }
            elif ledger_claim.status == ToolOperationClaimStatus.CONFLICT:
                return _denied(
                    "worker_tool_operation_key_conflict",
                    name=prepared.request.tool_name,
                )
            else:
                return _denied(
                    ledger_claim.reason or "worker_tool_execution_not_active",
                    name=prepared.request.tool_name,
                )
        else:
            await self._publish_tool_started(
                execution=_execution_from_request(prepared.request),
                tool_call_id=prepared.tool_call_id,
                tool_name=prepared.request.tool_name,
                operation_key=prepared.request.operation_key,
                arguments=prepared.request.arguments,
            )
            result = await self._tool_service.execute(prepared.request)
            await self._publish_tool_completed(
                execution=_execution_from_request(prepared.request),
                tool_call_id=prepared.tool_call_id,
                tool_name=prepared.request.tool_name,
                operation_key=prepared.request.operation_key,
                status=result.status,
                error=result.error,
                duration_ms=result.duration_ms,
                output_length=len(result.output or ""),
                output_preview=_safe_output_preview(result.output),
            )
        return result

    async def _publish_tool_started(
        self,
        *,
        execution: Mapping[str, Any],
        tool_call_id: str,
        tool_name: str,
        operation_key: str | None,
        arguments: Any = None,
    ) -> None:
        """在副作用操作开始前发布持久化的启动事件。

        参数：
        - `execution`（Mapping[str, Any]）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。
        - `tool_call_id`（str）：与模型工具调用消息对应的稳定标识；约束：无额外运行时限制。
        - `tool_name`（str）：目标工具的名称；约束：无额外运行时限制。
        - `operation_key`（str | None）：标识同一工具副作用操作的幂等键；约束：允许为 None，按未提供值的分支处理。
        - `arguments`（Any）：调用工具或构造操作所需的参数映射；约束：可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        await self._publish_tool_lifecycle(
            EventType.TOOL_STARTED,
            execution=execution,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            operation_key=operation_key,
            status="running",
            error=None,
            arguments=arguments,
        )

    async def _publish_tool_completed(
        self,
        *,
        execution: Mapping[str, Any],
        tool_call_id: str,
        tool_name: str,
        operation_key: str | None,
        status: str,
        error: Any,
        duration_ms: float | None = None,
        output_length: int | None = None,
        output_preview: str | None = None,
    ) -> None:
        """发布可安全重放且具幂等身份的工具完成事件。

        参数：
        - `execution`（Mapping[str, Any]）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。
        - `tool_call_id`（str）：与模型工具调用消息对应的稳定标识；约束：无额外运行时限制。
        - `tool_name`（str）：目标工具的名称；约束：无额外运行时限制。
        - `operation_key`（str | None）：标识同一工具副作用操作的幂等键；约束：允许为 None，按未提供值的分支处理。
        - `status`（str）：当前操作或记录的状态值；约束：无额外运行时限制。
        - `error`（Any）：操作失败时捕获的异常或错误说明；约束：无额外运行时限制。
        - `duration_ms`（float | None）：本阶段测得的耗时毫秒数；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `output_length`（int | None）：工具输出的完整字符数；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `output_preview`（str | None）：供事件或审计记录使用的工具输出摘要；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        error_detail = (
            error.get("code") or error.get("message")
            if isinstance(error, Mapping)
            else error if isinstance(error, str) else None
        )
        safe_error = _safe_output_preview(error_detail)
        await self._publish_tool_lifecycle(
            EventType.TOOL_COMPLETED,
            execution=execution,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            operation_key=operation_key,
            status=status,
            error=safe_error[:240] if safe_error else None,
            duration_ms=duration_ms,
            output_length=output_length,
            output_preview=output_preview,
        )

    async def _publish_tool_lifecycle(
        self,
        event_type: EventType,
        *,
        execution: Mapping[str, Any],
        tool_call_id: str,
        tool_name: str,
        operation_key: str | None,
        status: str,
        error: str | None,
        arguments: Any = None,
        duration_ms: float | None = None,
        output_length: int | None = None,
        output_preview: str | None = None,
    ) -> None:
        """用独立稳定转换键保存各工具生命周期边界。

        参数：
        - `event_type`（EventType）：要发布的事件类型名称；约束：无额外运行时限制。
        - `execution`（Mapping[str, Any]）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。
        - `tool_call_id`（str）：与模型工具调用消息对应的稳定标识；约束：无额外运行时限制。
        - `tool_name`（str）：目标工具的名称；约束：无额外运行时限制。
        - `operation_key`（str | None）：标识同一工具副作用操作的幂等键；约束：允许为 None，按未提供值的分支处理。
        - `status`（str）：当前操作或记录的状态值；约束：无额外运行时限制。
        - `error`（str | None）：操作失败时捕获的异常或错误说明；约束：允许为 None，按未提供值的分支处理。
        - `arguments`（Any）：调用工具或构造操作所需的参数映射；约束：可省略，默认值为 `None`。
        - `duration_ms`（float | None）：本阶段测得的耗时毫秒数；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `output_length`（int | None）：工具输出的完整字符数；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `output_preview`（str | None）：供事件或审计记录使用的工具输出摘要；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        if self._events is None:
            return
        task = execution.get("task", {})
        task = task if isinstance(task, Mapping) else {}
        task_id = str(task.get("task_id", ""))
        generation = int(execution.get("execution_generation", 0))
        phase = "started" if event_type == EventType.TOOL_STARTED else "completed"
        payload: dict[str, Any] = {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "task_id": task_id,
            "execution_id": str(execution.get("execution_id", "")),
            "execution_generation": generation,
            "status": status,
        }
        if operation_key:
            payload["operation_key"] = operation_key
        if event_type == EventType.TOOL_STARTED:
            payload["arguments_summary"] = _safe_argument_summary(arguments)
        if duration_ms is not None:
            payload["duration_ms"] = int(duration_ms)
        if output_length is not None:
            payload["output_length"] = output_length
        if output_preview:
            payload["output_preview"] = output_preview
        if error:
            payload["error"] = {"code": error}
        # started/completed 共用工具调用身份但使用不同阶段键，允许事件持久化幂等重放。
        await self._events.publish(
            ApplicationEvent(
                event_type=event_type,
                durability=EventDurability.DURABLE,
                session_id=str(execution.get("session_id", "")),
                run_id=str(execution.get("run_id", "")) or None,
                transition_id=f"worker-tool:{tool_call_id}:{phase}",
                occurred_at=datetime.now(timezone.utc),
                payload=payload,
            )
        )

    async def _format_result(
        self,
        prepared: _PreparedWorkerToolCall,
        result: ToolExecutionResult,
    ) -> dict[str, Any]:
        """格式化工具结果，并将超长文本保存为执行内可读引用。

        参数：
        - `prepared`（_PreparedWorkerToolCall）：已完成权限、参数和风险校验的工具调用；约束：无额外运行时限制。
        - `result`（ToolExecutionResult）：当前阶段生成的领域结果或操作回执；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：将工具执行结果整理为 Worker 消息可消费的字段映射。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        payload = {
            "id": prepared.tool_call_id,
            "name": prepared.request.tool_name,
            "status": result.status,
            "output": result.output,
            "error": result.error,
            "duration_ms": result.duration_ms,
            "operation_key": prepared.request.operation_key,
        }
        if self._result_store is not None and isinstance(result.output, str):
            stored = await self._result_store.store(
                result.output,
                identity=prepared.result_identity,
            )
            payload["output"] = stored.output
            if stored.result_ref is not None and stored.storage_key is not None:
                payload["result_ref"] = stored.result_ref
                payload["truncated"] = True
                payload["_storage_key"] = stored.storage_key
        return payload


def _is_worker_tool(config: ToolResponse) -> bool:
    """判断工具是否属于 Worker 可调用的原生工具。

    参数：
    - `config`（ToolResponse）：当前操作使用的已解析配置；约束：无额外运行时限制。

    返回：
    - bool：操作是否成功或条件是否成立。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    risk_level = _risk_level(config)
    return (
        config.enabled
        and risk_level in {"low", "medium", "high"}
        and config.execution_mode.lower() == "native"
    )


def _risk_level(config: ToolResponse) -> str:
    """将工具返回的风险值规范为受支持的等级。

    参数：
    - `config`（ToolResponse）：当前操作使用的已解析配置；约束：无额外运行时限制。

    返回：
    - str：规范化为受支持等级的工具风险名称。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return config.risk_level.lower()


def _requires_approval(config: ToolResponse) -> bool:
    """根据治理配置和风险等级判断是否需要人工审批。

    参数：
    - `config`（ToolResponse）：当前操作使用的已解析配置；约束：无额外运行时限制。

    返回：
    - bool：操作是否成功或条件是否成立。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return config.require_approval or _risk_level(config) in {"medium", "high"}


def _operation_key(
    run_id: str,
    task_id: str,
    generation: int,
    tool_round: int,
    call_index: int,
) -> str:
    """构造满足数据库长度限制的稳定副作用幂等键。

    参数：
    - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。
    - `task_id`（str）：目标 Worker 任务的稳定标识；约束：无额外运行时限制。
    - `generation`（int）：Worker 执行代次，用于隔离重试和副作用身份；约束：无额外运行时限制。
    - `tool_round`（int）：Worker 当前工具调用轮次，用于关联本轮调用；约束：无额外运行时限制。
    - `call_index`（int）：当前工具轮次中的调用序号，用于构造稳定身份；约束：无额外运行时限制。

    返回：
    - str：满足持久层长度限制的操作幂等键。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    value = f"athena:{run_id}:{task_id}:{generation}:{tool_round}:{call_index}"
    if len(value) <= 128:
        return value
    return "athena:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _logical_operation_key(
    run_id: str,
    task_id: str,
    tool_round: int,
    call_index: int,
) -> str:
    """为跨执行重试的同一工具调用位置生成稳定键。

    参数：
    - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。
    - `task_id`（str）：目标 Worker 任务的稳定标识；约束：无额外运行时限制。
    - `tool_round`（int）：Worker 当前工具调用轮次，用于关联本轮调用；约束：无额外运行时限制。
    - `call_index`（int）：当前工具轮次中的调用序号，用于构造稳定身份；约束：无额外运行时限制。

    返回：
    - str：跨执行重试保持稳定的逻辑操作幂等键。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return f"athena:{run_id}:{task_id}:logical:{tool_round}:{call_index}"


def _strict_json(value: Any) -> str:
    """以稳定字段顺序序列化 JSON，供审批摘要和幂等校验使用。

    参数：
    - `value`（Any）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - str：字段顺序稳定且不含非标准值的 JSON 文本。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _denied(code: str, *, name: str | None = None) -> dict[str, Any]:
    """构造包含错误代码和工具名称的拒绝回执。

    参数：
    - `code`（str）：用于区分错误或结果类型的代码；约束：无额外运行时限制。
    - `name`（str | None）：目标名称，用于标识相应配置、工具或对象；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

    返回：
    - dict[str, Any]：包含拒绝状态、错误代码和工具标识的调用回执。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    result: dict[str, Any] = {"status": "denied", "error": {"code": code}}
    if name:
        result["name"] = name
    return result


def _safe_argument_summary(arguments: Any) -> dict[str, str]:
    """仅提取允许展示的工具参数，避免泄露敏感字段。

    参数：
    - `arguments`（Any）：调用工具或构造操作所需的参数映射；约束：无额外运行时限制。

    返回：
    - dict[str, str]：仅包含白名单参数且已遮蔽敏感值的摘要映射。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    if not isinstance(arguments, Mapping):
        return {}
    allowed = {
        "query",
        "filename",
        "file_name",
        "path",
        "file_path",
        "title",
        "url",
        "command",
    }
    summary: dict[str, str] = {}
    for key, value in arguments.items():
        normalized_key = str(key).lower()
        if (
            normalized_key not in allowed
            or not isinstance(value, str)
            or not value.strip()
        ):
            continue
        safe_value = _safe_output_preview(value)
        if safe_value:
            summary[normalized_key] = safe_value[:240]
    return summary


def _safe_output_preview(output: Any) -> str | None:
    """生成经过凭据遮蔽且长度受限的工具输出预览。

    参数：
    - `output`（Any）：待处理或生成的输出内容；约束：无额外运行时限制。

    返回：
    - str | None：经过凭据遮蔽和长度限制的输出预览；无有效文本时返回 None。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    if output is None:
        return None
    preview = str(output).strip()[:500]
    if not preview:
        return None
    preview = re.sub(
        r"""(?i)(["']?(?:api[_-]?key|(?:access|refresh|id|api)?[_-]?token|authorization|password|secret|credential)["']?\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}&]+)""",
        r'\1"[已隐藏]"',
        preview,
    )
    preview = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+", "Bearer [已隐藏]", preview)
    return preview


def _tool_call_id(
    execution: Mapping[str, Any], tool_round: int, call_index: int
) -> str:
    """读取并规范化 Worker 助手消息中的工具调用标识。

    参数：
    - `execution`（Mapping[str, Any]）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。
    - `tool_round`（int）：Worker 当前工具调用轮次，用于关联本轮调用；约束：无额外运行时限制。
    - `call_index`（int）：当前工具轮次中的调用序号，用于构造稳定身份；约束：无额外运行时限制。

    返回：
    - str：Worker 助手消息中经规范化的稳定工具调用标识。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    task = execution.get("task", {})
    task = task if isinstance(task, Mapping) else {}
    return (
        f"worker:{str(task.get('task_id', ''))}:"
        f"{int(execution.get('execution_generation', 0))}:"
        f"{tool_round}:{call_index}"
    )


def _execution_from_request(request: ToolExecutionRequest) -> dict[str, Any]:
    """从工具请求恢复最小化的公开执行身份。

    参数：
    - `request`（ToolExecutionRequest）：当前请求及其关联身份，用于限定操作范围；约束：无额外运行时限制。

    返回：
    - dict[str, Any]：供事件记录使用的最小公开执行身份字段。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return {
        "session_id": request.session_id,
        "run_id": request.run_id,
        "plan_id": request.plan_id,
        "execution_id": request.execution_id,
        "execution_generation": _generation_from_tool_call_id(request.tool_call_id),
        "task": {"task_id": request.task_id},
    }


def _generation_from_tool_call_id(tool_call_id: str) -> int:
    """从稳定工具调用标识中解析 Worker 执行代次。

    参数：
    - `tool_call_id`（str）：与模型工具调用消息对应的稳定标识；约束：无额外运行时限制。

    返回：
    - int：从稳定工具调用标识解析出的执行代次；格式无效时返回 None。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    parts = tool_call_id.split(":")
    try:
        return int(parts[2])
    except (IndexError, ValueError):
        return 0


__all__ = [
    "WorkerToolExecutor",
    "worker_tool_manifest",
]
