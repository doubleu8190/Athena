"""Worker application workflow independent from LangGraph."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .root_workflow import as_dict, call, json_value


class WorkerWorkflow:
    """Run, pause, resume and finalize one persisted task."""

    def __init__(
        self,
        *,
        execute_tool: Callable[..., Any] | None = None,
        execute: Callable[..., Any] | None = None,
        max_tool_rounds: int = 4,
        max_tool_calls_per_round: int = 8,
    ) -> None:
        if max_tool_rounds < 1 or max_tool_calls_per_round < 1:
            raise ValueError("worker tool budgets are invalid")
        self.execute_tool = execute_tool
        self.execute = execute
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
            return {"result": {"status": "failed", "error": {"code": "worker_not_configured"}}}
        payload = as_dict(await call(self.execute, execution=state.get("execution", {}), state=state))
        return {"result": payload, "waiting": str(payload.get("status", "completed")) == "waiting_approval"}

    async def approval_gate(self, state: Mapping[str, Any]) -> dict[str, Any]:
        result = state.get("result", {})
        return {"status": "waiting_approval" if state.get("waiting") else str(result.get("status", "completed"))}

    async def execute_tool_batch(self, state: Mapping[str, Any]) -> dict[str, Any]:
        if self.execute_tool is None:
            return {"result": {"status": "failed", "error": {"code": "tool_executor_not_configured"}}, "waiting": False}
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
                await call(
                    self.execute_tool,
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
                    self._append_tool_results(
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
            tool_results.append(result)
        return self._append_tool_results(
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
        approval = state.get("pending_approval", {})
        approval = approval if isinstance(approval, Mapping) else {}
        if not isinstance(decision, Mapping):
            raise RuntimeError("worker_approval_resume_payload_invalid")
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
                await call(
                    self.execute_tool,
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

        if result.get("status") == "approval_required":
            raise RuntimeError("worker_tool_reapproval_required")
        return self._append_tool_results(
            state,
            [result],
            tool_round=int(approval.get("tool_round", 0)),
        )

    @staticmethod
    def _append_tool_results(
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

        Raises:
            无：该方法只整理已产生的 JSON 风格结果。
        """
        execution = dict(state.get("execution", {}))
        previous_results = execution.get("tool_results", [])
        previous_refs = execution.get("_worker_result_refs", {})
        result_refs = dict(previous_refs) if isinstance(previous_refs, Mapping) else {}
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

    async def finalize(self, state: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(state.get("result", {}))
        return {"result": result, "waiting": bool(state.get("waiting")), "status": "waiting_approval" if state.get("waiting") else str(result.get("status", "completed"))}

    @staticmethod
    def route_after_gate(state: Mapping[str, Any]) -> str:
        if state.get("waiting"):
            return "finish_worker"
        result = state.get("result", {})
        status = str(result.get("status", "completed"))
        if status not in {"running", "completed"}:
            return "finish_worker"
        return "execute_tool_batch" if state.get("result", {}).get("tool_calls") else "finish_worker"

    @staticmethod
    def route_after_tools(state: Mapping[str, Any]) -> str:
        if state.get("pending_approval"):
            return "request_worker_approval"
        return "finish_worker" if state.get("result", {}).get("error") else "worker_agent_loop"


__all__ = ["WorkerWorkflow"]
