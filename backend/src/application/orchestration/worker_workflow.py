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
        worker: Any | None = None,
        resume: Callable[..., Any] | None = None,
        max_tool_rounds: int = 4,
        max_tool_calls_per_round: int = 8,
    ) -> None:
        if max_tool_rounds < 1 or max_tool_calls_per_round < 1:
            raise ValueError("worker tool budgets are invalid")
        self.execute_tool = execute_tool
        self.execute = execute
        self.worker = worker
        self.resume = resume
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
        execution_fn = self.execute
        if execution_fn is None and self.worker is not None and hasattr(self.worker, "execute"):
            execution_fn = self.worker.execute
        if execution_fn is None:
            return {"result": {"status": "failed", "error": {"code": "worker_not_configured"}}}
        payload = as_dict(await call(execution_fn, execution=state.get("execution", {}), state=state))
        return {"result": payload, "waiting": str(payload.get("status", "done")) == "waiting_approval"}

    async def approval_gate(self, state: Mapping[str, Any]) -> dict[str, Any]:
        result = state.get("result", {})
        return {"status": "waiting_approval" if state.get("waiting") else str(result.get("status", "done"))}

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
        tool_results = [
            as_dict(
                await call(
                    self.execute_tool,
                    tool_call=item,
                    execution=state.get("execution", {}),
                    state=state,
                    tool_round=tool_round,
                    call_index=index,
                )
            )
            for index, item in enumerate(calls)
        ]
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
        }

    async def resume_node(self, state: Mapping[str, Any]) -> dict[str, Any]:
        resume_fn = self.resume
        if resume_fn is None and self.worker is not None and hasattr(self.worker, "resume"):
            resume_fn = self.worker.resume
        if resume_fn is None:
            return {"result": {"status": "failed", "error": {"code": "worker_resume_not_configured"}}, "waiting": False}
        decisions = state.get("response", {}).get("decisions", {})
        payload = as_dict(await call(resume_fn, execution=state.get("execution", {}), decisions=decisions, state=state))
        return {"result": payload, "waiting": str(payload.get("status", "done")) == "waiting_approval"}

    async def finalize(self, state: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(state.get("result", {}))
        return {"result": result, "waiting": bool(state.get("waiting")), "status": "waiting_approval" if state.get("waiting") else str(result.get("status", "done"))}

    @staticmethod
    def route(state: Mapping[str, Any]) -> str:
        return "resume" if state.get("response", {}).get("resume") else "worker_agent_loop"

    @staticmethod
    def route_after_gate(state: Mapping[str, Any]) -> str:
        if state.get("waiting"):
            return "finish_worker"
        result = state.get("result", {})
        status = str(result.get("status", "done"))
        if status not in {"done", "completed"} and result.get("error"):
            return "finish_worker"
        return "execute_tool_batch" if state.get("result", {}).get("tool_calls") else "finish_worker"

    @staticmethod
    def route_after_tools(state: Mapping[str, Any]) -> str:
        return "finish_worker" if state.get("result", {}).get("error") else "worker_agent_loop"


__all__ = ["WorkerWorkflow"]
