"""接收外部 Worker 顺序上报的完整 Progress。"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import JsonValue

from application.observability import (
    ObservabilityRepository,
    ProgressConflictError,
    ProgressExecutionTerminalError,
    RunObservabilityService,
)
from .worker_protocol_models import (
    WorkerProgressReceiptModel,
    WorkerProgressSubmissionModel,
)


class WorkerProgressService:
    """从认证实例和执行路径恢复归属后原子写入 Progress。"""

    def __init__(
        self,
        repository: ObservabilityRepository,
        observability: RunObservabilityService | None = None,
    ) -> None:
        self._repository = repository
        self._observability = observability

    async def submit(
        self,
        value: Mapping[str, JsonValue],
        *,
        execution_id: str,
        instance_id: str,
    ) -> WorkerProgressReceiptModel:
        """接收一条 sequence>=1 的完整 Progress。

        参数：
        - `value`：仅包含 `sequence` 与 `content` 的请求体。
        - `execution_id`：认证路由中的执行标识。
        - `instance_id`：Bearer 身份解析出的 Worker 实例。

        返回：持久化后的稳定 Progress 回执。

        异常：
        - ValueError：执行不属于当前实例或不存在。
        - ProgressExecutionTerminalError：终态后拒绝迟到 Progress。
        - ProgressConflictError：同一序号被不同内容占用。
        """
        request = WorkerProgressSubmissionModel.model_validate(dict(value))
        context = await self._repository.worker_progress_context(
            execution_id=execution_id,
            worker_id=instance_id,
        )
        if context is None:
            raise ValueError("worker_progress_execution_identity_unknown")
        entry = await self._repository.append_worker_progress(
            context,
            sequence=request.sequence,
            content=request.content.strip(),
        )
        if self._observability is not None:
            await self._observability.publish_progress(entry)
        return WorkerProgressReceiptModel(
            progress_id=entry.progress_id,
            execution_id=execution_id,
            sequence=entry.sequence,
            content=entry.content,
            received_at=entry.created_at,
        )


__all__ = [
    "ProgressConflictError",
    "ProgressExecutionTerminalError",
    "WorkerProgressService",
]
