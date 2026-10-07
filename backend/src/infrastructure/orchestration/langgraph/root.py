"""Root Agent graph.

The graph is deliberately a thin adapter.  It owns routing and checkpoint
identity while task understanding, context acquisition, plan persistence and
worker scheduling remain injected application capabilities.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Callable, Mapping

from domain.orchestration import Plan, PlanEdge, Task

from ._builder import END, GraphBuilder, call
from .state import RootGraphState, json_value


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(json_value(value))
    if is_dataclass(value):
        return dict(json_value(asdict(value)))
    if hasattr(value, "__dict__"):
        return dict(json_value(vars(value)))
    return {"value": json_value(value)}


def _task_spec(value: Any) -> dict[str, Any]:
    return _as_dict(value)


@dataclass(slots=True)
class RootGraphDependencies:
    """Application capabilities used by the Root graph.

    All fields are optional to keep the graph useful with focused test doubles
    and during staged migration.  A missing capability produces a structured
    response rather than putting a service object in checkpoint state.
    """

    task_understanding: Any | None = None
    context: Any | None = None
    orchestration: Any | None = None
    plan_repository: Any | None = None
    scheduler: Any | None = None
    request_handler: Callable[..., Any] | None = None
    history_loader: Callable[..., Any] | None = None
    attachment_loader: Callable[..., Any] | None = None
    knowledge_loader: Callable[..., Any] | None = None
    agent: Callable[..., Any] | None = None
    plan_builder: Callable[..., Any] | None = None
    plan_loader: Callable[..., Any] | None = None
    finalizer: Callable[..., Any] | None = None
    persist_plan: Callable[..., Any] | None = None
    validate_plan: Callable[..., Any] | None = None
    event_publisher: Any | None = None


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
        value = _as_dict(plan)
    if tasks is not None:
        value["tasks"] = [_as_dict(task) for task in tasks]
    value.setdefault("task_ids", [item.get("task_id") for item in value.get("tasks", [])])
    value.setdefault("edges", [])
    return dict(json_value(value))


def _coerce_plan(value: Any, *, request: Mapping[str, Any], task: Mapping[str, Any]) -> tuple[Plan, list[Task]]:
    if isinstance(value, tuple) and len(value) == 2:
        value, raw_tasks = value
    else:
        raw_tasks = None
    if isinstance(value, Plan):
        plan = value
        raw_tasks = raw_tasks or []
    else:
        payload = _as_dict(value)
        raw_tasks = raw_tasks or payload.get("tasks", [])
        task_ids = tuple(str(item) for item in payload.get("task_ids", []))
        if not task_ids:
            task_ids = tuple(str(item.get("task_id")) for item in raw_tasks if item.get("task_id"))
        edges = tuple(
            PlanEdge(str(item.get("from_task_id", item.get("from", ""))), str(item.get("to_task_id", item.get("to", ""))))
            for item in payload.get("edges", [])
        )
        plan = Plan(
            plan_id=str(payload.get("plan_id") or f"plan:{request.get('run_id', '')}"),
            goal=str(payload.get("goal") or task.get("goal") or request.get("user_message", "")),
            task_ids=task_ids,
            edges=edges,
            max_parallelism=max(1, int(payload.get("max_parallelism", 1))),
            session_id=request.get("session_id"),
            run_id=request.get("run_id"),
        )
    tasks = []
    for item in raw_tasks or []:
        if isinstance(item, Task):
            tasks.append(item)
            continue
        payload = _as_dict(item)
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


async def _publish(dependencies: RootGraphDependencies, event_type: str, state: Mapping[str, Any]) -> None:
    publisher = dependencies.event_publisher
    if publisher is None or not hasattr(publisher, "publish"):
        return
    request = state.get("request", {})
    try:
        from domain.events import ApplicationEvent, EventDurability

        event = ApplicationEvent(
            event_type=event_type,
            durability=EventDurability.DURABLE,
            session_id=str(request.get("session_id", "")),
            run_id=request.get("run_id"),
            message_id=request.get("message_id"),
            payload={"node": event_type},
        )
        await call(publisher.publish, event)
    except Exception:
        # Observability must never change agent behavior.
        return


def _node(name: str, handler: Callable[[RootGraphState], Any], dependencies: RootGraphDependencies):
    async def wrapped(state: RootGraphState) -> dict[str, Any]:
        await _publish(dependencies, "node.started", state)
        try:
            value = await call(handler, state)
        except Exception as exc:
            await _publish(dependencies, "node.failed", state)
            return {"status": "failed", "error": {"code": "node_failed", "node": name, "message": str(exc) or type(exc).__name__}, "next_route": "assemble_final_response"}
        await _publish(dependencies, "node.completed", state)
        return value or {}

    wrapped.__name__ = name
    return wrapped


def _route_after_understanding(state: RootGraphState) -> str:
    understanding = state.get("understanding", {})
    return "clarification_response" if understanding.get("clarification_question") else "plan_context"


def _route_after_agent(state: RootGraphState) -> str:
    route = state.get("next_route") or state.get("execution", {}).get("route")
    if route in {"materialize_execution_plan", "plan_requested", "plan"}:
        return "materialize_execution_plan"
    return "post_process_and_build_result"


def _route_after_prepare(state: RootGraphState) -> str:
    if state.get("resume_value"):
        return "project_plan_result"
    return "understand_task"


def _route_after_wait(state: RootGraphState) -> str:
    return "project_plan_result" if state.get("waiting") is False else END


def build_root_graph(
    handle_request: Callable[..., Any] | None = None,
    validate_plan_submission: Callable[..., Any] | None = None,
    persist_plan: Callable[..., Any] | None = None,
    load_results: Callable[..., Any] | None = None,
    finalize_response: Callable[..., Any] | None = None,
    checkpointer: Any = None,
    *,
    dependencies: RootGraphDependencies | None = None,
) -> Any:
    """Build the Root Graph described by the target architecture."""
    if isinstance(handle_request, RootGraphDependencies) and dependencies is None:
        dependencies = handle_request
        handle_request = None
    deps = dependencies or RootGraphDependencies()
    if handle_request is not None:
        deps.request_handler = handle_request
    if persist_plan is not None:
        deps.persist_plan = persist_plan
    if load_results is not None:
        deps.plan_loader = load_results
    if finalize_response is not None:
        deps.finalizer = finalize_response
    if validate_plan_submission is not None:
        deps.validate_plan = validate_plan_submission

    async def prepare_request(state: RootGraphState) -> dict[str, Any]:
        request = dict(state.get("request", {}))
        request.setdefault("session_id", "")
        request.setdefault("run_id", "")
        request.setdefault("user_message", "")
        request["attachment_ids"] = list(dict.fromkeys(request.get("attachment_ids", [])))
        if deps.request_handler is not None and not state.get("resume_value"):
            update = await call(deps.request_handler, request=request, state=state)
            if isinstance(update, Mapping):
                request.update(json_value(update.get("request", update)))
                extra = {key: json_value(value) for key, value in update.items() if key != "request"}
            else:
                extra = {}
        else:
            extra = {}
        if deps.history_loader is not None and "history" not in request:
            request["history"] = json_value(await call(deps.history_loader, session_id=request["session_id"], request=request))
        if deps.attachment_loader is not None and request["attachment_ids"] and "attachment_refs" not in request:
            request["attachment_refs"] = json_value(await call(deps.attachment_loader, session_id=request["session_id"], attachment_ids=request["attachment_ids"], request=request))
        return {"request": json_value(request), **extra}

    async def understand_task(state: RootGraphState) -> dict[str, Any]:
        request = state["request"]
        if deps.task_understanding is None:
            task = {"goal": request.get("user_message", ""), "mode": "answer", "context_requirements": ["conversation"]}
            return {"understanding": {"task_spec": task, "source": "fallback"}}
        result = await call(
            deps.task_understanding.understand,
            session_id=request.get("session_id", ""),
            user_message=request.get("user_message", ""),
            history=request.get("history", []),
            attachment_refs=request.get("attachment_refs", []),
            knowledge_bases=(await call(deps.knowledge_loader, request) if deps.knowledge_loader else []),
        )
        payload = _as_dict(result)
        payload["task_spec"] = _task_spec(payload.get("task") or payload.get("task_spec"))
        return {"understanding": payload}

    async def clarification_response(state: RootGraphState) -> dict[str, Any]:
        text = state.get("understanding", {}).get("clarification_question", "请补充任务目标。")
        return {"response": {"content": str(text), "kind": "clarification"}, "status": "completed", "next_route": "assemble_final_response"}

    async def plan_context(state: RootGraphState) -> dict[str, Any]:
        task = state.get("understanding", {}).get("task_spec", {})
        providers = list(task.get("context_requirements") or ["conversation"])
        if state.get("request", {}).get("attachment_refs") and "file" not in providers:
            providers.append("file")
        return {"context_plan": {"providers": providers, "max_items": 20, "max_tokens": 4000}}

    async def acquire_context(state: RootGraphState) -> dict[str, Any]:
        if deps.context is None:
            return {"context_bundle": {"items": [], "provider_results": []}}
        request = state["request"]
        raw_plan = state.get("context_plan", {})
        try:
            from application.runs.context import ContextPlan

            context_plan: Any = ContextPlan(
                providers=tuple(raw_plan.get("providers", ("conversation",))),
                max_items=int(raw_plan.get("max_items", 20)),
                max_tokens=int(raw_plan.get("max_tokens", 4000)),
            )
        except ImportError:
            context_plan = raw_plan
        value = await call(
            deps.context.acquire,
            session_id=request.get("session_id", ""),
            task=state.get("understanding", {}).get("task_spec", {}),
            plan=context_plan,
            run_id=request.get("run_id"),
        )
        return {"context_bundle": _as_dict(value)}

    async def prepare_harness_input(state: RootGraphState) -> dict[str, Any]:
        request = state["request"]
        return {"harness_input": {"messages": list(request.get("history", [])) + [{"role": "user", "content": request.get("user_message", "")}], "task": state.get("understanding", {}).get("task_spec", {}), "context": state.get("context_bundle", {})}}

    async def run_root_execution_loop(state: RootGraphState) -> dict[str, Any]:
        if deps.agent is None:
            return {"response": {"content": state.get("request", {}).get("user_message", "")}, "next_route": "post_process_and_build_result"}
        value = await call(deps.agent, state=state, harness_input=state.get("harness_input", {}))
        payload = _as_dict(value)
        route = payload.get("next_route") or payload.get("route")
        if payload.get("plan") is not None or route in {"plan", "plan_requested", "materialize_execution_plan"}:
            route = "materialize_execution_plan"
        else:
            route = "post_process_and_build_result"
        return {"response": payload.get("response", payload), "execution": payload.get("execution", {}), "next_route": route, "plan": payload.get("plan", {})}

    async def materialize_execution_plan(state: RootGraphState) -> dict[str, Any]:
        if deps.plan_builder is None:
            return {"plan": {}, "response": {"content": "当前运行时未配置计划执行器。", "error": {"code": "plan_builder_not_configured"}}, "next_route": "post_process_and_build_result"}
        request = state["request"]
        task = state.get("understanding", {}).get("task_spec", {})
        raw = await call(deps.plan_builder, state=state, task=task)
        plan, tasks = _coerce_plan(raw, request=request, task=task)
        if deps.validate_plan is not None:
            await call(deps.validate_plan, plan=plan, tasks=tasks)
        if deps.persist_plan is not None:
            await call(deps.persist_plan, plan=plan, tasks=tasks, state=state)
        elif deps.orchestration is not None and hasattr(deps.orchestration, "create_plan"):
            await call(deps.orchestration.create_plan, plan=plan, tasks=tasks)
        return {"plan": _plan_dict(plan, tasks), "status": "running"}

    async def wait_for_plan_completion(state: RootGraphState) -> dict[str, Any]:
        plan = state.get("plan", {})
        if deps.scheduler is not None and plan.get("plan_id"):
            await call(deps.scheduler.start_ready_tasks, plan_id=plan["plan_id"])
        if deps.plan_loader is not None:
            loaded = await call(deps.plan_loader, plan=plan, plan_id=plan.get("plan_id"), state=state)
            if isinstance(loaded, Mapping) and loaded.get("completed"):
                return {"waiting": False, "plan_results": json_value(loaded.get("results", {}))}
        return {"waiting": True, "status": "waiting_for_plan"}

    async def project_plan_result(state: RootGraphState) -> dict[str, Any]:
        plan = state.get("plan", {})
        loaded: Any = None
        if deps.plan_loader is not None:
            loaded = await call(deps.plan_loader, plan=plan, plan_id=plan.get("plan_id"), state=state)
        elif deps.plan_repository is not None and plan.get("plan_id") and hasattr(deps.plan_repository, "get_plan"):
            loaded = {"plan": await call(deps.plan_repository.get_plan, plan_id=plan["plan_id"])}
        if isinstance(loaded, Mapping):
            results = loaded.get("results", loaded.get("plan_results", {}))
        else:
            results = {}
        repository = deps.plan_repository or deps.orchestration
        if not results and repository is not None and plan.get("plan_id") and hasattr(repository, "get_results"):
            results = await call(repository.get_results, plan_id=plan["plan_id"])
        return {"plan_results": json_value(results), "response": {"plan_results": json_value(results)}, "next_route": "finalize_plan_stream"}

    async def finalize_plan_stream(state: RootGraphState) -> dict[str, Any]:
        if deps.finalizer is None:
            return {"response": {"content": _render_plan_results(state.get("plan_results", {}))}, "next_route": "assemble_final_response", "waiting": False}
        value = await call(deps.finalizer, state=state, results=state.get("plan_results", {}))
        return {"response": _as_dict(value), "next_route": "assemble_final_response", "waiting": False}

    async def post_process_and_build_result(state: RootGraphState) -> dict[str, Any]:
        response = dict(state.get("response", {}))
        if not response and state.get("error"):
            response = {"error": state["error"]}
        return {"result": response, "next_route": "assemble_final_response", "status": state.get("status", "completed")}

    async def assemble_final_response(state: RootGraphState) -> dict[str, Any]:
        response = dict(state.get("response", {}))
        result = dict(state.get("result", {}))
        if response and not result:
            result = response
        if state.get("waiting"):
            return {"result": result, "status": "waiting", "waiting": True}
        failed = bool(state.get("error") or response.get("error"))
        return {"result": result, "status": "failed" if failed else "completed", "waiting": False}

    handlers = {
        "prepare_request_and_persist_message": prepare_request,
        "understand_task": understand_task,
        "clarification_response": clarification_response,
        "plan_context": plan_context,
        "acquire_context": acquire_context,
        "prepare_harness_input": prepare_harness_input,
        "run_root_execution_loop": run_root_execution_loop,
        "materialize_execution_plan": materialize_execution_plan,
        "wait_for_plan_completion": wait_for_plan_completion,
        "project_plan_result": project_plan_result,
        "finalize_plan_stream": finalize_plan_stream,
        "post_process_and_build_result": post_process_and_build_result,
        "assemble_final_response": assemble_final_response,
    }
    builder = GraphBuilder(RootGraphState)
    wrapped = {name: _node(name, handler, deps) for name, handler in handlers.items()}
    for name, handler in wrapped.items():
        builder.add_node(name, handler)
    builder.add_edge(builder.start, "prepare_request_and_persist_message")
    builder.add_conditional("prepare_request_and_persist_message", _route_after_prepare, {"understand_task": "understand_task", "project_plan_result": "project_plan_result"})
    builder.add_conditional("understand_task", _route_after_understanding, {"clarification_response": "clarification_response", "plan_context": "plan_context"})
    builder.add_edge("clarification_response", "assemble_final_response")
    builder.add_edge("plan_context", "acquire_context")
    builder.add_edge("acquire_context", "prepare_harness_input")
    builder.add_edge("prepare_harness_input", "run_root_execution_loop")
    builder.add_conditional("run_root_execution_loop", _route_after_agent, {"post_process_and_build_result": "post_process_and_build_result", "materialize_execution_plan": "materialize_execution_plan"})
    builder.add_conditional("materialize_execution_plan", lambda state: "wait_for_plan_completion" if state.get("plan") and not state.get("error") and not state.get("response", {}).get("error") else "post_process_and_build_result", {"wait_for_plan_completion": "wait_for_plan_completion", "post_process_and_build_result": "post_process_and_build_result"})
    builder.add_conditional("wait_for_plan_completion", _route_after_wait, {"project_plan_result": "project_plan_result", END: END})
    builder.add_edge("project_plan_result", "finalize_plan_stream")
    builder.add_edge("finalize_plan_stream", "assemble_final_response")
    builder.add_edge("post_process_and_build_result", "assemble_final_response")
    builder.add_edge("assemble_final_response", builder.end)
    return builder.compile(checkpointer=checkpointer)


def build_runtime_root_graph(runtime: Any, checkpointer: Any = None) -> Any:
    """Build a graph from a runtime facade during the migration period."""
    dependencies = RootGraphDependencies(
        task_understanding=getattr(runtime, "task_understanding", getattr(runtime, "_task_understanding_service", None)),
        context=getattr(runtime, "context", getattr(runtime, "_context_service", None)),
        orchestration=getattr(runtime, "orchestration", None),
        plan_repository=getattr(runtime, "plan_repository", None),
        scheduler=getattr(runtime, "scheduler", None),
        agent=getattr(runtime, "agent", None),
        event_publisher=getattr(runtime, "event_publisher", getattr(runtime, "_event_publisher", None)),
    )
    return build_root_graph(dependencies=dependencies, checkpointer=checkpointer)


async def invoke_root_graph(
    graph: Any,
    *,
    session_id: str,
    run_id: str,
    user_message: str = "",
    message_id: str = "",
    attachment_ids: list[str] | None = None,
    resume_value: dict[str, object] | None = None,
    stop_signal: Any = None,
) -> dict[str, Any]:
    """Invoke Root with a stable checkpoint identity ``run:{run_id}``."""
    if not run_id:
        raise ValueError("run_id is required")
    state: RootGraphState = {
        "request": {"session_id": session_id, "run_id": run_id, "message_id": message_id, "user_message": user_message, "attachment_ids": list(attachment_ids or [])},
    }
    if resume_value is not None:
        state["resume_value"] = json_value(resume_value)
    config = {"configurable": {"thread_id": f"run:{run_id}"}}
    if stop_signal is not None:
        config["configurable"]["stop_signal"] = stop_signal
    return await call(graph.ainvoke, state, config=config)


def _render_plan_results(results: Mapping[str, Any]) -> str:
    if not results:
        return "计划已完成，但没有可展示的任务结果。"
    lines = []
    for task_id, value in results.items():
        payload = value if isinstance(value, Mapping) else {"output": value}
        lines.append(f"[{task_id}] {payload.get('output', payload.get('error', payload))}")
    return "\n".join(lines)


__all__ = ["RootGraphDependencies", "build_root_graph", "build_runtime_root_graph", "invoke_root_graph"]
