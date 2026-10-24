"""Root 工作流装配所需的应用能力。"""

from __future__ import annotations

from dataclasses import dataclass

from application.runs.context.planner import ContextPlanner
from application.runs.context.service import ContextAcquisitionService
from application.runs.request_preparation import RunRequestPreparationService
from application.tools.service import ToolService
from .answer_generation import AnswerGenerationService
from domain.events.ports import EventPort
from domain.orchestration.ports import ReviewContextRepository

from .protocols import PlanLoader, RootAgent, RootPlanCommandPort


@dataclass(slots=True)
class RootWorkflowDependencies:
    """Root 用例协调器调用的业务能力与持久化端口。"""

    context_acquisition: ContextAcquisitionService
    context_planner: ContextPlanner
    request_preparation: RunRequestPreparationService
    agent: RootAgent
    event_publisher: EventPort
    answer_generation: AnswerGenerationService
    tool_service: ToolService
    available_context_providers: tuple[str, ...] = ()
    root_max_turns: int = 4
    context_prefetch_timeout_seconds: float = 3.0
    review_context_repository: ReviewContextRepository | None = None
    orchestration_command_handler: RootPlanCommandPort | None = None
    plan_loader: PlanLoader | None = None


__all__ = ["RootWorkflowDependencies"]
