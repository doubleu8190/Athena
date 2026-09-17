"""Task Understanding 的领域契约与服务。"""

from .contracts import (
    ContextBundle,
    ContextItem,
    ContextPlan,
    ContextRequirement,
    ProviderResult,
    TaskQueryHints,
    TaskType,
    UserTaskSpec,
)
from .fast_path import build_fast_path_task
from .service import TaskUnderstandingService

__all__ = [
    "ContextBundle",
    "ContextItem",
    "ContextPlan",
    "ContextRequirement",
    "ProviderResult",
    "TaskQueryHints",
    "TaskType",
    "UserTaskSpec",
    "build_fast_path_task",
    "TaskUnderstandingService",
]
