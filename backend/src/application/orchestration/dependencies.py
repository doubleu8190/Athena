"""Root 工作流装配所需的应用能力。"""

from __future__ import annotations

from dataclasses import dataclass

from application.runs.context.planner import ContextPlanner
from application.runs.context.service import ContextAcquisitionService
from application.runs.request_preparation import RunRequestPreparationService
from application.runs.task_understanding.service import TaskUnderstandingService
from application.tools.service import ToolService
from domain.events.ports import EventPort
from domain.orchestration.ports import ReviewContextRepository

from .protocols import PlanLoader, RootAgent, RootPlanCommandPort


@dataclass(slots=True)
class RootWorkflowDependencies:
    """Root 用例协调器调用的业务能力与持久化端口。"""

    task_understanding: TaskUnderstandingService
    context_acquisition: ContextAcquisitionService
    context_planner: ContextPlanner
    request_preparation: RunRequestPreparationService
    agent: RootAgent
    event_publisher: EventPort
    tool_service: ToolService
    review_context_repository: ReviewContextRepository | None = None
    orchestration_command_handler: RootPlanCommandPort | None = None
    plan_loader: PlanLoader | None = None


__all__ = ["RootWorkflowDependencies"]
