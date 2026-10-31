"""验证 Worker 停止确认并持久化稳定容量裁定。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

import rfc8785
from pydantic import JsonValue

from domain.orchestration.ports import WorkerOrchestrationRepository
from domain.orchestration.worker_entities import WorkerStopConfirmation

from .worker_protocol_models import (
    WorkerStopConfirmationModel,
    WorkerStopConfirmationReceiptModel,
)
from .worker_protocol_validation import validate_worker_request


class WorkerStopConfirmationService:
    """保留请求原始 JSON 语义后调用停止确认事务。"""

    def __init__(self, repository: WorkerOrchestrationRepository) -> None:
        """初始化停止确认用例。

        参数：
        - `repository`：负责身份绑定、幂等回执和容量释放事务的仓储端口。

        返回：无。

        异常：此构造函数不主动抛出业务异常。
        """
        self._repository = repository

    async def confirm(
        self,
        raw_body: Mapping[str, JsonValue],
        *,
        instance_id: str,
    ) -> WorkerStopConfirmationReceiptModel:
        """确认指定 Worker 实例已停止某个执行代次。

        参数：
        - `raw_body`：Worker v1 原始 JSON 请求体；摘要在 UUID/时间字段解析前计算。
        - `instance_id`：由认证身份解析出的 Worker 实例标识。

        返回：可幂等重放的 Worker v1 停止确认回执。

        异常：协议错误、身份冲突、幂等冲突和数据库错误原样传播。
        """
        raw_document = dict(raw_body)
        body_hash = hashlib.sha256(rfc8785.dumps(raw_document)).hexdigest()
        validate_worker_request("StopConfirmation", raw_document)
        request = WorkerStopConfirmationModel.model_validate(raw_document)
        receipt = await self._repository.confirm_stopped(
            WorkerStopConfirmation(
                instance_id=instance_id,
                stop_confirmation_id=str(request.stop_confirmation_id),
                cancel_command_id=str(request.cancel_command_id),
                execution_id=request.execution_id,
                generation=request.generation,
                cancellation_epoch=request.cancellation_epoch,
                reason=request.reason,
                stopped_at=request.stopped_at,
                request_hash=body_hash,
            )
        )
        return WorkerStopConfirmationReceiptModel.model_validate(
            {
                "stop_confirmation_id": receipt.stop_confirmation_id,
                "cancel_command_id": receipt.cancel_command_id,
                "execution_id": receipt.execution_id,
                "generation": receipt.generation,
                "cancellation_epoch": receipt.cancellation_epoch,
                "reason": receipt.reason,
                "disposition": receipt.disposition,
                "task_status_unchanged": True,
                "received_at": receipt.received_at,
            }
        )


__all__ = ["WorkerStopConfirmationService"]
