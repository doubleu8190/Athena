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
    ) -> None:
        self.execute_tool = execute_tool
        self.execute = execute
        self.worker = worker
        self.resume = resume

    async def prepare(self, state: Mapping[str, Any]) -> dict[str, Any]:
        execution = dict(state.get("execution", {}))
        return {"execution": json_value(execution), "input": json_value(state.get("input", execution.get("task", {})))}

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
        calls = result.get("tool_calls", []) if isinstance(result, Mapping) else []
        tool_results = [as_dict(await call(self.execute_tool, tool_call=item, execution=state.get("execution", {}), state=state)) for item in calls]
        execution = dict(state.get("execution", {}))
        execution["tool_results"] = tool_results
        return {"execution": execution, "result": {}, "waiting": False}

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
        return "execute_tool_batch" if state.get("result", {}).get("tool_calls") else "finish_worker"

    @staticmethod
    def route_after_tools(state: Mapping[str, Any]) -> str:
        return "finish_worker" if state.get("result", {}).get("error") else "worker_agent_loop"


__all__ = ["WorkerWorkflow"]
