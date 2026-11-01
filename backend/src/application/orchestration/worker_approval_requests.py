"""Worker 审批申请的协议校验和应用协调。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

import rfc8785
from pydantic import JsonValue

from domain.orchestration.worker_entities import WorkerApprovalIntent, WorkerApprovalItem

from .worker_protocol_models import WorkerApprovalReceiptModel, WorkerApprovalRequestModel
from .worker_protocol_validation import validate_worker_request


class WorkerApprovalRequestService:
    """把 Worker 审批申请转换为持久化意图并返回稳定回执。"""

    def __init__(self, repository: "WorkerApprovalRequestRepository") -> None:
        """保存支持 `submit_approval_request` 的编排仓储。"""
        self._repository = repository

    async def submit(
        self,
        raw_body: Mapping[str, JsonValue],
        *,
        instance_id: str,
    ) -> WorkerApprovalReceiptModel:
        """校验并原子保存单项或批量审批申请。"""
        document = dict(raw_body)
        request_hash = hashlib.sha256(rfc8785.dumps(document)).hexdigest()
        validate_worker_request("ApprovalRequest", document)
        request = WorkerApprovalRequestModel.model_validate(document)
        intent = WorkerApprovalIntent(
            approval_request_id=str(request.approval_request_id),
            pause_id=str(request.pause_id),
            run_id=request.run_id,
            plan_id=request.plan_id,
            task_id=request.task_id,
            execution_id=request.execution_id,
            generation=request.generation,
            cancellation_epoch=request.cancellation_epoch,
            items=tuple(
                WorkerApprovalItem(
                    item_id=str(item.item_id),
                    tool_name=item.tool_name,
                    summary=item.summary,
                    risk_level=item.risk_level,
                    arguments=item.arguments,
                    arguments_sha256=item.arguments_sha256,
                )
                for item in request.items
            ),
            request_hash=request_hash,
        )
        response = await self._repository.submit_approval_request(
            intent, instance_id=instance_id
        )
        return WorkerApprovalReceiptModel.model_validate(response)


__all__ = ["WorkerApprovalRequestService"]


class WorkerApprovalRequestRepository(Protocol):
    """定义审批申请应用服务依赖的持久化边界。"""

    async def submit_approval_request(
        self, intent: WorkerApprovalIntent, *, instance_id: str
    ) -> dict[str, JsonValue]:
        """按实例身份幂等保存审批申请并暂停对应执行。"""
