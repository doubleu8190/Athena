"""持久领取审批恢复命令，并通过受限 HTTPS 投递给原 Worker。"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from uuid import uuid4

import httpx
from jsonschema.exceptions import ValidationError

from application.orchestration.worker_protocol_models import (
    WorkerHttpErrorResponseModel,
    WorkerResumeCommandModel,
    WorkerResumeReceiptModel,
)
from application.orchestration.worker_resources import WorkerResourceService
from application.orchestration.worker_protocol_validation import validate_worker_request
from domain.orchestration.worker_entities import WorkerDispatchLease, WorkerDispatchStart
from infrastructure.integrations.worker_http import (
    PinnedWorkerHttpClient,
    WorkerOriginRejected,
    WorkerTaskPostResponse,
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


class WorkerResumeDispatcher:
    """在有限并发槽位内投递 ResumeCommand，并保持命令幂等性。"""

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
        """配置恢复命令调度器。

        参数：
        - `repository`：持久化恢复命令和租约的仓储。
        - `resource_service`：在首次发送前签发执行授权。
        - `http_client`：执行固定 DNS/TLS 的 HTTPS 客户端。
        - `worker_id`：当前调度进程的租约所有者。
        - 其余参数：并发、HTTP 响应上限、传输重试和轮询配置。

        返回：无。

        异常：配置不是有限正数或重试上限关系无效时抛出 ValueError。
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
            raise ValueError("worker_resume_dispatch_configuration_invalid")
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

    @property
    def _lease_seconds(self) -> float:
        """返回覆盖一次恢复 HTTP 请求和持久收尾的租约时长。"""
        return max(self._request_timeout + 10.0, 15.0)

    async def start(self) -> None:
        """启动唯一恢复命令循环；重复调用保持幂等。"""
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stop.clear()
        self._loop_task = asyncio.create_task(
            self.run(), name=f"athena-worker-resume-dispatcher:{self._worker_id}"
        )

    async def stop(self) -> None:
        """停止领取并取消网络操作，让持久租约自然恢复。"""
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
        """持续领取 ResumeCommand，并限制本进程并发数。"""
        while not self._stop.is_set():
            claimed = False
            while not self._stop.is_set() and len(self._inflight) < self._max_concurrency:
                try:
                    lease = await self._repository.claim_dispatch_command(
                        owner_id=self._worker_id,
                        lease_seconds=self._lease_seconds,
                        command_types=("resume",),
                    )
                    if lease is None:
                        break
                    task = asyncio.create_task(
                        self._dispatch(lease),
                        name=f"athena-worker-resume-command:{lease.command_id}",
                    )
                    self._inflight.add(task)
                    task.add_done_callback(self._on_done)
                    claimed = True
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.error("worker_resume_command_claim_failed", exc_info=True)
                    break
            if not claimed or len(self._inflight) < self._max_concurrency:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
                except asyncio.TimeoutError:
                    pass

    def _on_done(self, task: asyncio.Task[None]) -> None:
        """回收完成的恢复任务并记录异常。"""
        self._inflight.discard(task)
        error = None if task.cancelled() else task.exception()
        if error is not None:
            logger.error(
                "worker_resume_dispatch_failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _dispatch(self, lease: WorkerDispatchLease) -> None:
        """准备并发送一次恢复命令；重投复用已冻结的 body 和 deadline。"""
        try:
            start = await self._repository.prepare_resume_dispatch(
                lease, lease_seconds=self._lease_seconds
            )
            if start is None:
                return
            first_send = "assignment_required" in start.command
            if first_send:
                grant_context = await self._resource_service.prepare_execution_grant_context(
                    start.execution_id
                )

                def build_command(final_start: WorkerDispatchStart) -> dict[str, object]:
                    if final_start.deadline_at is None:
                        raise ValueError("worker_resume_deadline_missing")
                    grant = self._resource_service.issue_prepared_execution_grant(
                        grant_context, final_start
                    )
                    draft = {
                        key: value
                        for key, value in final_start.command.items()
                        if key != "assignment_required"
                    }
                    command = WorkerResumeCommandModel.model_validate(
                        {
                            **draft,
                            "execution_grant": {
                                "token": grant,
                                "expires_at": final_start.deadline_at,
                            },
                        }
                    )
                    return command.model_dump(mode="json", exclude_none=True)

                start = await self._repository.prepare_first_resume_send(
                    lease,
                    lease_seconds=self._lease_seconds,
                    command_builder=build_command,
                )
                if start is None:
                    return
            else:
                # 已冻结命令必须按原始内容重投，不重新签发授权或重置 deadline。
                start = WorkerDispatchStart(
                    command_id=start.command_id,
                    execution_id=start.execution_id,
                    instance_id=start.instance_id,
                    instance_key=start.instance_key,
                    service_principal=start.service_principal,
                    base_url=start.base_url,
                    command=dict(start.command),
                    request_hash=start.request_hash,
                    started_at=start.started_at,
                    deadline_at=start.deadline_at,
                )
            response = await self._http.send_resume(
                start,
                max_response_bytes=self._max_response_bytes,
                timeout_seconds=self._request_timeout,
            )
            await self._handle_response(lease, response)
        except asyncio.CancelledError:
            raise
        except WorkerOriginRejected:
            await self._repository.await_dispatch_result(lease)
        except (httpx.HTTPError, WorkerTaskResponseInvalid):
            await self._retry_or_wait(lease)
        except ValueError as exc:
            if str(exc) in {"worker_dispatch_lease_lost", "worker_dispatch_command_body_immutable"}:
                return
            await self._repository.await_dispatch_result(lease)
        except Exception:
            logger.error("worker_resume_dispatch_unexpected_error", exc_info=True)
            await self._retry_or_wait(lease)

    async def _handle_response(
        self, lease: WorkerDispatchLease, response: WorkerTaskPostResponse
    ) -> None:
        """验证 ResumeReceipt 或协议错误并更新持久命令状态。"""
        if response.status_code in {200, 202}:
            try:
                receipt = WorkerResumeReceiptModel.model_validate(response.body)
            except ValueError:
                # 响应已到但无法解析，Worker 是否已持久接收未知；重投同一命令。
                await self._retry_or_wait(lease)
                return
            if (
                str(receipt.command_id) != lease.command_id
                or (response.status_code == 202 and receipt.disposition != "accepted")
                or (response.status_code == 200 and receipt.disposition != "duplicate")
            ):
                await self._retry_or_wait(lease)
                return
            await self._repository.record_resume_receipt(lease, receipt)
            return
        try:
            validate_worker_request("ErrorResponse", response.body)
            error = WorkerHttpErrorResponseModel.model_validate(response.body)
        except (ValidationError, ValueError):
            # 损坏的错误响应不能证明命令未被接收，按传输不确定性重投。
            await self._retry_or_wait(lease)
            return
        if (
            response.status_code == _RETRY_STATUS_BY_CODE.get(error.code)
            and error.directive == "retry_same_command"
        ):
            if lease.attempt >= self._max_retries:
                await self._repository.exhaust_dispatch_retries(lease)
                return
            retry_after = max(
                value
                for value in (response.retry_after_seconds, error.retry_after_seconds)
                if value is not None
            ) if (response.retry_after_seconds is not None or error.retry_after_seconds is not None) else None
            await self._repository.retry_dispatch_command(
                lease,
                delay_seconds=self._retry_delay(lease.attempt, retry_after),
            )
            return
        await self._repository.await_dispatch_result(lease)

    async def _retry_or_wait(self, lease: WorkerDispatchLease) -> None:
        """对网络不确定性重投同一命令，耗尽后保留持久等待状态。"""
        if lease.attempt >= self._max_retries:
            await self._repository.exhaust_dispatch_retries(lease)
            return
        await self._repository.retry_dispatch_command(
            lease,
            delay_seconds=self._retry_delay(lease.attempt, None),
        )

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        """计算指数退避，并遵守 Worker 给出的 Retry-After 下限。"""
        base = min(self._retry_max, self._retry_initial * (2**attempt))
        spread = base * self._jitter_ratio
        delay = min(self._retry_max, max(0.0, self._jitter(base - spread, base + spread)))
        return max(delay, retry_after or 0.0)


__all__ = ["WorkerResumeDispatcher"]
