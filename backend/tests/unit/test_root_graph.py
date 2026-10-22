from __future__ import annotations

from types import SimpleNamespace

import pytest

from application.orchestration import RootWorkflow, RootWorkflowDependencies
from application.runs.context import ContextAcquisitionService, ProviderResult
from application.runs.context import ContextPlanner
from application.runs.task_understanding import TaskUnderstandingResult, UserTaskSpec
from application.orchestration.langgraph import (
    build_root_graph,
    invoke_root_graph,
)


class _TaskUnderstanding:
    def __init__(self, task: UserTaskSpec) -> None:
        self._task = task

    async def understand(self, **_kwargs):
        return TaskUnderstandingResult(self._task, "test")


class _Agent:
    async def __call__(self, *, harness_input, **_kwargs):
        return {"response": {"content": "ok"}}


class _RequestPreparation:
    async def prepare(self, *, request, state):
        return {"request": request}

    async def load_history(self, **_kwargs):
        return []

    async def load_attachments(self, **_kwargs):
        return []

    async def load_knowledge_bases(self, _request):
        return []


class _Provider:
    def __init__(self, handler):
        self.handler = handler

    async def retrieve_context(self, **kwargs):
        return await self.handler(**kwargs)


class _Events:
    async def publish(self, _event):
        return None


class _TraceRepository:
    async def record_context(self, **_kwargs):
        return "retrieval-1"


def _dependencies(task_understanding, *, provider=None):
    events = _Events()
    return RootWorkflowDependencies(
        task_understanding=task_understanding,
        context_acquisition=ContextAcquisitionService(
            {"conversation": _Provider(provider)} if provider else {},
            trace_repository=_TraceRepository(),
            event_publisher=events,
        ),
        context_planner=ContextPlanner(),
        request_preparation=_RequestPreparation(),
        agent=_Agent(),
        event_publisher=events,
        tool_service=SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_root_graph_builds_context_plan_from_dependency_graph() -> None:
    observed: dict[str, object] = {}

    async def provider(**kwargs):
        observed.update(kwargs)
        return ProviderResult("conversation", "succeeded")

    dependencies = _dependencies(
        _TaskUnderstanding(UserTaskSpec(goal="hello")), provider=provider
    )
    graph = build_root_graph(
        workflow=RootWorkflow(dependencies), dependencies=dependencies
    )

    result = await invoke_root_graph(
        graph, session_id="session-1", run_id="run-1", user_message="hello"
    )

    assert result["status"] == "completed"
    assert observed["plan"].providers == ["conversation"]


def test_root_graph_merges_terminal_steps_and_keeps_plan_wait_boundary() -> None:
    dependencies = _dependencies(
        _TaskUnderstanding(UserTaskSpec(goal="hello"))
    )
    graph = build_root_graph(
        workflow=RootWorkflow(dependencies), dependencies=dependencies
    )

    node_names = set(graph.get_graph().nodes)

    assert "wait_for_plan_completion" in node_names
    assert "build_plan_response" in node_names
    assert "assemble_final_response" in node_names
    assert not {
        "project_plan_result",
        "finalize_plan_stream",
        "post_process_and_build_result",
    } & node_names


@pytest.mark.asyncio
async def test_root_workflow_loads_plan_results_once_and_renders_response() -> None:
    dependencies = _dependencies(
        _TaskUnderstanding(UserTaskSpec(goal="hello"))
    )
    load_calls = 0

    async def load_plan(*, plan_id):
        nonlocal load_calls
        load_calls += 1
        return {
            "completed": True,
            "results": {"task-1": {"output": "done"}},
        }

    dependencies.plan_loader = load_plan
    workflow = RootWorkflow(dependencies)
    state = {
        "request": {"run_id": "run-1"},
        "plan": {
            "plan_id": "plan-1",
            "tasks": [{"task_id": "task-1", "title": "Task one"}],
        },
    }

    wait_result = await workflow.wait_for_plan_completion(state)
    response_result = await workflow.build_plan_response(
        {**state, **wait_result}
    )
    final_result = await workflow.assemble_final_response(
        {**state, **response_result}
    )

    assert load_calls == 1
    assert response_result == {
        "response": {"content": "Task one：done"},
        "waiting": False,
    }
    assert final_result == {
        "result": {"content": "Task one：done"},
        "status": "completed",
        "waiting": False,
    }


@pytest.mark.asyncio
async def test_root_workflow_assembles_node_error_as_failed_result() -> None:
    workflow = RootWorkflow(
        _dependencies(_TaskUnderstanding(UserTaskSpec(goal="hello")))
    )

    result = await workflow.assemble_final_response(
        {"error": {"code": "node_failed", "message": "failed"}}
    )

    assert result == {
        "result": {"error": {"code": "node_failed", "message": "failed"}},
        "status": "failed",
        "waiting": False,
    }


@pytest.mark.asyncio
async def test_root_graph_routes_nested_task_clarification() -> None:
    dependencies = _dependencies(
        _TaskUnderstanding(
            UserTaskSpec(
                goal="choose a file",
                mode="clarify",
                clarification_question="请选择目标文件。",
            )
        )
    )
    graph = build_root_graph(
        workflow=RootWorkflow(dependencies), dependencies=dependencies
    )

    result = await invoke_root_graph(
        graph, session_id="session-1", run_id="run-2", user_message="处理文件"
    )

    assert result["result"] == {
        "content": "请选择目标文件。",
        "kind": "clarification",
    }


@pytest.mark.asyncio
async def test_root_graph_rejects_legacy_state_based_resume() -> None:
    with pytest.raises(RuntimeError, match="root_resume_value_type_invalid"):
        await invoke_root_graph(
            None,
            session_id="session-1",
            run_id="run-legacy-resume",
            user_message="",
            resume_value={"tool_call_id": "old-root-approval"},
        )
