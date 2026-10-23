"""Governed tools available to independent Worker executions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from application.common.responses import ToolResponse
from application.tools.service import ToolService

from domain.tools import (
    ToolExecutionRequest,
    ToolExecutionResult,
    ToolOperationClaimStatus,
    WorkerToolOperationLedger,
)

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
) -> tuple[dict[str, Any], ...]:
    """Return enabled native tools with their deterministic approval policy."""
    if tool_service is None:
        return ()
    configured = await tool_service.list_tools()
    return tuple(
        {
            "name": name,
            "description": tool.description,
            "parameters": dict(tool.parameters),
            "risk_level": _risk_level(tool),
            "requires_approval": _requires_approval(tool),
        }
        for name, tool in sorted(
            (
                (tool.name, tool)
                for tool in configured
                if _is_worker_tool(tool)
            ),
            key=lambda item: item[0],
        )
    )


class WorkerToolExecutor:
    """Execute task-authorized native tools through deterministic governance."""

    def __init__(
        self,
        tool_service: ToolService,
        *,
        result_store: WorkerResultStore | None = None,
        operation_ledger: WorkerToolOperationLedger | None = None,
    ) -> None:
        self._tool_service = tool_service
        self._result_store = result_store
        self._operation_ledger = operation_ledger

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
        Args:
            tool_call: Worker 生成的工具名称、参数及审批信息。
            execution: 当前 Run、Task、执行代次和租约身份。
            tool_round: 当前 Worker 工具调用轮次。
            call_index: 当前轮次中的工具调用序号。
            **_kwargs: LangGraph 注入的其他执行参数，不参与工具决策。
        Returns:
            包含工具状态和结果的字典；拒绝或等待审批时返回对应治理结果。
        Raises:
            RuntimeError: 幂等账本缺少已完成结果或未能持久化执行结果时。
        """
        if not isinstance(tool_call, Mapping):
            return _denied("invalid_tool_call")
        if str(tool_call.get("name", "")).strip() == "read_worker_result":
            if self._result_store is None:
                return _denied(
                    "worker_result_storage_unavailable",
                    name="read_worker_result",
                )
            return await self._result_store.read(tool_call, execution)
        prepared = await self._prepare_tool_call(
            tool_call,
            execution,
            tool_round=tool_round,
            call_index=call_index,
        )
        if isinstance(prepared, dict):
            return prepared
        result = await self._execute_prepared_call(prepared)
        if isinstance(result, dict):
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
        Args:
            tool_call: Worker 请求执行的工具和参数。
            execution: 当前执行上下文。
            tool_round: 工具调用轮次。
            call_index: 轮次内的调用序号。
        Returns:
            已准备的工具调用，或可直接返回给 Worker 的拒绝/审批结果。
        """
        name = str(tool_call.get("name", "")).strip()
        task = execution.get("task", {})
        task = task if isinstance(task, Mapping) else {}
        allowed_tools = {
            str(value) for value in task.get("allowed_tools", ())
        }
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
        tool_call_id = (
            f"worker:{task_id}:{generation}:{tool_round}:{call_index}"
        )
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
                execution.get("worker_thread_id")
                or f"task:{task_id}:exec:{generation}"
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
        """构造绑定当前执行代次和参数哈希的审批请求。"""
        approval_id = "approval:" + hashlib.sha256(
            f"{worker_thread_id}:{operation_key}".encode("utf-8")
        ).hexdigest()[:32]
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
        """把已校验身份转换为底层工具服务使用的请求。"""
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
            fencing_token=(
                int(execution["fencing_token"])
                if execution.get("fencing_token") is not None
                else None
            ),
            cancellation_epoch=(
                int(execution["cancellation_epoch"])
                if execution.get("cancellation_epoch") is not None
                else None
            ),
        )

    async def _execute_prepared_call(
        self, prepared: _PreparedWorkerToolCall
    ) -> ToolExecutionResult | dict[str, Any]:
        """按幂等账本状态恢复结果或执行一次工具副作用。"""
        if self._operation_ledger is not None:
            ledger_claim = await self._operation_ledger.claim(
                prepared.request, args_hash=prepared.args_hash
            )
            if ledger_claim.status == ToolOperationClaimStatus.COMPLETED:
                if ledger_claim.result is None:
                    raise RuntimeError("worker_tool_operation_result_missing")
                result = ledger_claim.result
            elif ledger_claim.status == ToolOperationClaimStatus.ACQUIRED:
                result = await self._tool_service.execute(prepared.request)
                if not await self._operation_ledger.complete(
                    prepared.request, args_hash=prepared.args_hash, result=result
                ):
                    raise RuntimeError("worker_tool_operation_result_not_persisted")
            elif ledger_claim.status == ToolOperationClaimStatus.IN_PROGRESS:
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
            result = await self._tool_service.execute(prepared.request)
        return result

    async def _format_result(
        self,
        prepared: _PreparedWorkerToolCall,
        result: ToolExecutionResult,
    ) -> dict[str, Any]:
        """格式化工具结果，并将超长文本保存为执行内可读引用。"""
        payload = {
            "id": prepared.tool_call_id,
            "name": prepared.request.tool_name,
            "status": result.status,
            "output": result.output,
            "error": result.error,
            "duration_ms": result.duration_ms,
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
    """Workers may use enabled native tools with a known risk classification."""
    risk_level = _risk_level(config)
    return (
        config.enabled
        and risk_level in {"low", "medium", "high"}
        and config.execution_mode.lower() == "native"
    )


def _risk_level(config: ToolResponse) -> str:
    """Normalize the application tool response risk value."""
    return config.risk_level.lower()


def _requires_approval(config: ToolResponse) -> bool:
    """Require approval when configured or when risk is medium/high."""
    return config.require_approval or _risk_level(config) in {"medium", "high"}


def _operation_key(
    run_id: str,
    task_id: str,
    generation: int,
    tool_round: int,
    call_index: int,
) -> str:
    """Build a stable side-effect key within the database column limit."""
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
    """Identify the same planned tool-call position across execution attempts."""
    return f"athena:{run_id}:{task_id}:logical:{tool_round}:{call_index}"


def _strict_json(value: Any) -> str:
    """Serialize an operation deterministically for approval binding."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _denied(code: str, *, name: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "denied", "error": {"code": code}}
    if name:
        result["name"] = name
    return result
__all__ = [
    "WorkerToolExecutor",
    "worker_tool_manifest",
]
