"""持久领取任务命令并通过受限 HTTPS 发送给远程 Worker。"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from uuid import uuid4

import httpx
from jsonschema.exceptions import ValidationError

from application.orchestration.worker_protocol_models import (
    WorkerCommandReceiptModel,
    WorkerHttpErrorResponseModel,
    WorkerTaskCommandModel,
)
from application.orchestration.worker_resources import WorkerResourceService
from application.orchestration.worker_protocol_validation import validate_worker_request
from domain.orchestration.worker_entities import WorkerDispatchLease, WorkerDispatchStart
from infrastructure.integrations.worker_http import (
    PinnedWorkerHttpClient,
    WorkerOriginRejected,
    WorkerTaskResponseInvalid,
)
from infrastructure.persistence.postgres.repositories.worker_orchestration_repository import (
    PostgresWorkerOrchestrationRepository,
)
from shared.logging import get_logger

logger = get_logger(__name__)

_RETRY_STATUS_BY_CODE = {
    "INSTANCE_NOT_READY": 503,
    "SERVER_OVERLOADED": 503,
    "RATE_LIMITED": 429,
    "TEMPORARY_UNAVAILABLE": 503,
    "INTERNAL_ERROR": 500,
}
_PERMANENT_STATUS_BY_CODE = {
    "AUTHENTICATION_FAILED": 401,
    "FORBIDDEN": 403,
    "PROTOCOL_VERSION_UNSUPPORTED": 426,
    "MALFORMED_REQUEST": 400,
    "RESOURCE_DIGEST_MISMATCH": 400,
    "UNSUPPORTED_MEDIA_TYPE": 415,
    "PAYLOAD_TOO_LARGE": 413,
    "VALIDATION_ERROR": 422,
    "REGISTRATION_INVALID": 422,
    "TASK_INPUT_INVALID": 422,
    "APPROVAL_REQUEST_INVALID": 422,
    "UNKNOWN_CONTRACT": 422,
    "IDEMPOTENCY_KEY_REUSED": 409,
    "COMMAND_ID_CONFLICT": 409,
    "RESULT_ID_CONFLICT": 409,
    "APPROVAL_ID_CONFLICT": 409,
    "STOP_CONFIRMATION_CONFLICT": 409,
    "RESOURCE_ID_CONFLICT": 409,
    "EXECUTION_NOT_FOUND": 404,
    "EXECUTION_SUPERSEDED": 409,
    "TASK_CANCELLED": 409,
}
_PERMANENT_DIRECTIVE_BY_CODE = {
    "TASK_INPUT_INVALID": "root_replan",
    "UNKNOWN_CONTRACT": "root_replan",
    "EXECUTION_SUPERSEDED": "stop",
    "TASK_CANCELLED": "stop",
    **{
        code: "operator_action"
        for code in _PERMANENT_STATUS_BY_CODE
        if code
        not in {
            "TASK_INPUT_INVALID",
            "UNKNOWN_CONTRACT",
            "EXECUTION_SUPERSEDED",
            "TASK_CANCELLED",
        }
    },
}


class WorkerTaskDispatcher:
    """在有限并发槽位内调度持久命令并保留远端收件不确定性。"""

    def __init__(
        self,
        repository: PostgresWorkerOrchestrationRepository,
        resource_service: WorkerResourceService,
        http_client: PinnedWorkerHttpClient,
        *,
        worker_id: str,
        max_concurrency: int,
        request_timeout_seconds: float,
        max_response_bytes: int,
        max_retries: int,
        retry_initial_seconds: float,
        retry_max_seconds: float,
        retry_jitter_ratio: float = 0.2,
        poll_interval_seconds: float = 0.25,
        id_factory: Callable[[], str] | None = None,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        """验证设置并准备持久命令调度循环。

        参数：
        - `repository`（PostgresWorkerOrchestrationRepository）：持久化命令及容量事实。
        - `resource_service`（WorkerResourceService）：签发当前执行的资源授权。
        - `http_client`（PinnedWorkerHttpClient）：执行 HTTPS、TLS、DNS/IP pinning 的客户端。
        - `worker_id`（str）：当前进程唯一标识，用于数据库命令租约所有权。
        - `max_concurrency`（int）：单进程最多同时进行的命令数。
        - `request_timeout_seconds`（float）：单次发送等待上限。
        - `max_response_bytes`（int）：Worker HTTP JSON 响应的字节上限。
        - `max_retries`（int）：同一命令最多进行的额外传输重试次数。
        - `retry_initial_seconds`（float）：指数退避首间隔。
        - `retry_max_seconds`（float）：指数退避与远端 Retry-After 的上限。
        - `retry_jitter_ratio`（float）：相对退避基准值的随机比例，范围为 0–1。
        - `poll_interval_seconds`（float）：无可运行命令时的队列轮询间隔。
        - `id_factory`（Callable[[], str] | None）：默认使用 UUID 生成进程租约 ID。
        - `jitter`（Callable[[float, float], float]）：用于测试注入的抖动函数。

        返回：无。

        异常：ValueError 表示进程身份、并发数或时间/重试配置无效。
        """
        if (
            not worker_id.strip()
            or max_concurrency < 1
            or request_timeout_seconds <= 0
            or max_response_bytes < 1
            or max_retries < 0
            or retry_initial_seconds <= 0
            or retry_max_seconds < retry_initial_seconds
            or not 0 <= retry_jitter_ratio <= 1
            or poll_interval_seconds <= 0
        ):
            raise ValueError("worker_dispatch_configuration_invalid")
        self._repository = repository
        self._resource_service = resource_service
        self._http = http_client
        self._worker_id = worker_id
        self._max_concurrency = max_concurrency
        self._request_timeout = request_timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._max_retries = max_retries
        self._retry_initial = retry_initial_seconds
        self._retry_max = retry_max_seconds
        self._jitter_ratio = retry_jitter_ratio
        self._poll_interval = poll_interval_seconds
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self._jitter = jitter
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._inflight: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        """启动唯一调度循环；重复调用保持幂等。"""
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stop.clear()
        self._loop_task = asyncio.create_task(
            self.run(), name=f"athena-worker-task-dispatcher:{self._worker_id}"
        )

    async def stop(self) -> None:
        """停止领取并取消当前网络操作，让持久租约到期后可恢复。"""
        self._stop.set()
        if self._loop_task is not None:
            await self._loop_task
            self._loop_task = None
        pending = tuple(self._inflight)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def run(self) -> None:
        """占有本地并发槽后才领取命令，避免租约在等待 HTTP 槽时消耗。"""
        while not self._stop.is_set():
            claimed = False
            while not self._stop.is_set() and len(self._inflight) < self._max_concurrency:
                try:
                    lease = await self._repository.claim_dispatch_command(
                        owner_id=self._worker_id,
                        lease_seconds=self._lease_seconds,
                    )
                    if lease is None:
                        break
                    task = asyncio.create_task(
                        self._dispatch(lease),
                        name=f"athena-worker-task-command:{lease.command_id}",
                    )
                    self._inflight.add(task)
                    task.add_done_callback(self._on_done)
                    claimed = True
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.error("worker_task_command_claim_failed", exc_info=True)
                    break
            if not claimed or len(self._inflight) < self._max_concurrency:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
                except asyncio.TimeoutError:
                    pass

    @property
    def _lease_seconds(self) -> float:
        """确保租约覆盖一次 HTTP timeout 与短暂持久化收尾。"""
        return max(self._request_timeout + 10.0, 15.0)

    def _on_done(self, task: asyncio.Task[None]) -> None:
        """回收完成的本地任务并记录未预期异常。"""
        self._inflight.discard(task)
        error = None if task.cancelled() else task.exception()
        if error is not None:
            logger.error(
                "worker_task_dispatch_failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _dispatch(self, lease: WorkerDispatchLease) -> None:
        """完成分配、Grant 构造、命令冻结和一次有界 HTTP 发送。"""
        try:
            start = await self._repository.start_dispatch(
                lease,
                lease_seconds=self._lease_seconds,
                assignment_retry_seconds=self._poll_interval,
            )
            if start is None:
                return
            send_intent_committed = False
            if "assignment_required" in start.command:
                grant_context = (
                    await self._resource_service.prepare_execution_grant_context(
                        start.execution_id
                    )
                )

                def build_command(final_start: WorkerDispatchStart) -> dict[str, object]:
                    if final_start.deadline_at is None:
                        raise ValueError("worker_dispatch_deadline_missing")
                    grant = self._resource_service.issue_prepared_execution_grant(
                        grant_context, final_start
                    )
                    draft = {
                        key: value
                        for key, value in final_start.command.items()
                        if key != "assignment_required"
                    }
                    command = WorkerTaskCommandModel.model_validate(
                        {
                            **draft,
                            "execution_grant": {
                                "token": grant,
                                "expires_at": final_start.deadline_at,
                            },
                        }
                    )
                    return command.model_dump(mode="json", exclude_none=True)

                start = await self._repository.prepare_first_dispatch_send(
                    lease,
                    lease_seconds=self._lease_seconds,
                    command_builder=build_command,
                )
                if start is None:
                    return
                command_body = start.command
                send_intent_committed = True
            else:
                # 租约接管或同命令重投必须复用已冻结的 Grant 与字节语义。
                if start.deadline_at is None:
                    raise ValueError("worker_dispatch_deadline_missing")
                command_body = WorkerTaskCommandModel.model_validate(
                    start.command
                ).model_dump(mode="json", exclude_none=True)
                frozen_body, _ = await self._repository.freeze_dispatch_body(
                    lease, command_body
                )
                command_body = frozen_body
            start = type(start)(
                command_id=start.command_id,
                execution_id=start.execution_id,
                instance_id=start.instance_id,
                instance_key=start.instance_key,
                service_principal=start.service_principal,
                base_url=start.base_url,
                command=command_body,
                request_hash=start.request_hash,
                started_at=start.started_at,
                deadline_at=start.deadline_at,
            )
            if not send_intent_committed:
                if not await self._repository.prepare_dispatch_send(
                    lease, lease_seconds=self._lease_seconds
                ):
                    return
            response = await self._http.send_task(
                start,
                max_response_bytes=self._max_response_bytes,
                timeout_seconds=self._request_timeout,
            )
            await self._handle_response(
                lease,
                response.status_code,
                response.body,
                response.retry_after_seconds,
            )
        except asyncio.CancelledError:
            raise
        except WorkerOriginRejected as exc:
            await self._repository.fail_dispatch_command(
                lease,
                error_code="WORKER_ORIGIN_REJECTED",
                message=str(exc),
                root_replan=False,
            )
        except httpx.HTTPError:
            await self._retry_or_wait(lease)
        except WorkerTaskResponseInvalid:
            # Worker 已经返回响应，无法解析的回执仍代表收件结果不确定。
            await self._retry_or_wait(lease)
        except ValueError as exc:
            if str(exc) in {"worker_dispatch_lease_lost", "worker_dispatch_command_body_immutable"}:
                logger.warning("worker_dispatch_fencing_rejected", extra={"command_id": lease.command_id})
                return
            if str(exc) == "worker_execution_grant_expiry_not_future":
                await self._repository.await_dispatch_result(lease)
                return
            # Grant/DTO 引用错误意味着该分配无法合法发送，收口后等待 Root 修订。
            await self._repository.fail_dispatch_command(
                lease,
                error_code="TASK_COMMAND_INVALID",
                message=str(exc)[:400],
                root_replan=False,
            )
        except Exception:
            logger.error("worker_task_dispatch_unexpected_error", extra={"command_id": lease.command_id}, exc_info=True)
            await self._retry_or_wait(lease)

    async def _handle_response(
        self,
        lease: WorkerDispatchLease,
        status_code: int,
        body: dict[str, object],
        retry_after_seconds: float | None,
    ) -> None:
        """校验 HTTP 状态和协议响应组合，再执行明确的 S1 分类。"""
        if status_code in {200, 202}:
            try:
                receipt = WorkerCommandReceiptModel.model_validate(body)
            except ValueError:
                await self._repository.await_dispatch_result(lease)
                return
            if (
                str(receipt.command_id) != lease.command_id
                or (status_code == 202 and receipt.disposition != "accepted")
            ):
                await self._repository.await_dispatch_result(lease)
                return
            await self._repository.record_dispatch_receipt(lease, receipt)
            return
        try:
            validate_worker_request("ErrorResponse", body)
            error = WorkerHttpErrorResponseModel.model_validate(body)
        except (ValidationError, ValueError):
            await self._repository.await_dispatch_result(lease)
            return
        if status_code == _RETRY_STATUS_BY_CODE.get(error.code) and error.directive == "retry_same_command":
            if lease.attempt >= self._max_retries:
                await self._repository.exhaust_dispatch_retries(lease)
                return
            retry_after = _max_optional(
                retry_after_seconds, error.retry_after_seconds
            )
            delay = self._retry_delay(lease.attempt, retry_after)
            await self._repository.retry_dispatch_command(lease, delay_seconds=delay)
            return
        expected_status = _PERMANENT_STATUS_BY_CODE.get(error.code)
        if (
            expected_status == status_code
            and error.directive == _PERMANENT_DIRECTIVE_BY_CODE.get(error.code)
        ):
            await self._repository.fail_dispatch_command(
                lease,
                error_code=error.code,
                message=error.message,
                root_replan=error.code in {"TASK_INPUT_INVALID", "UNKNOWN_CONTRACT"},
            )
            return
        # 错误码、状态与 directive 任一不符都不授予重试/Root 重规划权限。
        await self._repository.await_dispatch_result(lease)

    async def _retry_or_wait(self, lease: WorkerDispatchLease) -> None:
        """网络结果不明时只重投原命令；耗尽后保留容量等待结果/deadline。"""
        if lease.attempt >= self._max_retries:
            await self._repository.exhaust_dispatch_retries(lease)
            return
        await self._repository.retry_dispatch_command(
            lease,
            delay_seconds=self._retry_delay(lease.attempt, None),
        )

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        """按配置的指数上限与抖动计算单次同命令重试间隔。"""
        base = min(self._retry_max, self._retry_initial * (2**attempt))
        spread = base * self._jitter_ratio
        jittered = min(
            self._retry_max,
            max(0.0, self._jitter(base - spread, base + spread)),
        )
        return max(jittered, retry_after or 0.0)


def _max_optional(left: float | None, right: float | None) -> float | None:
    """返回多个重试下限中较大的有效值。"""
    values = [value for value in (left, right) if value is not None]
    return max(values) if values else None


__all__ = ["WorkerTaskDispatcher"]
