"""Task Understanding 的领域契约与服务。"""

from .contracts import (
    ContextBundle,
    ContextItem,
    ContextPlan,
    ContextRequirement,
    ContentType,
    OutputFormat,
    OutputSpec,
    OutputTarget,
    ProviderResult,
    TaskMode,
    TaskQueryHints,
    TaskDomain,
    TaskSlots,
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
    "TaskMode",
    "TaskDomain",
    "TaskSlots",
    "ContentType",
    "OutputFormat",
    "OutputTarget",
    "OutputSpec",
    "UserTaskSpec",
    "build_fast_path_task",
    "TaskUnderstandingService",
]
