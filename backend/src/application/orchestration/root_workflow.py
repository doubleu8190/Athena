"""Root run workflow independent from LangGraph.

This module owns the application steps required to prepare a request, acquire
context, execute an agent and materialize a worker plan.  The LangGraph
adapter only supplies state and connects these steps into a graph.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
from typing import Any, Callable, Mapping

from domain.orchestration import Plan, PlanEdge, Task

from .protocols import RootWorkflowDependencies


async def call(value: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    result = value(*args, **kwargs)
    if hasattr(result, "__await__"):
        return await result
    return result


def as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if is_dataclass(value):
        return as_dict(asdict(value))
    if hasattr(value, "model_dump"):
        return as_dict(value.model_dump())
    if hasattr(value, "__dict__"):
        return as_dict(vars(value))
    return {"value": json_value(value)}


def json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value):
        return json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_value(item) for item in value]
    if hasattr(value, "model_dump"):
        return json_value(value.model_dump())
    if hasattr(value, "value") and isinstance(value.value, (str, int, float, bool)):
        return value.value
    if hasattr(value, "__dict__"):
        return json_value(vars(value))
    return str(value)


def _plan_dict(plan: Any, tasks: list[Any] | None = None) -> dict[str, Any]:
    if isinstance(plan, Plan):
        value = {
            "plan_id": plan.plan_id,
            "goal": plan.goal,
            "task_ids": list(plan.task_ids),
            "edges": [asdict(edge) for edge in plan.edges],
            "max_parallelism": plan.max_parallelism,
            "session_id": plan.session_id,
            "run_id": plan.run_id,
        }
    else:
        value = as_dict(plan)
    if tasks is not None:
        value["tasks"] = [as_dict(task) for task in tasks]
    value.setdefault(
        "task_ids", [item.get("task_id") for item in value.get("tasks", [])]
    )
    value.setdefault("edges", [])
    return json_value(value)


def _coerce_plan(
    value: Any, *, request: Mapping[str, Any], task: Mapping[str, Any]
) -> tuple[Plan, list[Task]]:
    if isinstance(value, tuple) and len(value) == 2:
        value, raw_tasks = value
    else:
        raw_tasks = None
    if isinstance(value, Plan):
        plan = value
        raw_tasks = raw_tasks or []
    else:
        payload = as_dict(value)
        raw_tasks = raw_tasks or payload.get("tasks", [])
        task_ids = tuple(str(item) for item in payload.get("task_ids", []))
        if not task_ids:
            task_ids = tuple(
                str(item.get("task_id")) for item in raw_tasks if item.get("task_id")
            )
        edges = tuple(
            PlanEdge(
                str(item.get("from_task_id", item.get("from", ""))),
                str(item.get("to_task_id", item.get("to", ""))),
            )
            for item in payload.get("edges", [])
        )
        plan = Plan(
            plan_id=str(payload.get("plan_id") or f"plan:{request.get('run_id', '')}"),
            goal=str(
                payload.get("goal")
                or task.get("goal")
                or request.get("user_message", "")
            ),
            task_ids=task_ids,
            edges=edges,
            max_parallelism=max(1, int(payload.get("max_parallelism", 1))),
            session_id=request.get("session_id"),
            run_id=request.get("run_id"),
        )
    tasks: list[Task] = []
    for item in raw_tasks or []:
        if isinstance(item, Task):
            tasks.append(item)
            continue
        payload = as_dict(item)
        tasks.append(
            Task(
                task_id=str(payload.get("task_id", "")),
                title=str(payload.get("title", payload.get("objective", "Task"))),
                objective=str(payload.get("objective", "")),
                input=payload.get("input", {}),
                expected_output=payload.get("expected_output", {}),
                allowed_tools=tuple(payload.get("allowed_tools", ())),
                worker_type=str(payload.get("worker_type", "general")),
            )
        )
    return plan, tasks


def render_plan_results(results: Mapping[str, Any]) -> str:
    if not results:
        return "计划已完成，但没有可展示的任务结果。"
    lines = []
    for task_id, value in results.items():
        payload = value if isinstance(value, Mapping) else {"output": value}
        lines.append(
            f"[{task_id}] {payload.get('output', payload.get('error', payload))}"
        )
    return "\n".join(lines)


class RootWorkflow:
    """Execute Root application steps without depending on LangGraph."""

    def __init__(self, dependencies: RootWorkflowDependencies | None = None) -> None:
        self.dependencies = dependencies or RootWorkflowDependencies()

    async def prepare_request(self, state: Mapping[str, Any]) -> dict[str, Any]:
        deps = self.dependencies
        request = dict(state.get("request", {}))
        request.setdefault("session_id", "")
        request.setdefault("run_id", "")
        request.setdefault("user_message", "")
        request["attachment_ids"] = list(
            dict.fromkeys(request.get("attachment_ids", []))
        )
        extra: dict[str, Any] = {}
        if deps.request_handler is not None and not state.get("resume_value"):
            update = await call(deps.request_handler, request=request, state=state)
            if isinstance(update, Mapping):
                request.update(json_value(update.get("request", update)))
                extra = {
                    key: json_value(value)
                    for key, value in update.items()
                    if key != "request"
                }
        if deps.history_loader is not None and "history" not in request:
            request["history"] = json_value(
                await call(
                    deps.history_loader,
                    session_id=request["session_id"],
                    request=request,
                )
            )
        if (
            deps.attachment_loader is not None
            and request["attachment_ids"]
            and "attachment_refs" not in request
        ):
            request["attachment_refs"] = json_value(
                await call(
                    deps.attachment_loader,
                    session_id=request["session_id"],
                    attachment_ids=request["attachment_ids"],
                    request=request,
                )
            )
        return {"request": json_value(request), **extra}

    async def understand_task(self, state: Mapping[str, Any]) -> dict[str, Any]:
        deps = self.dependencies
        request = state.get("request", {})
        if deps.task_understanding is None:
            task = {
                "goal": request.get("user_message", ""),
                "mode": "answer",
                "context_requirements": ["conversation"],
            }
            return {"understanding": {"task_spec": task, "source": "fallback"}}
        result = await call(
            deps.task_understanding.understand,
            session_id=request.get("session_id", ""),
            user_message=request.get("user_message", ""),
            history=request.get("history", []),
            attachment_refs=request.get("attachment_refs", []),
            knowledge_bases=(
                await call(deps.knowledge_loader, request)
                if deps.knowledge_loader
                else []
            ),
        )
        payload = as_dict(result)
        payload["task_spec"] = as_dict(payload.get("task") or payload.get("task_spec"))
        return {"understanding": payload}

    async def clarification_response(self, state: Mapping[str, Any]) -> dict[str, Any]:
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
            "next_route": "assemble_final_response",
        }

    async def plan_context(self, state: Mapping[str, Any]) -> dict[str, Any]:
        deps = self.dependencies
        task = state.get("understanding", {}).get("task_spec", {})
        request = state.get("request", {})
        if deps.context_planner is not None:
            planned = deps.context_planner.plan(
                task, requested_file_ids=request.get("attachment_ids", [])
            )
            return {"context_plan": as_dict(planned)}
        providers = list(task.get("context_requirements") or ["conversation"])
        attachment_ids = tuple(
            task.get("attachment_ids") or request.get("attachment_ids") or []
        )
        if request.get("attachment_refs") and "file" not in providers:
            providers.append("file")
        hints = task.get("query_hints") or {}
        if isinstance(hints, (list, tuple, str)):
            hints = {"knowledge": hints if isinstance(hints, str) else " ".join(hints)}
        return {
            "context_plan": {
                "providers": providers,
                "memory_query": hints.get("memory", ""),
                "knowledge_query": hints.get("knowledge", ""),
                "graph_query": hints.get("graph", ""),
                "file_query": hints.get("file", ""),
                "file_ids": list(attachment_ids),
                "knowledge_base_ids": list(task.get("knowledge_base_ids") or []),
                "max_files": 5,
                "limit_per_file": 5,
                "max_items": 12,
                "max_tokens": 4000,
                "recall_limit": 30,
                "rerank_limit": 10,
            }
        }

    async def acquire_context(self, state: Mapping[str, Any]) -> dict[str, Any]:
        deps = self.dependencies
        if deps.context is None:
            return {"context_bundle": {"items": [], "provider_results": []}}
        from application.runs.context import ContextPlan

        request = state.get("request", {})
        raw_plan = state.get("context_plan", {})
        context_plan = (
            deps.context_plan_factory(raw_plan)
            if deps.context_plan_factory
            else ContextPlan(**raw_plan)
        )
        value = await call(
            deps.context.retrieve_context,
            session_id=request.get("session_id", ""),
            task=state.get("understanding", {}).get("task_spec", {}),
            plan=context_plan,
            run_id=request.get("run_id"),
        )
        return {"context_bundle": as_dict(value)}

    async def prepare_harness_input(self, state: Mapping[str, Any]) -> dict[str, Any]:
        request = state.get("request", {})
        return {
            "harness_input": {
                "messages": list(request.get("history", []))
                + [{"role": "user", "content": request.get("user_message", "")}],
                "task": state.get("understanding", {}).get("task_spec", {}),
                "context": state.get("context_bundle", {}),
            }
        }

    async def run_root_execution_loop(self, state: Mapping[str, Any]) -> dict[str, Any]:
        deps = self.dependencies
        if deps.agent is None:
            return {
                "response": {
                    "content": state.get("request", {}).get("user_message", "")
                },
                "next_route": "post_process_and_build_result",
            }
        payload = as_dict(
            await call(
                deps.agent, state=state, harness_input=state.get("harness_input", {})
            )
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
            route = "post_process_and_build_result"
        return {
            "response": payload.get("response", payload),
            "execution": payload.get("execution", {}),
            "next_route": route,
            "plan": payload.get("plan", {}),
        }

    async def materialize_execution_plan(
        self, state: Mapping[str, Any]
    ) -> dict[str, Any]:
        deps = self.dependencies
        request = state.get("request", {})
        task = state.get("understanding", {}).get("task_spec", {})
        raw = state.get("plan") or None
        if not raw and deps.plan_builder is not None:
            raw = await call(deps.plan_builder, state=state, task=task)
        if not raw:
            return {
                "response": {
                    "content": "无法生成可执行计划。",
                    "error": {
                        "code": "plan_generation_failed",
                        "message": "Root Agent did not return a structured plan.",
                    },
                },
                "status": "failed",
                "next_route": "post_process_and_build_result",
            }
        plan, tasks = _coerce_plan(raw, request=request, task=task)
        if deps.validate_plan is not None:
            await call(deps.validate_plan, plan=plan, tasks=tasks)
        if deps.persist_plan is not None:
            await call(deps.persist_plan, plan=plan, tasks=tasks, state=state)
        elif deps.orchestration is not None and hasattr(
            deps.orchestration, "create_plan"
        ):
            await call(deps.orchestration.create_plan, plan=plan, tasks=tasks)
        return {"plan": _plan_dict(plan, tasks), "status": "running"}

    async def wait_for_plan_completion(
        self, state: Mapping[str, Any]
    ) -> dict[str, Any]:
        deps = self.dependencies
        plan = state.get("plan", {})
        if deps.scheduler is not None and plan.get("plan_id"):
            await call(deps.scheduler.start_ready_tasks, plan_id=plan["plan_id"])
        if deps.plan_loader is None:
            return {"waiting": True, "status": "waiting_for_plan"}
        deadline = asyncio.get_running_loop().time() + 120.0
        while True:
            loaded = await call(
                deps.plan_loader, plan=plan, plan_id=plan.get("plan_id"), state=state
            )
            if isinstance(loaded, Mapping) and loaded.get("completed"):
                return {
                    "waiting": False,
                    "plan_results": json_value(loaded.get("results", {})),
                }
            if asyncio.get_running_loop().time() >= deadline:
                return {"waiting": True, "status": "waiting_for_plan"}
            await asyncio.sleep(0.05)

    async def project_plan_result(self, state: Mapping[str, Any]) -> dict[str, Any]:
        deps = self.dependencies
        plan = state.get("plan", {})
        loaded: Any = None
        if deps.plan_loader is not None:
            loaded = await call(
                deps.plan_loader, plan=plan, plan_id=plan.get("plan_id"), state=state
            )
        elif (
            deps.plan_repository is not None
            and plan.get("plan_id")
            and hasattr(deps.plan_repository, "get_plan")
        ):
            loaded = {
                "plan": await call(
                    deps.plan_repository.get_plan, plan_id=plan["plan_id"]
                )
            }
        results = (
            loaded.get("results", loaded.get("plan_results", {}))
            if isinstance(loaded, Mapping)
            else {}
        )
        repository = deps.plan_repository or deps.orchestration
        if (
            not results
            and repository is not None
            and plan.get("plan_id")
            and hasattr(repository, "get_results")
        ):
            results = await call(repository.get_results, plan_id=plan["plan_id"])
        return {
            "plan_results": json_value(results),
            "response": {"plan_results": json_value(results)},
            "next_route": "finalize_plan_stream",
        }

    async def finalize_plan_stream(self, state: Mapping[str, Any]) -> dict[str, Any]:
        deps = self.dependencies
        if deps.finalizer is None:
            response = {"content": render_plan_results(state.get("plan_results", {}))}
        else:
            response = as_dict(
                await call(
                    deps.finalizer, state=state, results=state.get("plan_results", {})
                )
            )
        return {
            "response": response,
            "next_route": "assemble_final_response",
            "waiting": False,
        }

    async def post_process_and_build_result(
        self, state: Mapping[str, Any]
    ) -> dict[str, Any]:
        response = dict(state.get("response", {}))
        if not response and state.get("error"):
            response = {"error": state.get("error")}
        return {
            "result": response,
            "next_route": "assemble_final_response",
            "status": state.get("status", "completed"),
        }

    async def assemble_final_response(self, state: Mapping[str, Any]) -> dict[str, Any]:
        response = dict(state.get("response", {}))
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
    "RootWorkflowDependencies",
    "as_dict",
    "call",
    "json_value",
    "render_plan_results",
]
