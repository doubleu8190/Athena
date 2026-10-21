from __future__ import annotations

import pytest

from application.orchestration import RootWorkflowDependencies
from application.runs.context import ContextAcquisitionService, ProviderResult
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


@pytest.mark.asyncio
async def test_root_graph_builds_context_plan_from_dependency_graph() -> None:
    observed: dict[str, object] = {}

    async def provider(**kwargs):
        observed.update(kwargs)
        return ProviderResult("conversation", "succeeded")

    graph = build_root_graph(
        dependencies=RootWorkflowDependencies(
            task_understanding=_TaskUnderstanding(UserTaskSpec(goal="hello")),
            context_acquisition=ContextAcquisitionService({"conversation": provider}),
            agent=_Agent(),
        )
    )

    result = await invoke_root_graph(
        graph, session_id="session-1", run_id="run-1", user_message="hello"
    )

    assert result["status"] == "completed"
    assert observed["plan"].providers == ["conversation"]


@pytest.mark.asyncio
async def test_root_graph_routes_nested_task_clarification() -> None:
    graph = build_root_graph(
        dependencies=RootWorkflowDependencies(
            task_understanding=_TaskUnderstanding(
                UserTaskSpec(
                    goal="choose a file",
                    mode="clarify",
                    clarification_question="请选择目标文件。",
                )
            )
        )
    )

    result = await invoke_root_graph(
        graph, session_id="session-1", run_id="run-2", user_message="处理文件"
    )

    assert result["result"] == {
        "content": "请选择目标文件。",
        "kind": "clarification",
    }
