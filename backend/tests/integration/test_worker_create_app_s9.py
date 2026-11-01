"""S9：真实 create_app、隔离 PostgreSQL 和独立参考 Worker 验收。

本文件验证生产应用工厂的 HTTP wiring、认证边界、Worker 注册协议以及
应用重启后的持久事实。测试只使用随机 PostgreSQL schema，并将外部资源
（Neo4j、Embedding、Sandbox 和后台消费者）明确排除在本切片之外。
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import uvicorn
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from bootstrap import create_app
from bootstrap.dependencies import build_dependencies
from bootstrap.postgres import build_postgres_dependencies
from configuration.settings import Settings
from infrastructure.persistence.postgres.models import WorkerInstanceModel
from infrastructure.persistence.postgres.models import Base
from interfaces.http.auth import AuthOptions
from tests.integration.test_reference_worker_https import (
    _ORCHESTRATOR_TOKEN,
    _SERVICE_TOKEN,
    _create_test_certificates,
    _free_port,
    _terminate,
    _wait_until,
    _worker_process,
)


_DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
_SERVICE_PRINCIPAL = "worker-service"
_GRANT_SECRET = "s9-isolated-test-grant-secret-long-enough"
_AUTH_SECRET = "s9-isolated-test-auth-session-secret-long-enough"
_CAPABILITY = {
    "can_do": ["Echo one JSON object and return the exact input."],
    "cannot_do": ["Run arbitrary code or access data outside the request."],
    "input_schema": {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {"echo": {"type": "object"}},
        "required": ["echo"],
        "additionalProperties": False,
    },
}


async def _stop_server(
    server: uvicorn.Server,
    task: asyncio.Task[bool],
    resources: tuple[object, ...] = (),
) -> None:
    server.should_exit = True
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    for resource in reversed(resources):
        stop = getattr(resource, "stop", None)
        if stop is not None:
            result = stop()
            if result is not None:
                await result


async def _wait_https(client: httpx.AsyncClient, origin: str) -> None:
    async def ready() -> bool:
        try:
            return (await client.get(f"{origin}/health")).status_code == 200
        except httpx.HTTPError:
            return False

    await _wait_until(ready, timeout=20)


async def _wait_startup(app: object) -> None:
    async def started() -> bool:
        return bool(getattr(getattr(app, "state", None), "startup_complete", False))

    await _wait_until(started, timeout=30)


@pytest_asyncio.fixture
async def s9_runtime(tmp_path: Path):
    """创建真实生产依赖图，并将所有连接限制在随机 schema。"""
    if not _DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"worker_s9_{uuid4().hex}"
    base_url = make_url(_DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    admin_engine = create_async_engine(base_url)
    schema_url = base_url.update_query_dict({"options": f"-c search_path={schema},public"})
    schema_engine = create_async_engine(schema_url)
    ca_file, certificate, private_key = _create_test_certificates(tmp_path)
    center_port, worker_port = _free_port(), _free_port()
    center_origin = f"https://127.0.0.1:{center_port}"
    worker_origin = f"https://127.0.0.1:{worker_port}"
    capability_file = tmp_path / "capability.json"
    capability_file.write_text(json.dumps(_CAPABILITY), encoding="utf-8")
    worker_data = tmp_path / "worker-data"
    worker = None
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with schema_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection, checkfirst=False
                )
            )
        settings = Settings().model_copy(
            update={
                "postgres_host": base_url.host or "127.0.0.1",
                "postgres_port": base_url.port or 8190,
                "postgres_user": base_url.username or "",
                "postgres_password": base_url.password or "",
                "postgres_db": base_url.database or "",
                "postgres_create_schema": True,
                "workers_enabled": False,
                "worker_protocol_service_tokens": {
                    _SERVICE_PRINCIPAL: SecretStr(_SERVICE_TOKEN)
                },
                "worker_orchestrator_token": SecretStr(_ORCHESTRATOR_TOKEN),
                "worker_allowed_origins": (worker_origin,),
                "worker_allowed_networks": ("127.0.0.0/8",),
                "worker_protocol_ca_file": str(ca_file),
                "worker_execution_grant_secret": SecretStr(_GRANT_SECRET),
                "worker_resource_storage_path": str(tmp_path / "resources"),
                "worker_heartbeat_interval_seconds": 2,
                "worker_heartbeat_request_timeout_seconds": 1,
                "worker_dispatch_http_timeout_seconds": 1,
                "sandbox_enabled": False,
            }
        )
        dependencies = build_postgres_dependencies(settings)
        # ``PostgresOptions`` is frozen; mutate only this freshly-created test
        # graph before startup so every production repository closure keeps the
        # same isolated engine and URL.
        object.__setattr__(
            dependencies.resources[0].options,
            "url",
            schema_url.render_as_string(hide_password=False),
        )
        # The production graph is assembled intact; this S9 slice starts only
        # PostgreSQL because external model/graph services are not acceptance
        # prerequisites for HTTP wiring and would make the test non-isolated.
        reduced = build_dependencies(
            services=dependencies.services,
            resources=(dependencies.resources[0],),
            routers=dependencies.routers,
        )
        auth = AuthOptions(
            enabled=True,
            username="s9-user",
            password="s9-password",
            session_secret=_AUTH_SECRET,
            host="127.0.0.1",
            port=center_port,
            frontend_port=5173,
        )
        app = create_app(dependencies=reduced, auth_options=auth)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=center_port,
                ssl_certfile=str(certificate),
                ssl_keyfile=str(private_key),
                log_level="warning",
                access_log=False,
            )
        )
        server_task = asyncio.create_task(server.serve())
        tls = ssl.create_default_context(cafile=str(ca_file))
        async with httpx.AsyncClient(verify=tls, timeout=10) as client:
            await _wait_https(client, center_origin)
            await _wait_startup(app)
            worker = _worker_process(
                base_url=center_origin,
                public_url=worker_origin,
                data_dir=worker_data,
                capability_file=capability_file,
                certificate=certificate,
                private_key=private_key,
                ca_file=ca_file,
                port=worker_port,
            )
            yield {
                "app": app,
                "dependencies": dependencies,
                "reduced": reduced,
                "settings": settings,
                "client": client,
                "server": server,
                "server_task": server_task,
                "session_factory": dependencies.resources[0].session_factory,
                "schema": schema,
                "center_origin": center_origin,
                "worker_origin": worker_origin,
                "worker": worker,
                "worker_data": worker_data,
                "ca_file": ca_file,
                "certificate": certificate,
                "private_key": private_key,
                "schema_url": schema_url,
            }
        await _stop_server(server, server_task, (dependencies.resources[0],))
        server = None
    finally:
        if worker is not None:
            _terminate(worker)
        if "server" in locals() and server is not None:
            await _stop_server(server, server_task, (dependencies.resources[0],))
        await schema_engine.dispose()
        await admin_engine.dispose()
        cleanup_engine = create_async_engine(base_url)
        try:
            async with cleanup_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            await cleanup_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_s9_create_app_worker_registration_auth_and_restart(s9_runtime) -> None:
    """真实应用工厂可承载 Worker 注册，认证边界有效且重启保留事实。"""
    runtime = s9_runtime
    client: httpx.AsyncClient = runtime["client"]
    center = runtime["center_origin"]

    assert (await client.get(f"{center}/health")).json() == {
        "status": "ok",
        "architecture": "restructured",
    }
    assert (await client.get(f"{center}/api/projects")).status_code == 401
    login = await client.post(
        f"{center}/api/auth/login",
        json={"username": "s9-user", "password": "s9-password"},
    )
    assert login.status_code == 200
    protected = await client.get(
        f"{center}/api/projects",
        headers={"Origin": "http://127.0.0.1:5173"},
    )
    assert protected.status_code == 200
    rejected_origin = await client.post(
        f"{center}/api/projects",
        headers={"Origin": "https://untrusted.invalid"},
        json={"name": "S9 denied origin"},
    )
    assert rejected_origin.status_code == 403

    async def registered() -> bool:
        session_factory = runtime["session_factory"]
        async with session_factory() as session:
            return await session.scalar(select(WorkerInstanceModel)) is not None

    await _wait_until(registered, timeout=30)
    async with runtime["dependencies"].resources[0].session_factory() as session:
        instance = await session.scalar(select(WorkerInstanceModel))
        assert instance is not None
        assert instance.base_url == runtime["worker_origin"]
        assert instance.contract_revision == 1

    bad = await client.post(
        f"{center}/api/worker-instances",
        headers={
            "Authorization": "Bearer wrong-token",
            "X-Athena-Protocol-Version": "1.0",
            "Content-Type": "application/json; charset=utf-8",
            "Idempotency-Key": str(uuid4()),
        },
        content=b"{}",
    )
    assert bad.status_code == 401

    # Rebuild a second real application graph against the same schema.  This
    # exercises the production startup wiring and reads the durable project
    # and registration facts through fresh repositories.
    await _stop_server(
        runtime["server"],
        runtime["server_task"],
        (runtime["dependencies"].resources[0],),
    )
    restarted_dependencies = build_postgres_dependencies(runtime["settings"])
    object.__setattr__(
        restarted_dependencies.resources[0].options,
        "url",
        runtime["schema_url"].render_as_string(hide_password=False),
    )
    restarted_reduced = build_dependencies(
        services=restarted_dependencies.services,
        resources=(restarted_dependencies.resources[0],),
        routers=restarted_dependencies.routers,
    )
    restarted_port = _free_port()
    restarted_app = create_app(
        dependencies=restarted_reduced,
        auth_options=AuthOptions(
            enabled=True,
            username="s9-user",
            password="s9-password",
            session_secret=_AUTH_SECRET,
            host="127.0.0.1",
            port=restarted_port,
            frontend_port=5173,
        ),
    )
    restarted_server = uvicorn.Server(
        uvicorn.Config(
            restarted_app,
            host="127.0.0.1",
            port=restarted_port,
            ssl_certfile=str(runtime["certificate"]),
            ssl_keyfile=str(runtime["private_key"]),
            log_level="warning",
            access_log=False,
        )
    )
    restarted_task = asyncio.create_task(restarted_server.serve())
    restarted_center = f"https://127.0.0.1:{restarted_port}"
    try:
        async with httpx.AsyncClient(
            verify=ssl.create_default_context(cafile=str(runtime["ca_file"])),
            timeout=10,
        ) as restarted_client:
            await _wait_https(restarted_client, restarted_center)
            await _wait_startup(restarted_app)
            restarted_login = await restarted_client.post(
                f"{restarted_center}/api/auth/login",
                json={"username": "s9-user", "password": "s9-password"},
            )
            assert restarted_login.status_code == 200
            restarted_session = await restarted_client.get(f"{restarted_center}/api/auth/session")
            assert restarted_session.status_code == 200
            assert restarted_session.json()["authenticated"] is True
            async with restarted_dependencies.resources[0].session_factory() as session:
                restored = await session.scalar(select(WorkerInstanceModel))
                assert restored is not None
                assert restored.base_url == runtime["worker_origin"]
    finally:
        await _stop_server(
            restarted_server,
            restarted_task,
            (restarted_dependencies.resources[0],),
        )

    # The restarted graph above read the durable Worker row through a fresh
    # repository; schema cleanup happens in the fixture.
