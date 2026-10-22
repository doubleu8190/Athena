"""Application ports used by the LangGraph orchestration adapter.

The graph adapter depends on these contracts, while the concrete workflows
remain application implementations assembled by bootstrap.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from application.orchestration.scheduler import WorkerSchedulerService
from application.orchestration.service import OrchestrationService
from application.runs.context.planner import ContextPlanner
from application.runs.context.service import ContextAcquisitionService
from application.runs.request_preparation import (
    RunRequestPreparationService,
)
from application.runs.task_understanding.service import TaskUnderstandingService
from application.approval.service import ApprovalService
from application.tools.service import ToolService
from domain.events.ports import EventPort
from domain.orchestration import (
    CommandReceipt,
    OrchestrationCommand,
    Plan,
    ReviewDecision,
    Task,
)
from domain.orchestration.ports import PlanRepository, ReviewContextRepository

if TYPE_CHECKING:
    from application.orchestration.langgraph.runtime.root_agent import (
        ConfiguredRootAgent,
    )


@dataclass(slots=True)
class RootWorkflowDependencies:
    """Application capabilities used by the Root workflow."""

    task_understanding: TaskUnderstandingService
    context_acquisition: ContextAcquisitionService
    context_planner: ContextPlanner
    orchestration: OrchestrationService
    plan_repository: PlanRepository
    worker_scheduler: WorkerSchedulerService
    request_preparation: RunRequestPreparationService
    agent: ConfiguredRootAgent
    event_publisher: EventPort
    tool_service: ToolService
    approval_service: ApprovalService
    review_context_repository: ReviewContextRepository | None = None
    orchestration_command_handler: OrchestrationCommandHandlerPort | None = None
    plan_builder: Callable[..., Any] | None = None
    plan_loader: Callable[..., Any] | None = None
    finalizer: Callable[..., Any] | None = None
    persist_plan: Callable[..., Any] | None = None
    validate_plan: Callable[..., Any] | None = None


class OrchestrationCommandHandlerPort(Protocol):
    """Application command boundary used by Root plan submission and review."""

    async def submit_plan(
        self,
        command: OrchestrationCommand,
        plan: Plan,
        tasks: list[Task],
        *,
        legal_tools: Iterable[str] | None = None,
    ) -> CommandReceipt: ...

    async def apply_review_decision(
        self, command: OrchestrationCommand, decision: ReviewDecision
    ) -> CommandReceipt: ...

    async def load_review_decision(
        self,
        *,
        scope_key: str,
        idempotency_key: str,
        plan_id: str,
        plan_version: int,
        round_id: str,
    ) -> ReviewDecision | None: ...


class RootWorkflowPort(Protocol):
    async def prepare_request(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def understand_task(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def clarification_response(
        self, state: Mapping[str, Any]
    ) -> dict[str, Any]: ...
    async def plan_context(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def acquire_context(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def prepare_harness_input(
        self, state: Mapping[str, Any]
    ) -> dict[str, Any]: ...
    async def run_root_execution_loop(
        self, state: Mapping[str, Any]
    ) -> dict[str, Any]: ...
    async def materialize_execution_plan(
        self, state: Mapping[str, Any]
    ) -> dict[str, Any]: ...
    async def wait_for_plan_completion(
        self, state: Mapping[str, Any]
    ) -> dict[str, Any]: ...
    async def review_completed_round(
        self, state: Mapping[str, Any]
    ) -> dict[str, Any]: ...
    async def project_plan_result(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def finalize_plan_stream(
        self, state: Mapping[str, Any]
    ) -> dict[str, Any]: ...
    async def post_process_and_build_result(
        self, state: Mapping[str, Any]
    ) -> dict[str, Any]: ...
    async def assemble_final_response(
        self, state: Mapping[str, Any]
    ) -> dict[str, Any]: ...


class WorkerWorkflowPort(Protocol):
    async def prepare(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def run(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def approval_gate(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def execute_tool_batch(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def complete_tool_approval(
        self, state: Mapping[str, Any], decision: Any
    ) -> dict[str, Any]: ...
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
    "OrchestrationCommandHandlerPort",
    "RootWorkflowPort",
    "WorkerWorkflowPort",
    "make_root_workflow",
]
