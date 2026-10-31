"""Worker v1 注册与结果事实提交 HTTP 路由。"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from application.orchestration.worker_registration import (
    WorkerRegistrationForbidden,
    WorkerRegistrationService,
)
from application.orchestration.worker_errors import WorkerIdempotencyConflict
from application.orchestration.worker_registry import WorkerRegistryService
from application.orchestration.worker_registry import WorkerRegistrationAddressValidator
from application.orchestration.worker_result_submission import (
    WorkerResultSubmissionService,
)
from application.orchestration.worker_stop_confirmation import (
    WorkerStopConfirmationService,
)
from interfaces.http.worker_protocol import WorkerProtocolError, WorkerProtocolHttp


def build_worker_registry_router(
    registration_service_factory: Callable[[], WorkerRegistrationService],
    registry_service_factory: Callable[[], WorkerRegistryService],
    result_service_factory: Callable[[], WorkerResultSubmissionService],
    stop_service_factory: Callable[[], WorkerStopConfirmationService],
    origin_validator: WorkerRegistrationAddressValidator,
    protocol: WorkerProtocolHttp,
) -> APIRouter:
    """组装使用独立 Worker Bearer 身份的 v1 注册、结果和停止路由。

    参数：
    - `registration_service_factory`：保存注册实例和不可变能力版本的应用服务工厂。
    - `registry_service_factory`：依据执行分配记录解析 Worker 实例归属的应用服务工厂。
    - `result_service_factory`：持久化 Worker 结果的应用服务工厂。
    - `stop_service_factory`：处理物理停止确认的应用服务工厂。
    - `origin_validator`：检查注册地址 allowlist、DNS 结果和网络策略的 HTTP 客户端。
    - `protocol`：检查服务身份、协议头和原始 JSON 请求的共享边界。

    返回：没有前缀的路由器；注册路由由调用方挂载在 `/api` 下。

    异常：此函数不主动抛出业务异常。
    """
    router = APIRouter(tags=["worker-protocol-v1"])

    @router.post("/worker-instances")
    async def register_worker(request: Request) -> JSONResponse:
        """验证 Worker 注册并稳定返回实例与能力版本。

        参数：
        - `request`（Request）：含服务凭据、幂等头和 RegistrationRequest 的 HTTP 请求。

        返回：首次注册为 201；精确幂等重放为 200。

        异常：WorkerProtocolError 表示协议无效或注册身份拒绝。
        """
        principal = protocol.authenticate(request)
        raw_body = await protocol.read_json(
            request,
            schema_name="RegistrationRequest",
            id_field="registration_request_id",
        )
        registration_service = registration_service_factory()
        try:
            replay = await registration_service.find_replay(
                raw_body, service_principal=principal
            )
        except WorkerIdempotencyConflict as exc:
            raise _idempotency_conflict("IDEMPOTENCY_KEY_REUSED") from exc
        if replay is not None:
            return JSONResponse(
                status_code=200,
                content=replay.response.model_dump(mode="json", exclude_none=True),
            )
        base_url = raw_body.get("base_url")
        if not isinstance(base_url, str):
            raise WorkerProtocolError(
                "REGISTRATION_INVALID", 422, "operator_action", "base_url must be an HTTPS origin"
            )
        try:
            await origin_validator.validate_registration_origin(base_url)
        except ValueError as exc:
            raise WorkerProtocolError(
                "REGISTRATION_INVALID",
                422,
                "operator_action",
                "base_url is not available under the configured network policy",
            ) from exc
        try:
            outcome = await registration_service.register_with_replay(
                raw_body, service_principal=principal
            )
        except WorkerIdempotencyConflict as exc:
            raise _idempotency_conflict("IDEMPOTENCY_KEY_REUSED") from exc
        except WorkerRegistrationForbidden as exc:
            raise WorkerProtocolError(
                "FORBIDDEN", 403, "operator_action", "instance identity belongs to another service"
            ) from exc
        response_body = outcome.response.model_dump(mode="json", exclude_none=True)
        return JSONResponse(
            status_code=200 if outcome.replayed else 201,
            content=response_body,
        )

    @router.post("/worker-executions/{execution_id}/result")
    async def submit_worker_result(
        execution_id: str, request: Request
    ) -> Response:
        """按数据库分配身份接受 Worker 的持久结果提交。

        参数：
        - `execution_id`（str）：S1 路径中的目标 Execution ID。
        - `request`（Request）：包含结果 ID 和已结束执行结果的原始请求。

        返回：HTTP 200 与可幂等重放的 ResultReceipt。

        异常：WorkerProtocolError 表示身份、路径、Schema 或结果裁定错误。
        """
        principal = protocol.authenticate(request)
        raw_body = await protocol.read_json(
            request,
            schema_name="ResultSubmission",
            id_field="result_id",
        )
        protocol.validate_path_identifier(execution_id)
        instance_id = await registry_service_factory().resolve_execution_instance(
            principal, execution_id
        )
        if instance_id is None:
            raise WorkerProtocolError(
                "EXECUTION_NOT_FOUND", 404, "operator_action", "execution does not belong to this service"
            )
        try:
            result = await result_service_factory().submit(
                raw_body,
                instance_id=instance_id,
                execution_id=execution_id,
            )
        except WorkerIdempotencyConflict as exc:
            raise _idempotency_conflict("RESULT_ID_CONFLICT") from exc
        return JSONResponse(
            status_code=200,
            content=result.model_dump(mode="json", exclude_none=True),
        )

    @router.post("/worker-executions/{execution_id}/stop-confirmation")
    async def confirm_worker_execution_stopped(
        execution_id: str, request: Request
    ) -> Response:
        """仅接受与原 CancelCommand 实例身份匹配的物理停止确认。

        参数：
        - `execution_id`（str）：S1 路径中的目标 Execution ID。
        - `request`（Request）：包含 stop_confirmation_id 和原取消命令引用的请求。

        返回：HTTP 200 与稳定的 StopConfirmationReceipt。

        异常：WorkerProtocolError 表示身份、路径、Schema 或取消命令绑定错误。
        """
        principal = protocol.authenticate(request)
        raw_body = await protocol.read_json(
            request,
            schema_name="StopConfirmation",
            id_field="stop_confirmation_id",
            execution_id=execution_id,
        )
        command_id = raw_body.get("cancel_command_id")
        if not isinstance(command_id, str):
            raise WorkerProtocolError(
                "STOP_CONFIRMATION_CONFLICT",
                409,
                "operator_action",
                "cancel command identity does not match",
            )
        instance_id = await registry_service_factory().resolve_cancel_instance(
            principal, execution_id, str(UUID(command_id))
        )
        if instance_id is None:
            raise WorkerProtocolError(
                "STOP_CONFIRMATION_CONFLICT",
                409,
                "operator_action",
                "cancel command identity does not match",
            )
        try:
            receipt = await stop_service_factory().confirm(
                raw_body,
                instance_id=instance_id,
            )
        except WorkerIdempotencyConflict as exc:
            raise _idempotency_conflict("STOP_CONFIRMATION_CONFLICT") from exc
        return JSONResponse(
            status_code=200,
            content=receipt.model_dump(mode="json", exclude_none=True),
        )

    return router


def _idempotency_conflict(code: str) -> WorkerProtocolError:
    """将领域幂等冲突映射为 Worker v1 的稳定 HTTP 409。"""
    return WorkerProtocolError(
        code,
        409,
        "operator_action",
        "idempotency key was already used with different content",
    )


__all__ = ["build_worker_registry_router"]
