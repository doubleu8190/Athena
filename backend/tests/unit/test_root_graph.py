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
        plan_repository=SimpleNamespace(),
        request_preparation=_RequestPreparation(),
        agent=_Agent(),
        event_publisher=events,
        tool_service=SimpleNamespace(),
        approval_service=SimpleNamespace(),
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
