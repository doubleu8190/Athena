"""S4 独立中心与 Worker HTTPS 反代进程入口。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
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
    """使用生产客户端策略校验反代的 HTTPS origin。"""

    def __init__(self, client: PinnedWorkerHttpClient) -> None:
        self._client = client

    async def validate_registration_origin(self, base_url: str) -> None:
        await self._client.validate_registration_origin(base_url)


async def _serve_center(
    *, database_url: str, schema: str, storage_root: str,
    service_token: str, orchestrator_token: str, grant_secret: str,
    worker_origin: str, ca_file: str, certificate: str, private_key: str,
    host: str, port: int,
) -> None:
    """装配 S4 独立中心，不注入 S3 的结果回执故障。"""
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    orchestration = PostgresWorkerOrchestrationRepository(sessions)
    registry = WorkerRegistryService(PostgresWorkerRegistryRepository(sessions))
    protocol = WorkerProtocolHttp({"worker-service": service_token})
    worker_http = PinnedWorkerHttpClient(
        allowed_origins=[worker_origin], allowed_networks=["127.0.0.0/8"],
        orchestrator_token=orchestrator_token, ca_file=ca_file, timeout_seconds=1,
    )
    resource_service = WorkerResourceService(
        PostgresWorkerResourceRepository(sessions, Path(storage_root)),
        WorkerExecutionGrantSigner(SecretStr(grant_secret)), max_bytes=1_048_576,
    )
    monitor = WorkerHealthMonitor(
        registry, worker_http, interval_seconds=1, failure_threshold=1,
        recovery_threshold=1, max_concurrency=2,
    )
    dispatcher = WorkerTaskDispatcher(
        orchestration, resource_service, worker_http,
        worker_id=f"s4-test-dispatcher:{schema}", max_concurrency=4,
        request_timeout_seconds=2, max_response_bytes=1_048_576, max_retries=3,
        retry_initial_seconds=0.1, retry_max_seconds=1, retry_jitter_ratio=0,
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
    app.include_router(
        build_worker_registry_router(
            lambda: WorkerRegistrationService(orchestration), lambda: registry,
            lambda: WorkerResultSubmissionService(orchestration),
            lambda: WorkerStopConfirmationService(orchestration),
            _TestOriginPolicy(worker_http), protocol,
        ), prefix="/api",
    )
    app.include_router(
        build_worker_resources_router(lambda: resource_service, protocol), prefix="/api"
    )
    install_worker_protocol_handlers(app)
    await uvicorn.Server(uvicorn.Config(
        app, host=host, port=port, ssl_certfile=certificate,
        ssl_keyfile=private_key, log_level="warning", access_log=False,
    )).serve()


def run_center(**kwargs: str | int) -> None:
    """多进程入口，避免与 pytest 事件循环共享 ASGI 服务。"""
    asyncio.run(_serve_center(**kwargs))


async def _proxy_app(
    *, upstream_origin: str, ca_file: str, marker_file: str, delay_seconds: float
) -> FastAPI:
    """创建转发 Worker 请求的 HTTPS 反代。"""
    app = FastAPI()
    first_task = True
    lock = asyncio.Lock()

    @app.api_route(
        "/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    )
    async def proxy(request: Request, path: str) -> Response:
        nonlocal first_task
        body = await request.body()
        headers = {
            key: value for key, value in request.headers.items()
            if key.lower() not in {"host", "content-length"}
        }
        async with httpx.AsyncClient(verify=ca_file, timeout=10, follow_redirects=False) as client:
            upstream = await client.request(
                request.method, f"{upstream_origin}/{path}", params=request.query_params,
                headers=headers, content=body,
            )
        if path == "worker-api/v1/tasks" and upstream.status_code == 202:
            async with lock:
                should_delay = first_task
                first_task = False
            if should_delay:
                Path(marker_file + ".received").touch()
                async def release_marker() -> None:
                    await asyncio.sleep(delay_seconds)
                    Path(marker_file + ".released").touch()

                # Dispatcher 的请求超时可以先结束客户端连接；反代仍保留
                # 可观测的“原始 202 已收到、尚未释放”窗口。
                asyncio.create_task(release_marker())
                await asyncio.sleep(delay_seconds)
        response_headers = {
            key: value for key, value in upstream.headers.items()
            if key.lower() not in {"content-length", "transfer-encoding", "connection"}
        }
        return Response(content=upstream.content, status_code=upstream.status_code, headers=response_headers)

    return app


async def _serve_proxy(
    *, upstream_origin: str, ca_file: str, certificate: str, private_key: str,
    marker_file: str, delay_seconds: float, host: str, port: int,
) -> None:
    app = await _proxy_app(
        upstream_origin=upstream_origin, ca_file=ca_file,
        marker_file=marker_file, delay_seconds=delay_seconds,
    )
    await uvicorn.Server(uvicorn.Config(
        app, host=host, port=port, ssl_certfile=certificate,
        ssl_keyfile=private_key, log_level="warning", access_log=False,
    )).serve()


def run_worker_proxy(**kwargs: str | int | float) -> None:
    """多进程入口，启动延迟 TaskCommand 202 的 HTTPS 反代。"""
    asyncio.run(_serve_proxy(**kwargs))


__all__ = ["run_center", "run_worker_proxy"]
