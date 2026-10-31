"""真实 HTTPS 参考 Worker 验收所用的后端子进程入口。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from application.orchestration.worker_execution_grants import WorkerExecutionGrantSigner
from application.orchestration.worker_registration import WorkerRegistrationService
from application.orchestration.worker_registry import WorkerRegistryService
from application.orchestration.worker_resources import WorkerResourceService
from application.orchestration.worker_result_submission import WorkerResultSubmissionService
from application.orchestration.worker_stop_confirmation import WorkerStopConfirmationService
from infrastructure.integrations.worker_http import PinnedWorkerHttpClient
from infrastructure.persistence.postgres.repositories.worker_orchestration_repository import (
    PostgresWorkerOrchestrationRepository,
)
from infrastructure.persistence.postgres.repositories.worker_registry_repository import (
    PostgresWorkerRegistryRepository,
)
from infrastructure.persistence.postgres.repositories.worker_resource_repository import (
    PostgresWorkerResourceRepository,
)
from interfaces.http.controllers.worker_registry_controller import build_worker_registry_router
from interfaces.http.controllers.worker_resources_controller import build_worker_resources_router
from interfaces.http.worker_protocol import WorkerProtocolHttp, install_worker_protocol_handlers
from workers.worker_health_monitor import WorkerHealthMonitor
from workers.worker_task_dispatcher import WorkerTaskDispatcher


class _TestOriginPolicy:
    """使用生产 DNS/IP/TLS 策略校验临时 loopback Worker 地址。"""

    def __init__(self, client: PinnedWorkerHttpClient) -> None:
        self._client = client

    async def validate_registration_origin(self, base_url: str) -> None:
        await self._client.validate_registration_origin(base_url)


async def _serve(
    *,
    database_url: str,
    schema: str,
    storage_root: str,
    service_token: str,
    orchestrator_token: str,
    grant_secret: str,
    worker_origin: str,
    ca_file: str,
    certificate: str,
    private_key: str,
    host: str,
    port: int,
) -> None:
    """装配真实注册、资源、结果和健康组件并运行 TLS 服务。"""
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    orchestration = PostgresWorkerOrchestrationRepository(sessions)
    registry_repository = PostgresWorkerRegistryRepository(sessions)
    registry = WorkerRegistryService(registry_repository)
    protocol = WorkerProtocolHttp({"worker-service": service_token})
    worker_http = PinnedWorkerHttpClient(
        allowed_origins=[worker_origin],
        allowed_networks=["127.0.0.0/8"],
        orchestrator_token=orchestrator_token,
        ca_file=ca_file,
        timeout_seconds=1,
    )
    resource_service = WorkerResourceService(
        PostgresWorkerResourceRepository(sessions, Path(storage_root)),
        WorkerExecutionGrantSigner(SecretStr(grant_secret)),
        max_bytes=1_048_576,
    )
    monitor = WorkerHealthMonitor(
        registry,
        worker_http,
        interval_seconds=1,
        failure_threshold=1,
        recovery_threshold=1,
        max_concurrency=2,
    )
    dispatcher = WorkerTaskDispatcher(
        orchestration,
        resource_service,
        worker_http,
        worker_id=f"s4-test-dispatcher:{schema}",
        max_concurrency=4,
        request_timeout_seconds=2,
        max_response_bytes=1_048_576,
        max_retries=3,
        retry_initial_seconds=0.1,
        retry_max_seconds=1,
        retry_jitter_ratio=0,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await monitor.start()
        await dispatcher.start()
        try:
            yield
        finally:
            await dispatcher.stop()
            await monitor.stop()
            await engine.dispose()

    app = FastAPI(lifespan=lifespan)
    dropped_result_response = asyncio.Event()

    @app.middleware("http")
    async def lose_first_result_response(request: Request, call_next):
        """在结果已提交到 PostgreSQL 后丢弃第一次 HTTP 回执。"""
        response = await call_next(request)
        if (
            request.url.path.endswith("/result")
            and response.status_code == 200
            and not dropped_result_response.is_set()
        ):
            dropped_result_response.set()
            chunks = [chunk async for chunk in response.body_iterator]
            del chunks
            return JSONResponse(
                {"protocol_version": "1.0", "code": "SIMULATED_ACK_LOSS"},
                status_code=503,
            )
        return response

    app.include_router(
        build_worker_registry_router(
            lambda: WorkerRegistrationService(orchestration),
            lambda: registry,
            lambda: WorkerResultSubmissionService(orchestration),
            lambda: WorkerStopConfirmationService(orchestration),
            _TestOriginPolicy(worker_http),
            protocol,
        ),
        prefix="/api",
    )
    app.include_router(
        build_worker_resources_router(lambda: resource_service, protocol), prefix="/api"
    )
    install_worker_protocol_handlers(app)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        ssl_certfile=certificate,
        ssl_keyfile=private_key,
        log_level="warning",
        access_log=False,
    )
    await uvicorn.Server(config).serve()


def run_center(**kwargs: str | int) -> None:
    """多进程入口，避免在 pytest event loop 中共享 ASGI transport。"""
    asyncio.run(_serve(**kwargs))


__all__ = ["run_center"]
