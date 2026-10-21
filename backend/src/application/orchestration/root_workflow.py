"""Root run workflow independent from LangGraph.

This module owns the application steps required to prepare a request, acquire
context, execute an agent and materialize a worker plan.  The LangGraph
adapter only supplies state and connects these steps into a graph.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Callable, Mapping

from domain.orchestration import Plan, PlanEdge, Task

from .protocols import RootWorkflowDependencies, RootWorkflowPort
from .worker_tools import list_worker_read_tools, worker_read_tool_manifest

logger = logging.getLogger(__name__)


async def call(value: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    # Synchronous adapters are allowed by the application port, but must not
    # run on the ASGI event-loop thread.
    if inspect.iscoroutinefunction(value) or inspect.iscoroutinefunction(
        getattr(value, "__call__", None)
    ):
        result = value(*args, **kwargs)
    else:
        result = await asyncio.to_thread(value, *args, **kwargs)
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
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_value(item) for item in value]
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
                str(item.get("from_task_id") or item.get("from") or ""),
                str(item.get("to_task_id") or item.get("to") or ""),
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
        raw_input = payload.get("input", {})
        raw_expected = payload.get("expected_output", {})
        tasks.append(
            Task(
                task_id=str(payload.get("task_id", "")),
                title=str(payload.get("title", payload.get("objective", "Task"))),
                objective=str(payload.get("objective", "")),
                input=(
                    raw_input
                    if isinstance(raw_input, Mapping)
                    else {"value": raw_input}
                ),
                expected_output=(
                    raw_expected
                    if isinstance(raw_expected, Mapping)
                    else {"value": raw_expected}
                ),
                allowed_tools=tuple(
                    dict.fromkeys(
                        str(name).strip()
                        for name in payload.get("allowed_tools", [])
                        if str(name).strip()
                    )
                )
                if isinstance(payload.get("allowed_tools", []), (list, tuple, set))
                else (),
                worker_type="general",
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


class RootWorkflow(RootWorkflowPort):
    """Execute Root application steps without depending on LangGraph."""

    def __init__(self, dependencies: RootWorkflowDependencies) -> None:
        self.dependencies = dependencies

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
        if not state.get("resume_value"):
            update = await deps.request_preparation.prepare(
                request=request, state=state
            )
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

    async def understand_task(self, state: Mapping[str, Any]) -> dict[str, Any]:
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
        planned = deps.context_planner.plan(
            task, requested_file_ids=request.get("attachment_ids", [])
        )
        return {"context_plan": as_dict(planned)}

    async def acquire_context(self, state: Mapping[str, Any]) -> dict[str, Any]:
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

    async def prepare_harness_input(self, state: Mapping[str, Any]) -> dict[str, Any]:
        request = state.get("request", {})
        task = state.get("understanding", {}).get("task_spec", {})
        worker_tools = (
            await worker_read_tool_manifest(self.dependencies.tool_service)
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

    async def run_root_execution_loop(self, state: Mapping[str, Any]) -> dict[str, Any]:
        deps = self.dependencies
        payload = as_dict(
            await deps.agent.__call__( state=state, harness_input=state.get("harness_input", {}))
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
        run_id = str(request.get("run_id") or "").strip()
        if run_id and not plan.plan_id.endswith(f":{run_id}"):
            plan = Plan(
                plan_id=f"{plan.plan_id}:{run_id}",
                goal=plan.goal,
                task_ids=plan.task_ids,
                edges=plan.edges,
                max_parallelism=plan.max_parallelism,
                status=plan.status,
                session_id=plan.session_id,
                run_id=run_id,
            )
        worker_read_tools = set(
            await list_worker_read_tools(deps.tool_service)
        )
        # Task ids are database-global, so bind model-produced ids to this run.
        task_id_map = (
            {item.task_id: f"{item.task_id}:{run_id}" for item in tasks}
            if run_id
            else {item.task_id: item.task_id for item in tasks}
        )
        tasks = [
            Task(
                task_id=task_id_map[item.task_id],
                title=item.title,
                objective=item.objective,
                input=item.input,
                expected_output=item.expected_output,
                allowed_tools=tuple(
                    name for name in item.allowed_tools if name in worker_read_tools
                ),
                worker_type="general",
            )
            for item in tasks
        ]
        plan = Plan(
            plan_id=plan.plan_id,
            goal=plan.goal,
            task_ids=tuple(item.task_id for item in tasks),
            edges=tuple(
                PlanEdge(
                    task_id_map.get(edge.from_task_id, edge.from_task_id),
                    task_id_map.get(edge.to_task_id, edge.to_task_id),
                )
                for edge in plan.edges
                if task_id_map.get(edge.from_task_id, edge.from_task_id)
                in {item.task_id for item in tasks}
                and task_id_map.get(edge.to_task_id, edge.to_task_id)
                in {item.task_id for item in tasks}
            ),
            max_parallelism=plan.max_parallelism,
            status=plan.status,
            session_id=plan.session_id,
            run_id=plan.run_id,
        )
        if deps.validate_plan is not None:
            await call(deps.validate_plan, plan=plan, tasks=tasks)
        if deps.persist_plan is not None:
            await call(deps.persist_plan, plan=plan, tasks=tasks, state=state)
        elif deps.orchestration is not None and hasattr(
            deps.orchestration, "create_plan"
        ):
            await call(deps.orchestration.create_plan, plan=plan, tasks=tasks)
        if deps.event_publisher is not None:
            from domain.events import ApplicationEvent, EventDurability, EventType

            await call(
                deps.event_publisher.publish,
                ApplicationEvent(
                    event_type=EventType.PLAN_CREATED,
                    durability=EventDurability.DURABLE,
                    session_id=request.get("session_id", ""),
                    run_id=request.get("run_id"),
                    payload={
                        "activity_id": f"plan:{plan.plan_id}",
                        "activity_type": "plan",
                        "name": "execution_plan",
                        "parent_activity_id": f"run:{request.get('run_id', '')}",
                        "plan_id": plan.plan_id,
                        "goal": plan.goal,
                        "status": "running",
                        "task_ids": list(plan.task_ids),
                        "edges": [
                            {
                                "from_task_id": edge.from_task_id,
                                "to_task_id": edge.to_task_id,
                            }
                            for edge in plan.edges
                        ],
                        "tasks": [_plan_dict({}, [task])["tasks"][0] for task in tasks],
                    },
                ),
            )
        return {"plan": _plan_dict(plan, tasks), "status": "running"}

    async def wait_for_plan_completion(
        self, state: Mapping[str, Any]
    ) -> dict[str, Any]:
        deps = self.dependencies
        plan = state.get("plan", {})
        if deps.worker_scheduler is not None and plan.get("plan_id"):
            await call(deps.worker_scheduler.start_ready_tasks, plan_id=plan["plan_id"])
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
