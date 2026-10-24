"""Worker application workflow independent from LangGraph."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from domain.orchestration import (
    WorkerExecutionEntryType,
    WorkerExecutionMessageAppend,
    WorkerExecutionMessageContent,
    WorkerExecutionMessageRepository,
    WorkerToolResultContent,
)

from .serialization import as_dict
from shared.serialization import json_value


class WorkerWorkflow:
    """Run, pause, resume and finalize one persisted task."""

    def __init__(
        self,
        *,
        execute_tool: Callable[..., Awaitable[Mapping[str, Any]]] | None = None,
        execute: Callable[..., Awaitable[Mapping[str, Any]]] | None = None,
        execution_messages: WorkerExecutionMessageRepository | None = None,
        max_tool_rounds: int = 4,
        max_tool_calls_per_round: int = 8,
    ) -> None:
        if max_tool_rounds < 1 or max_tool_calls_per_round < 1:
            raise ValueError("worker tool budgets are invalid")
        self.execute_tool = execute_tool
        self.execute = execute
        self.execution_messages = execution_messages
        self.max_tool_rounds = max_tool_rounds
        self.max_tool_calls_per_round = max_tool_calls_per_round

    async def prepare(self, state: Mapping[str, Any]) -> dict[str, Any]:
        execution = dict(state.get("execution", {}))
        return {
            "execution": json_value(execution),
            "input": json_value(state.get("input", execution.get("task", {}))),
            "tool_round": int(state.get("tool_round", 0)),
        }

    async def run(self, state: Mapping[str, Any]) -> dict[str, Any]:
        if self.execute is None:
            return {
                "result": {
                    "status": "failed",
                    "error": {"code": "worker_not_configured"},
                }
            }
        payload = as_dict(
            await self.execute(execution=state.get("execution", {}), state=state)
        )
        return {
            "result": payload,
            "waiting": str(payload.get("status", "completed")) == "waiting_approval",
        }

    async def approval_gate(self, state: Mapping[str, Any]) -> dict[str, Any]:
        result = state.get("result", {})
        return {
            "status": (
                "waiting_approval"
                if state.get("waiting")
                else str(result.get("status", "completed"))
            )
        }

    async def execute_tool_batch(self, state: Mapping[str, Any]) -> dict[str, Any]:
        if self.execute_tool is None:
            return {
                "result": {
                    "status": "failed",
                    "error": {"code": "tool_executor_not_configured"},
                },
                "waiting": False,
            }
        result = state.get("result", {})
        raw_calls = result.get("tool_calls", []) if isinstance(result, Mapping) else []
        calls = raw_calls if isinstance(raw_calls, (list, tuple)) else []
        if len(calls) > self.max_tool_calls_per_round:
            return {
                "result": {
                    "status": "failed",
                    "error": {
                        "code": "worker_tool_call_limit_exceeded",
                        "max_tool_calls_per_round": self.max_tool_calls_per_round,
                    },
                },
                "waiting": False,
            }
        tool_round = int(state.get("tool_round", 0))
        if tool_round >= self.max_tool_rounds:
            return {
                "result": {
                    "status": "failed",
                    "error": {
                        "code": "worker_tool_round_limit_exceeded",
                        "max_tool_rounds": self.max_tool_rounds,
                    },
                },
                "waiting": False,
            }
        tool_results: list[dict[str, Any]] = []
        for index, item in enumerate(calls):
            result = as_dict(
                await self.execute_tool(
                    tool_call=item,
                    execution=state.get("execution", {}),
                    state=state,
                    tool_round=tool_round,
                    call_index=index,
                )
            )
            if result.get("status") == "approval_required":
                approval = result.get("approval_request", {})
                pending = (
                    await self._append_tool_results(
                        state,
                        tool_results,
                        tool_round=tool_round,
                    )
                    if tool_results
                    else {}
                )
                return {
                    **pending,
                    "pending_approval": dict(approval)
                    if isinstance(approval, Mapping)
                    else {},
                    "result": {"status": "waiting_approval"},
                    "waiting": True,
                }
            await self._persist_tool_results(
                execution=state.get("execution", {}),
                tool_results=[result],
                tool_round=tool_round,
            )
            tool_results.append(result)
        return await self._append_tool_results(
            state,
            tool_results,
            tool_round=tool_round,
        )

    async def complete_tool_approval(
        self,
        state: Mapping[str, Any],
        decision: Any,
    ) -> dict[str, Any]:
        """核对审批回执，再执行原样冻结的工具操作或回传拒绝结果。

        Args:
            state: LangGraph 中断前保存的 Worker 状态。
            decision: ResumeWorker Job 携带的审批身份和决定。

        Returns:
            更新后的执行状态；拒绝时将明确的拒绝结果交回 Worker。

        Raises:
            RuntimeError: 审批身份与中断中的工具操作不一致时抛出。
        """
        if not isinstance(decision, Mapping):
            raise RuntimeError("worker_approval_resume_payload_invalid")

        state, approval, execution = self._restore_approval_execution(
            state, decision
        )
        self._validate_approval_resume(approval, decision, execution)
        result = await self._resolve_approval(approval, decision, execution, state)
        if result.get("status") == "approval_required":
            raise RuntimeError("worker_tool_reapproval_required")
        await self._persist_tool_results(
            execution=execution,
            tool_results=[result],
            tool_round=int(approval.get("tool_round", 0)),
        )
        return await self._append_tool_results(
            state,
            [result],
            tool_round=int(approval.get("tool_round", 0)),
        )

    @staticmethod
    def _restore_approval_execution(
        state: Mapping[str, Any], decision: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
        """合并恢复执行上下文，同时保留中断时的工具结果和私有引用。"""
        approval = state.get("pending_approval", {})
        approval = approval if isinstance(approval, Mapping) else {}
        execution = state.get("execution", {})
        execution = execution if isinstance(execution, Mapping) else {}
        execution_update = decision.get("__worker_execution_update")
        if isinstance(execution_update, Mapping):
            updated_execution = dict(execution_update)
            for field in (
                "run_id",
                "plan_id",
                "session_id",
                "execution_id",
                "worker_thread_id",
                "execution_generation",
            ):
                if (
                    execution.get(field) is not None
                    and updated_execution.get(field) != execution.get(field)
                ):
                    raise RuntimeError("worker_approval_execution_identity_mismatch")
            previous_task = execution.get("task", {})
            updated_task = updated_execution.get("task", {})
            if (
                isinstance(previous_task, Mapping)
                and previous_task.get("task_id") is not None
                and (
                    not isinstance(updated_task, Mapping)
                    or updated_task.get("task_id") != previous_task.get("task_id")
                )
            ):
                raise RuntimeError("worker_approval_execution_identity_mismatch")
            for key in ("tool_results", "_worker_result_refs"):
                if key in execution:
                    updated_execution[key] = execution[key]
            execution = updated_execution
            state = {**state, "execution": updated_execution}

        return state, approval, execution

    @staticmethod
    def _validate_approval_resume(
        approval: Mapping[str, Any],
        decision: Mapping[str, Any],
        execution: Mapping[str, Any],
    ) -> None:
        """确保恢复回执仍绑定中断时批准的工具操作。"""
        expected = {
            "approval_id": approval.get("approval_id"),
            "execution_id": approval.get("execution_id"),
            "args_hash": approval.get("args_hash"),
            "operation_key": approval.get("operation_key"),
        }
        if any(decision.get(key) != value for key, value in expected.items()):
            raise RuntimeError("worker_approval_operation_mismatch")
        if not decision.get("interrupt_id"):
            raise RuntimeError("worker_approval_interrupt_identity_missing")
        if execution.get("execution_id") != approval.get("execution_id"):
            raise RuntimeError("worker_approval_execution_identity_mismatch")

    async def _resolve_approval(
        self,
        approval: Mapping[str, Any],
        decision: Mapping[str, Any],
        execution: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        """执行已批准操作，或构造拒绝和过期回执。"""
        resolution = str(decision.get("decision", "")).lower()
        tool_call = {
            "name": approval.get("tool_name"),
            "arguments": approval.get("arguments", {}),
            "operation_key": approval.get("operation_key"),
            "args_hash": approval.get("args_hash"),
            "tool_call_id": approval.get("tool_call_id"),
        }
        if resolution == "approved":
            tool_call["approval_decision"] = "approved"
            if self.execute_tool is None:
                raise RuntimeError("worker_tool_executor_not_configured")
            result = as_dict(
                await self.execute_tool(
                    tool_call=tool_call,
                    execution=execution,
                    state=state,
                    tool_round=int(approval.get("tool_round", 0)),
                    call_index=int(approval.get("call_index", 0)),
                )
            )
        elif resolution in {"denied", "expired"}:
            result = {
                "id": str(approval.get("tool_call_id", "")),
                "name": str(approval.get("tool_name", "")),
                "status": "denied",
                "error": {
                    "code": (
                        "approval_expired" if resolution == "expired"
                        else "approval_denied"
                    )
                },
            }
        else:
            raise RuntimeError("worker_approval_decision_invalid")

        result.setdefault("operation_key", approval.get("operation_key"))
        return result

    async def _append_tool_results(
        self,
        state: Mapping[str, Any],
        tool_results: list[dict[str, Any]],
        *,
        tool_round: int,
    ) -> dict[str, Any]:
        """追加工具回执，并把大结果的存储键留在 Worker 私有状态中。

        Args:
            state: 当前 Worker 图状态。
            tool_results: 本轮已完成工具的回执。
            tool_round: 这些工具所属的 LLM 工具轮次。

        Returns:
            包含安全投影、内部结果引用和下一轮计数的状态补丁。
        """
        execution = dict(state.get("execution", {}))
        previous_results = execution.get("tool_results", [])
        previous_refs = execution.get("_worker_result_refs", {})
        result_refs = (
            dict(previous_refs) if isinstance(previous_refs, Mapping) else {}
        )
        public_results = []
        for result in tool_results:
            storage_key = result.pop("_storage_key", None)
            result_ref = result.get("result_ref")
            if isinstance(storage_key, str) and isinstance(result_ref, str):
                result_refs[result_ref] = storage_key
            public_results.append(result)
        execution["tool_results"] = [
            *(previous_results if isinstance(previous_results, list) else []),
            *public_results,
        ]
        execution["_worker_result_refs"] = result_refs
        return {
            "execution": execution,
            "result": {},
            "waiting": False,
            "tool_round": tool_round + 1,
            "pending_approval": None,
        }

    async def _persist_tool_results(
        self,
        *,
        execution: Mapping[str, Any],
        tool_results: list[dict[str, Any]],
        tool_round: int,
    ) -> None:
        """Persist results only after the workflow has a determined tool outcome."""
        if self.execution_messages is None:
            return
        task = execution.get("task", {})
        task = task if isinstance(task, Mapping) else {}
        task_id = str(task.get("task_id", ""))
        generation = int(execution.get("execution_generation", 0))
        session_id = str(execution.get("session_id", ""))
        run_id = str(execution.get("run_id", ""))
        plan_id = str(execution.get("plan_id", ""))
        execution_id = str(execution.get("execution_id", ""))
        invocation_id = f"worker:{task_id}:{generation}:llm:{tool_round}"
        for result in tool_results:
            tool_call_id = str(result.get("id", ""))
            if not tool_call_id:
                raise ValueError("worker_tool_result_identity_missing")
            status = str(result.get("status", "failed"))
            error = result.get("error")
            error_value = dict(error) if isinstance(error, Mapping) else None
            projected = WorkerToolResultContent(
                status=status,
                output=result.get("output"),
                error=error_value,
                duration_ms=(
                    int(result["duration_ms"])
                    if isinstance(result.get("duration_ms"), (int, float))
                    else None
                ),
                result_ref=(
                    str(result["result_ref"])
                    if result.get("result_ref") is not None
                    else None
                ),
                truncated=(
                    bool(result["truncated"])
                    if result.get("truncated") is not None
                    else None
                ),
            )
            await self.execution_messages.append(
                WorkerExecutionMessageAppend(
                    message_id=(
                        f"worker:{task_id}:{generation}:tool-result:{tool_call_id}"
                    ),
                    session_id=session_id,
                    run_id=run_id,
                    plan_id=plan_id,
                    task_id=task_id,
                    execution_id=execution_id,
                    execution_generation=generation,
                    invocation_id=invocation_id,
                    entry_type=WorkerExecutionEntryType.TOOL_RESULT,
                    role=None,
                    content=WorkerExecutionMessageContent(tool_result=projected),
                    tool_call_id=tool_call_id,
                    tool_name=str(result.get("name", "")) or None,
                    operation_key=(
                        str(result["operation_key"])
                        if result.get("operation_key") is not None
                        else None
                    ),
                    result_ref=projected.result_ref,
                )
            )

    async def finalize(self, state: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(state.get("result", {}))
        status = (
            "waiting_approval"
            if state.get("waiting")
            else str(result.get("status", "completed"))
        )
        return {"result": result, "waiting": bool(state.get("waiting")), "status": status}

    @staticmethod
    def route_after_gate(state: Mapping[str, Any]) -> str:
        if state.get("waiting"):
            return "finish_worker"
        result = state.get("result", {})
        status = str(result.get("status", "completed"))
        if status not in {"running", "completed"}:
            return "finish_worker"
        return (
            "execute_tool_batch"
            if state.get("result", {}).get("tool_calls")
            else "finish_worker"
        )

    @staticmethod
    def route_after_tools(state: Mapping[str, Any]) -> str:
        if state.get("pending_approval"):
            return "request_worker_approval"
        return "finish_worker" if state.get("result", {}).get("error") else "worker_agent_loop"


__all__ = ["WorkerWorkflow"]
