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

from domain.orchestration import (
    OrchestrationCommand,
    OrchestrationCommandType,
    Plan,
    PlanEdge,
    PlanRevision,
    ReviewAction,
    ReviewDecision,
    ReviewContext,
    Task,
)

from .protocols import RootWorkflowDependencies, RootWorkflowPort
from .worker_tools import list_worker_tools, worker_tool_manifest

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


def _coerce_plan_edge(value: Any) -> PlanEdge:
    """Accept object and two-item array forms commonly emitted as JSON DAG edges."""
    if isinstance(value, Mapping):
        source = value.get("from_task_id") or value.get("from") or ""
        target = value.get("to_task_id") or value.get("to") or ""
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        source, target = value
    else:
        raise ValueError("plan edges must be objects or two-item arrays")
    return PlanEdge(str(source), str(target))


def _coerce_plan(
    value: Any, *, request: Mapping[str, Any], task: Mapping[str, Any]
) -> tuple[Plan, list[Task]]:
    original_goal = str(
        request.get("user_message") or task.get("goal") or ""
    ).strip()
    if isinstance(value, tuple) and len(value) == 2:
        value, raw_tasks = value
    else:
        raw_tasks = None
    if isinstance(value, Plan):
        plan = Plan(
            plan_id=value.plan_id,
            goal=original_goal or value.goal,
            task_ids=value.task_ids,
            edges=value.edges,
            max_parallelism=value.max_parallelism,
            status=value.status,
            session_id=value.session_id,
            run_id=value.run_id,
        )
        raw_tasks = raw_tasks or []
    else:
        payload = as_dict(value)
        raw_tasks = raw_tasks or payload.get("tasks", [])
        task_ids = tuple(str(item) for item in payload.get("task_ids", []))
        if not task_ids:
            task_ids = tuple(
                str(item.get("task_id")) for item in raw_tasks if item.get("task_id")
            )
        edges = tuple(_coerce_plan_edge(item) for item in payload.get("edges", []))
        plan = Plan(
            plan_id=str(payload.get("plan_id") or f"plan:{request.get('run_id', '')}"),
            goal=original_goal or str(payload.get("goal") or ""),
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
                timeout_seconds=payload.get("timeout_seconds", 3600.0),
            )
        )
    return plan, tasks


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


def _deferred_version_review_decision(
    context: ReviewContext, state: Mapping[str, Any]
) -> ReviewDecision | None:
    """在当前版本完成并 Review 后生成下一阶段的确定性修订。"""
    deferred = state.get("deferred_plan_versions")
    if not isinstance(deferred, Mapping):
        return None
    raw_tasks = deferred.get(str(context.plan_version + 1))
    if not isinstance(raw_tasks, (list, tuple)) or not raw_tasks:
        return None
    if context.remaining_tasks:
        return None
    results = (*context.previous_results_summary, *context.round_results)
    if not results or any(
        item.get("status") not in {"completed", "done"} for item in results
    ):
        return None

    plan = state.get("plan", {})
    request = state.get("request", {})
    dag_nodes = context.full_dag_summary.get("nodes", ())
    dag_task_ids = (
        [
            node.get("task_id")
            for node in dag_nodes
            if isinstance(node, Mapping) and isinstance(node.get("task_id"), str)
        ]
        if isinstance(dag_nodes, (list, tuple))
        else []
    )
    existing_ids = (
        dag_task_ids
        or (plan.get("task_ids", ()) if isinstance(plan, Mapping) else ())
    )
    run_id = request.get("run_id") if isinstance(request, Mapping) else None
    if (
        not isinstance(existing_ids, (list, tuple))
        or not existing_ids
        or any(not isinstance(task_id, str) for task_id in existing_ids)
        or len(existing_ids) != len(set(existing_ids))
        or not isinstance(run_id, str)
        or not run_id
    ):
        return None
    existing_id_set = set(existing_ids)
    new_task_ids = {
        str(item.get("task_id"))
        for item in raw_tasks
        if isinstance(item, Mapping) and isinstance(item.get("task_id"), str)
    }
    requested_to_scoped = {
        task_id: f"{task_id}:{run_id}" for task_id in new_task_ids
    }
    if (
        len(new_task_ids) != len(raw_tasks)
        or existing_id_set & new_task_ids
        or existing_id_set & set(requested_to_scoped.values())
    ):
        return None

    new_tasks: list[dict[str, Any]] = []
    for item in raw_tasks:
        if not isinstance(item, Mapping):
            return None
        task_id = item.get("task_id")
        title = item.get("title")
        objective = item.get("objective", item.get("instruction"))
        dependencies = item.get("depends_on", ())
        if (
            not isinstance(task_id, str)
            or not task_id.strip()
            or not isinstance(title, str)
            or not isinstance(objective, str)
            or not isinstance(dependencies, (list, tuple))
            or any(not isinstance(dep, str) for dep in dependencies)
        ):
            return None
        scoped_dependencies = []
        for dependency in dependencies:
            scoped = f"{dependency}:{run_id}"
            if dependency in existing_id_set:
                scoped_dependencies.append(dependency)
            elif scoped in existing_id_set:
                scoped_dependencies.append(scoped)
            elif dependency in requested_to_scoped:
                scoped_dependencies.append(requested_to_scoped[dependency])
            elif dependency in requested_to_scoped.values():
                scoped_dependencies.append(dependency)
            else:
                return None
            if scoped_dependencies[-1] == requested_to_scoped[task_id]:
                return None
        input_value = item.get("input", {})
        expected_output = item.get("expected_output", {})
        tools = item.get("allowed_tools", ())
        if not isinstance(input_value, Mapping):
            input_value = {"value": input_value}
        if not isinstance(expected_output, Mapping):
            expected_output = {"value": expected_output}
        if not isinstance(tools, (list, tuple)) or any(
            not isinstance(tool, str) for tool in tools
        ):
            return None
        new_tasks.append(
            {
                "task_id": task_id,
                "title": title,
                "objective": objective,
                "input": dict(input_value),
                "expected_output": dict(expected_output),
                "allowed_tools": list(tools),
                "worker_type": str(item.get("worker_type") or "general"),
                "timeout_seconds": item.get("timeout_seconds", 3600.0),
                "depends_on": list(dict.fromkeys(scoped_dependencies)),
            }
        )
    return ReviewDecision(
        action=ReviewAction.REPLAN,
        reason=(
            f"User-requested Plan Version {context.plan_version + 1} is due after "
            "completion and Root Review of the current version."
        ),
        revision=PlanRevision(
            base_version=context.plan_version,
            keep_task_ids=tuple(existing_ids),
            cancel_task_ids=(),
            new_tasks=tuple(new_tasks),
        ),
    )


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
        lines.append(f"{label}：{text}" if text else f"{label}：未返回可展示的文本结果。")
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
            "deferred_plan_versions": json_value(
                payload.get("deferred_plan_versions", {})
            ),
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
        worker_tools = set(await list_worker_tools(deps.tool_service))
        explicit_allowed_tools = task.get("allowed_tools")
        explicit_tool_set = (
            set(explicit_allowed_tools)
            if isinstance(explicit_allowed_tools, (list, tuple, set, frozenset))
            else None
        )
        if explicit_tool_set is not None:
            unavailable_tools = explicit_tool_set - worker_tools
            if unavailable_tools:
                raise ValueError(
                    "explicit_worker_tools_unavailable: "
                    + ", ".join(sorted(unavailable_tools))
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
                    dict.fromkeys(
                        name
                        for name in (
                            *item.allowed_tools,
                            *(
                                tool_name
                                for tool_name in explicit_tool_set or ()
                                if tool_name in item.input
                            ),
                        )
                        if name in worker_tools
                        and (
                            explicit_tool_set is None
                            or name in explicit_tool_set
                        )
                    )
                ),
                worker_type="general",
                timeout_seconds=item.timeout_seconds,
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
        await call(
            deps.orchestration_command_handler.submit_plan,
            command,
            plan,
            tasks,
            legal_tools=worker_tools,
        )
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
        loaded = await call(
            deps.plan_loader, plan=plan, plan_id=plan.get("plan_id"), state=state
        )
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

    async def review_completed_round(
        self, state: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Load a fixed Round context, decide once, and persist the action."""
        deps = self.dependencies
        resume_value = state.get("resume_value", {})
        wait_context = state.get("root_wait", {})
        if not isinstance(resume_value, Mapping) or not isinstance(
            wait_context, Mapping
        ):
            raise RuntimeError("root_review_resume_identity_missing")
        if resume_value.get("type") != "round_completed":
            raise RuntimeError("root_review_resume_type_invalid")
        identity_fields = ("run_id", "plan_id", "round_id", "plan_version")
        if (
            any(name not in resume_value or name not in wait_context for name in identity_fields)
            or any(
                resume_value.get(name) != wait_context.get(name)
                for name in identity_fields
            )
        ):
            raise RuntimeError("root_review_resume_identity_mismatch")
        request = state.get("request", {})
        if not isinstance(request, Mapping) or request.get("run_id") != resume_value.get(
            "run_id"
        ):
            raise RuntimeError("root_review_run_identity_mismatch")
        if deps.orchestration_command_handler is None:
            raise RuntimeError("root_review_command_handler_not_configured")

        round_id = str(resume_value["round_id"])
        plan_id = str(resume_value["plan_id"])
        plan_version = resume_value["plan_version"]
        run_id = str(resume_value["run_id"])
        if not isinstance(plan_version, int) or isinstance(plan_version, bool):
            raise RuntimeError("root_review_plan_version_invalid")
        idempotency_key = f"root-review:{round_id}"
        decision = await deps.orchestration_command_handler.load_review_decision(
            scope_key=run_id,
            idempotency_key=idempotency_key,
            plan_id=plan_id,
            plan_version=plan_version,
            round_id=round_id,
        )
        if decision is None:
            if deps.review_context_repository is None:
                raise RuntimeError("root_review_context_repository_not_configured")
            context = await deps.review_context_repository.load_review_context(
                round_id
            )
            if (
                context.round_id != round_id
                or context.plan_id != plan_id
                or context.plan_version != plan_version
            ):
                raise RuntimeError("root_review_context_identity_mismatch")
            deferred_decision = _deferred_version_review_decision(context, state)
            if deferred_decision is not None:
                decision = deferred_decision
            else:
                reviewer = getattr(deps.agent, "review", None)
                if not callable(reviewer):
                    raise RuntimeError("root_review_agent_not_configured")
                decision = await call(reviewer, context, request=request)
        if not isinstance(decision, ReviewDecision):
            raise RuntimeError("root_review_decision_invalid")

        command_type = {
            ReviewAction.CONTINUE: OrchestrationCommandType.CONTINUE_PLAN,
            ReviewAction.REPLAN: OrchestrationCommandType.REVISE_PLAN,
            ReviewAction.FINISH: OrchestrationCommandType.FINISH_PLAN,
        }[decision.action]
        command = OrchestrationCommand(
            message_id=f"root-review:{round_id}",
            command_type=command_type,
            idempotency_key=idempotency_key,
            correlation_id=run_id,
            occurred_at=datetime.now(timezone.utc),
            plan_id=plan_id,
            scope_key=run_id,
            payload={"action": decision.action.value, "reason": decision.reason},
            plan_version=plan_version,
            round_id=round_id,
            causation_id=(
                str(resume_value["interrupt_id"])
                if resume_value.get("interrupt_id")
                else None
            ),
        )
        receipt = await deps.orchestration_command_handler.apply_review_decision(
            command, decision
        )
        return {
            "review_action": decision.action.value,
            "review_reason": decision.reason,
            "review_receipt": as_dict(receipt),
        }

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
        repository = deps.plan_repository
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
            response = {
                "content": render_plan_results(
                    state.get("plan_results", {}),
                    state.get("plan", {}).get("tasks", []),
                )
            }
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
