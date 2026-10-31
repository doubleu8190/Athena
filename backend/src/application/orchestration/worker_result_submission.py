"""验证 Worker 结果请求并调用 Orchestrator 原子裁定用例。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

import rfc8785
from pydantic import JsonValue

from domain.orchestration.ports import WorkerOrchestrationRepository
from domain.orchestration.worker_entities import (
    WorkerExecutionError,
    WorkerResultSubmission,
)

from .worker_protocol_models import (
    WorkerResultReceiptModel,
    WorkerResultSubmissionModel,
)
from .worker_protocol_validation import validate_worker_request


class WorkerResultSubmissionService:
    """转换协议请求与领域结果，并返回稳定协议回执。"""

    def __init__(self, repository: WorkerOrchestrationRepository) -> None:
        """初始化结果提交用例。

        参数：
        - `repository`（WorkerOrchestrationRepository）：负责锁、幂等回执和状态原子转换的仓储端口。

        返回：无。

        异常：此构造函数不主动抛出业务异常。
        """
        self._repository = repository

    async def submit(
        self,
        raw_body: Mapping[str, JsonValue],
        *,
        instance_id: str,
        execution_id: str,
    ) -> WorkerResultReceiptModel:
        """提交通过协议模型校验的 Worker 结果。

        参数：
        - `raw_body`（Mapping[str, JsonValue]）：原始 JSON 请求体；先计算 JCS 摘要，再验证协议字段。
        - `instance_id`（str）：认证身份解析出的注册实例 ID；约束：不得来自请求正文。
        - `execution_id`（str）：HTTP path 中的执行标识；约束：不得来自请求正文。

        返回：符合 v1 Schema 的持久化裁定回执。

        异常：仓储的身份冲突、幂等冲突和数据库错误原样传播。
        """
        raw_document = dict(raw_body)
        request_hash = hashlib.sha256(rfc8785.dumps(raw_document)).hexdigest()
        validate_worker_request("ResultSubmission", raw_document)
        request = WorkerResultSubmissionModel.model_validate(raw_document)
        error = (
            None
            if request.error is None
            else WorkerExecutionError(
                code=request.error.code,
                message=request.error.message,
                retryable=request.error.retryable,
                details=request.error.details,
            )
        )
        submission = WorkerResultSubmission(
            execution_id=execution_id,
            instance_id=instance_id,
            generation=request.generation,
            result_id=str(request.result_id),
            status=request.status,
            output=request.output,
            error=error,
            request_hash=request_hash,
        )
        receipt = await self._repository.record_result(submission=submission)
        return WorkerResultReceiptModel.model_validate(
            {
                "result_id": receipt.result_id,
                "execution_id": receipt.execution_id,
                "generation": receipt.generation,
                "disposition": receipt.disposition,
                "reason": receipt.reason,
                "next_execution_id": receipt.next_execution_id,
                "received_at": receipt.received_at,
            }
        )

__all__ = ["WorkerResultSubmissionService"]
