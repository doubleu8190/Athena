"""Application ports used by the LangGraph orchestration adapter.

The graph adapter depends on these contracts, while the concrete workflows
remain application implementations assembled by bootstrap.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(slots=True)
class RootWorkflowDependencies:
    """Application capabilities used by the Root workflow."""

    task_understanding: Any | None = None
    context: Any | None = None
    context_planner: Any | None = None
    context_plan_factory: Callable[[Mapping[str, Any]], Any] | None = None
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


class RootWorkflowPort(Protocol):
    async def prepare_request(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def understand_task(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def clarification_response(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def plan_context(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def acquire_context(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def prepare_harness_input(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def run_root_execution_loop(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def materialize_execution_plan(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def wait_for_plan_completion(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def project_plan_result(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def finalize_plan_stream(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def post_process_and_build_result(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def assemble_final_response(self, state: Mapping[str, Any]) -> dict[str, Any]: ...


class WorkerWorkflowPort(Protocol):
    async def prepare(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def run(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def approval_gate(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def execute_tool_batch(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def resume_node(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def finalize(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    def route(self, state: Mapping[str, Any]) -> str: ...
    def route_after_gate(self, state: Mapping[str, Any]) -> str: ...
    def route_after_tools(self, state: Mapping[str, Any]) -> str: ...


def make_root_workflow(dependencies: RootWorkflowDependencies) -> RootWorkflowPort:
    """Compatibility factory for callers migrating to explicit injection."""
    from .root_workflow import RootWorkflow

    return RootWorkflow(dependencies)


__all__ = [
    "RootWorkflowDependencies",
    "RootWorkflowPort",
    "WorkerWorkflowPort",
    "make_root_workflow",
]
