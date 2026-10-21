"""运行用例的只读返回模型。"""

from dataclasses import dataclass

from application.common.responses import CommandEnqueueResponse, CommandStatusResponse, RunSummaryResponse


@dataclass(frozen=True, slots=True)
class RunCancellationResponse:
    """停止命令受理后返回的运行状态。"""

    command_id: str | None
    run_id: str | None
    status: str
    already_terminal: bool


__all__ = [
    "CommandEnqueueResponse",
    "CommandStatusResponse",
    "RunCancellationResponse",
    "RunSummaryResponse",
]
