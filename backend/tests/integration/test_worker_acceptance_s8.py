"""S8 实际 PostgreSQL 初始化及 HTTPS Worker 并发验收。"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import ssl
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI, Request, Response
from pydantic import SecretStr
from sqlalchemy import event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from application.orchestration.worker_execution_grants import WorkerExecutionGrantSigner
from application.orchestration.worker_plan_submission import WorkerPlanSubmissionService
from application.orchestration.worker_registry import WorkerRegistryService
from application.orchestration.worker_resources import WorkerResourceService
from application.orchestration.worker_result_submission import WorkerResultSubmissionService
from application.orchestration.worker_stop_confirmation import WorkerStopConfirmationService
from application.orchestration.worker_timeouts import WorkerTimeoutService
from application.orchestration.worker_registration import WorkerRegistrationService
from configuration.settings import Settings
from domain.orchestration.worker_entities import WorkerPlanSubmission, WorkerTaskSpec
from infrastructure.integrations.worker_http import PinnedWorkerHttpClient
from infrastructure.persistence.postgres.models import (
    AgentRunModel,
    Base,
    ExecutionLeaseModel,
    OrchestratorExecutionModel,
    OrchestratorPlanModel,
    OrchestratorRootResumeJobModel,
    OrchestratorTaskModel,
    OrchestratorWorkerCommandModel,
    OrchestrationOutboxModel,
    WorkerContractModel,
    WorkerApprovalRequestModel,
    WorkerInstanceModel,
    WorkerResourceModel,
    WorkerResultReceiptModel,
)
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
from tests.integration.test_reference_worker_https import (
    _ORCHESTRATOR_TOKEN,
    _SERVICE_TOKEN,
    _create_test_certificates,
    _free_port,
    _terminate_async,
    _wait_until,
    _worker_process,
)


_DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
_GRANT_SECRET = "s8-isolated-test-grant-secret-long-enough"
_SERVICE_PRINCIPAL = "worker-service"
_CAPABILITY = {
    "can_do": ["Echo one JSON object and return the exact input."],
    "cannot_do": ["Run arbitrary code or access data outside the request."],
    "input_schema": {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
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


@pytest_asyncio.fixture
async def s8_store():
    """在专用随机 schema 初始化完整目标元数据并于结束时删除。"""
    if not _DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"worker_s8_{uuid4().hex}"
    base_url = make_url(_DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    admin_engine = create_async_engine(base_url)
    schema_url = base_url.update_query_dict({"options": f"-c search_path={schema},public"})
    engine = create_async_engine(schema_url, pool_size=12, max_overflow=4)
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as connection:
            # search_path also includes public for extensions; disabling checkfirst
            # prevents SQLAlchemy from mistaking visible public tables for schema rows.
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection, checkfirst=False
                )
            )
        yield async_sessionmaker(engine, expire_on_commit=False), engine, schema, schema_url
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_s8_empty_schema_initializes_target_tables_and_lease_triggers(s8_store) -> None:
    """空 namespace 可完整创建所有目标表及队列租约触发器。"""
    _, engine, schema, _ = s8_store
    expected_tables = set(Base.metadata.tables)
    async with engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = :schema"),
            {"schema": schema},
        )
        actual_tables = {row[0] for row in rows}
        assert actual_tables == expected_tables
        triggers = await connection.execute(
            text(
                "SELECT c.relname FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema AND NOT t.tgisinternal "
                "AND t.tgname = 'execution_lease_enqueue'"
            ),
            {"schema": schema},
        )
        assert {
            row[0] for row in triggers
        } >= {"agent_runs", "task_executions", "orchestrator_root_resume_jobs"}


class _OriginPolicy:
    def __init__(self, client: PinnedWorkerHttpClient) -> None:
        self._client = client

    async def validate_registration_origin(self, base_url: str) -> None:
        await self._client.validate_registration_origin(base_url)


class _CountingProxy:
    """将 Worker HTTPS 202 回执丢弃一次并统计实际远端 HTTP 请求。"""

    def __init__(self, upstream_origin: str, ca_file: Path) -> None:
        import httpx

        self._httpx = httpx
        self._upstream_origin = upstream_origin
        self._ca_file = str(ca_file)
        self.task_requests = 0
        self.result_requests = 0
        self.first_task_accepted = asyncio.Event()
        self.dropped_task_response = False
        self.app = FastAPI()

        @self.app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
        async def proxy(request: Request, path: str) -> Response:
            body = await request.body()
            if path == "worker-api/v1/tasks":
                self.task_requests += 1
            async with self._httpx.AsyncClient(
                verify=self._ca_file, timeout=20, follow_redirects=False
            ) as client:
                upstream = await client.request(
                    request.method,
                    f"{self._upstream_origin}/{path}",
                    params=request.query_params,
                    headers={
                        key: value
                        for key, value in request.headers.items()
                        if key.lower() not in {"host", "content-length"}
                    },
                    content=body,
                )
            if path == "worker-api/v1/tasks" and upstream.status_code == 202:
                self.first_task_accepted.set()
                if not self.dropped_task_response:
                    self.dropped_task_response = True
                    return Response(status_code=503)
            response_headers = {
                key: value
                for key, value in upstream.headers.items()
                if key.lower() not in {"content-length", "transfer-encoding", "connection"}
            }
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                headers=response_headers,
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_s8_real_https_worker_100_tasks_8_concurrency_and_response_loss_recovery(
    s8_store, tmp_path: Path, record_property
) -> None:
    """真实 TLS Worker 完成 100 个并行任务并恢复一次丢失的 HTTP 回执。"""
    sessions, engine, schema, schema_url = s8_store
    tasks_count = int(os.getenv("ATHENA_S8_TASKS", "100"))
    concurrency = int(os.getenv("ATHENA_S8_CONCURRENCY", "8"))
    if not 1 <= tasks_count <= 1000 or not 1 <= concurrency <= 64:
        raise ValueError("s8_load_parameters_out_of_range")

    query_count = 0
    transaction_count = 0
    checkouts = 0
    checked_out = 0
    max_checked_out = 0

    def count_query(*_: object) -> None:
        nonlocal query_count
        query_count += 1

    def count_begin(*_: object) -> None:
        nonlocal transaction_count
        transaction_count += 1

    def count_checkout(*_: object) -> None:
        nonlocal checkouts, checked_out, max_checked_out
        checkouts += 1
        checked_out += 1
        max_checked_out = max(max_checked_out, checked_out)

    def count_checkin(*_: object) -> None:
        nonlocal checked_out
        checked_out -= 1

    event.listen(engine.sync_engine, "before_cursor_execute", count_query)
    event.listen(engine.sync_engine, "begin", count_begin)
    event.listen(engine.sync_engine, "checkout", count_checkout)
    event.listen(engine.sync_engine, "checkin", count_checkin)

    ca_file, certificate, private_key = _create_test_certificates(tmp_path)
    center_port, worker_port, proxy_port = _free_port(), _free_port(), _free_port()
    center_origin = f"https://127.0.0.1:{center_port}"
    worker_origin = f"https://127.0.0.1:{worker_port}"
    public_worker_origin = f"https://127.0.0.1:{proxy_port}"
    worker_data = tmp_path / "worker-data"
    capability_file = tmp_path / "capability.json"
    capability_file.write_text(json.dumps(_CAPABILITY), encoding="utf-8")
    settings = Settings().model_copy(
        update={
            "worker_protocol_service_tokens": {_SERVICE_PRINCIPAL: SecretStr(_SERVICE_TOKEN)},
            "worker_orchestrator_token": SecretStr(_ORCHESTRATOR_TOKEN),
            "worker_allowed_origins": (public_worker_origin,),
            "worker_allowed_networks": ("127.0.0.0/8",),
            "worker_protocol_ca_file": str(ca_file),
            "worker_execution_grant_secret": SecretStr(_GRANT_SECRET),
            "worker_resource_storage_path": str(tmp_path / "resources"),
        }
    )
    repository = PostgresWorkerOrchestrationRepository(sessions)
    registry = WorkerRegistryService(PostgresWorkerRegistryRepository(sessions))
    http_client = PinnedWorkerHttpClient(
        allowed_origins=[public_worker_origin],
        allowed_networks=["127.0.0.0/8"],
        orchestrator_token=_ORCHESTRATOR_TOKEN,
        ca_file=str(ca_file),
        timeout_seconds=2,
    )
    resource_service = WorkerResourceService(
        PostgresWorkerResourceRepository(sessions, tmp_path / "resources"),
        WorkerExecutionGrantSigner(settings.worker_execution_grant_secret),
        max_bytes=1_048_576,
    )
    monitor = WorkerHealthMonitor(
        registry,
        http_client,
        interval_seconds=1,
        failure_threshold=2,
        recovery_threshold=1,
        max_concurrency=2,
    )
    dispatcher = WorkerTaskDispatcher(
        repository,
        resource_service,
        http_client,
        worker_id=f"s8-dispatcher:{schema}",
        max_concurrency=concurrency,
        request_timeout_seconds=2,
        max_response_bytes=1_048_576,
        max_retries=4,
        retry_initial_seconds=0.1,
        retry_max_seconds=1,
        retry_jitter_ratio=0,
        poll_interval_seconds=0.02,
    )
    app = FastAPI()
    result_http_requests = 0

    @app.middleware("http")
    async def count_result_requests(request: Request, call_next):
        nonlocal result_http_requests
        if request.url.path.startswith("/api/worker-executions/") and request.url.path.endswith("/result"):
            result_http_requests += 1
        return await call_next(request)

    app.include_router(
        build_worker_registry_router(
            WorkerRegistrationService(repository),
            registry,
            WorkerResultSubmissionService(repository),
            WorkerStopConfirmationService(repository),
            _OriginPolicy(http_client),
            WorkerProtocolHttp({_SERVICE_PRINCIPAL: _SERVICE_TOKEN}),
        ),
        prefix="/api",
    )
    app.include_router(
        build_worker_resources_router(resource_service, WorkerProtocolHttp({_SERVICE_PRINCIPAL: _SERVICE_TOKEN})),
        prefix="/api",
    )
    install_worker_protocol_handlers(app)
    proxy = _CountingProxy(worker_origin, ca_file)
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
    proxy_server = uvicorn.Server(
        uvicorn.Config(
            proxy.app,
            host="127.0.0.1",
            port=proxy_port,
            ssl_certfile=str(certificate),
            ssl_keyfile=str(private_key),
            log_level="warning",
            access_log=False,
        )
    )
    server_task = asyncio.create_task(server.serve())
    proxy_task = asyncio.create_task(proxy_server.serve())
    worker: subprocess.Popen[str] | None = None
    await monitor.start()
    await dispatcher.start()
    started = time.perf_counter()
    try:
        tls = ssl.create_default_context(cafile=str(ca_file))
        import httpx

        async with httpx.AsyncClient(verify=tls, timeout=5) as client:
            await _wait_until(
                lambda: _https_ready(client, center_origin), timeout=20
            )
            concurrency_env = "ATHENA_REFERENCE_WORKER_MAX_CONCURRENCY"
            old_concurrency = os.environ.get(concurrency_env)
            os.environ[concurrency_env] = str(concurrency)
            try:
                worker = _worker_process(
                    base_url=center_origin,
                    public_url=public_worker_origin,
                    data_dir=worker_data,
                    capability_file=capability_file,
                    certificate=certificate,
                    private_key=private_key,
                    ca_file=ca_file,
                    port=worker_port,
                )
            finally:
                if old_concurrency is None:
                    os.environ.pop(concurrency_env, None)
                else:
                    os.environ[concurrency_env] = old_concurrency
            await _wait_until(lambda: _worker_ready(client, public_worker_origin), timeout=30)

            async def registered() -> bool:
                async with sessions() as session:
                    instance = await session.scalar(select(WorkerInstanceModel))
                return instance is not None and instance.status == "ready"

            await _wait_until(registered, timeout=30)
            async with sessions() as session:
                registered_instance = await session.scalar(select(WorkerInstanceModel))
                assert registered_instance is not None
                contract_id = registered_instance.contract_id
                worker_name = registered_instance.worker_name
            run_id, plan_id = str(uuid4()), str(uuid4())
            now = datetime.now(UTC)
            async with sessions() as session, session.begin():
                session.add(
                    AgentRunModel(
                        run_id=run_id,
                        session_id=f"s8-session-{run_id}",
                        message_id=str(uuid4()),
                        sequence=1,
                        status="running",
                        request_json={"acceptance": "s8"},
                        root_thread_id=f"run:{run_id}",
                        root_wait_generation=0,
                        orchestrator_plan_sequence=0,
                        cancellation_epoch=0,
                        cancellation_external_effect_status="none",
                        created_at=now,
                        updated_at=now,
                    )
                )
            submission = WorkerPlanSubmission(
                run_id=run_id,
                plan_id=plan_id,
                sequence=1,
                idempotency_key=f"s8-plan:{plan_id}",
                goal=f"S8 {tasks_count} concurrent HTTPS echo tasks",
                tasks=tuple(
                    WorkerTaskSpec(
                        task_ref=f"task-{index:04d}",
                        contract_id=contract_id,
                        contract_revision=1,
                        objective="Return the exact input object.",
                        input={"value": index},
                        acceptance_criteria="echo equals input",
                    )
                    for index in range(tasks_count)
                ),
                created_at=now,
            )
            accepted = await WorkerPlanSubmissionService(repository).submit(submission)
            assert accepted.accepted

            async def completed() -> bool:
                async with sessions() as session:
                    plan = await session.get(OrchestratorPlanModel, plan_id)
                    return plan is not None and plan.status == "completed"

            await _wait_until(completed, timeout=max(60, tasks_count * 2))
            elapsed = time.perf_counter() - started
            async with sessions() as session:
                statuses = list(
                    (
                        await session.scalars(
                            select(OrchestratorTaskModel.status).where(
                                OrchestratorTaskModel.plan_id == plan_id
                            )
                        )
                    ).all()
                )
                execution_names = list((await session.scalars(
                    select(OrchestratorExecutionModel.worker_name)
                    .join(
                        OrchestratorTaskModel,
                        OrchestratorExecutionModel.task_id == OrchestratorTaskModel.task_id,
                    )
                    .where(OrchestratorTaskModel.plan_id == plan_id)
                )).all())
                result_count = await session.scalar(
                    select(func.count()).select_from(
                        WorkerResultReceiptModel
                    ).join(
                        OrchestratorExecutionModel,
                        WorkerResultReceiptModel.execution_id
                        == OrchestratorExecutionModel.execution_id,
                    ).join(
                        OrchestratorTaskModel,
                        OrchestratorExecutionModel.task_id == OrchestratorTaskModel.task_id,
                    ).where(OrchestratorTaskModel.plan_id == plan_id)
                )
                resume_count = await session.scalar(
                    select(func.count()).select_from(
                        OrchestratorRootResumeJobModel
                    ).where(OrchestratorRootResumeJobModel.plan_id == plan_id)
                )
                command_rows = await session.execute(
                    select(OrchestratorWorkerCommandModel)
                    .join(
                        OrchestratorExecutionModel,
                        OrchestratorWorkerCommandModel.execution_id
                        == OrchestratorExecutionModel.execution_id,
                    )
                    .join(
                        OrchestratorTaskModel,
                        OrchestratorExecutionModel.task_id == OrchestratorTaskModel.task_id,
                    )
                    .where(
                        OrchestratorTaskModel.plan_id == plan_id,
                        OrchestratorWorkerCommandModel.command_type == "execute",
                    )
                )
                commands = list(command_rows.scalars().all())
                task_events = list((await session.scalars(
                    select(OrchestrationOutboxModel)
                    .where(
                        OrchestrationOutboxModel.plan_id == plan_id,
                        OrchestrationOutboxModel.event_type.in_(
                            ("TaskStarted", "TaskCompleted", "TaskFailed")
                        ),
                    )
                )).all())
                backlog = await session.scalar(
                    select(func.count()).select_from(
                        OrchestratorWorkerCommandModel
                    ).join(
                        OrchestratorExecutionModel,
                        OrchestratorWorkerCommandModel.execution_id
                        == OrchestratorExecutionModel.execution_id,
                    ).join(
                        OrchestratorTaskModel,
                        OrchestratorExecutionModel.task_id == OrchestratorTaskModel.task_id,
                    ).where(
                        OrchestratorTaskModel.plan_id == plan_id,
                        OrchestratorWorkerCommandModel.command_type == "execute",
                        OrchestratorWorkerCommandModel.status.in_(["queued", "retry_wait", "leased"]),
                    )
                )
            assert len(statuses) == tasks_count and set(statuses) == {"completed"}
            assert result_count == tasks_count
            assert resume_count == 1
            assert backlog == 0
            assert len(commands) == tasks_count
            assert all(command.status == "result_received" for command in commands)
            assert len(task_events) == tasks_count * 2
            started_events = [event for event in task_events if event.event_type == "TaskStarted"]
            completed_events = [event for event in task_events if event.event_type == "TaskCompleted"]
            assert len(started_events) == len(completed_events) == tasks_count
            assert all(event.payload_json["worker_name"] == worker_name for event in task_events)
            assert all(event.payload_json["parallel_task_count"] == tasks_count for event in started_events)
            assert all(event.payload_json["round_number"] == 1 for event in started_events)
            assert len(execution_names) == tasks_count
            assert set(execution_names) == {worker_name}
            # The dropped 202 can race with the Worker result callback; either the
            # idempotent retry or the result-before-receipt path may settle first.
            assert proxy.task_requests >= tasks_count
            assert proxy.dropped_task_response
            record_property("s8.tasks", tasks_count)
            record_property("s8.concurrency", concurrency)
            record_property("s8.elapsed_seconds", round(elapsed, 3))
            record_property("s8.tasks_per_second", round(tasks_count / elapsed, 3))
            record_property("s8.task_http_requests", proxy.task_requests)
            record_property("s8.result_http_requests", result_http_requests)
            record_property("s8.result_receipts", int(result_count or 0))
            record_property("s8.terminal_backlog", int(backlog or 0))
            record_property("s8.sql_statements", query_count)
            record_property("s8.transactions", transaction_count)
            record_property("s8.connection_checkouts", checkouts)
            record_property("s8.max_checked_out_connections", max_checked_out)
    finally:
        if worker is not None:
            await _terminate_async(worker)
        await dispatcher.stop()
        await monitor.stop()
        server.should_exit = True
        proxy_server.should_exit = True
        await asyncio.gather(server_task, proxy_task, return_exceptions=True)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_s8_timeout_scanner_batch_clears_due_backlog_and_is_idempotent(
    s8_store, record_property
) -> None:
    """隔离数据库中按批次裁决真实到期 Execution，并验证重复扫描不重复处理。"""
    sessions, _, _, _ = s8_store
    now = datetime.now(UTC)
    repository = PostgresWorkerOrchestrationRepository(
        sessions,
        max_execution_attempts=1,
        execution_retry_initial_seconds=1,
        execution_retry_max_seconds=1,
        execution_retry_jitter_ratio=0,
    )
    contract_id = str(uuid4())
    instance_id = str(uuid4())
    run_id, plan_id = str(uuid4()), str(uuid4())
    execution_ids: list[str] = []
    task_ids: list[str] = []
    async with sessions() as session, session.begin():
        session.add(
            AgentRunModel(
                run_id=run_id,
                session_id=f"s8-scanner-session-{run_id}",
                message_id=f"s8-scanner-message-{run_id}",
                sequence=1,
                status="running",
                request_json={"acceptance": "s8-scanner"},
                root_thread_id=f"s8-scanner-root-{run_id}",
                root_wait_generation=0,
                orchestrator_plan_sequence=1,
                cancellation_epoch=0,
                cancellation_external_effect_status="none",
                created_at=now - timedelta(seconds=10),
                updated_at=now,
            )
        )
        session.add(
            WorkerContractModel(
                contract_id=contract_id,
                revision=1,
                can_do_json=["scanner test"],
                cannot_do_json=[],
                input_schema_json={"type": "object"},
                output_schema_json={"type": "object"},
                fingerprint_hash=f"s8-scanner-contract-{contract_id}",
                created_at=now - timedelta(seconds=10),
            )
        )
        session.add(
            WorkerInstanceModel(
                instance_id=instance_id,
                service_principal="s8-scanner-service",
                instance_key=f"s8-scanner-instance-{instance_id}",
                worker_name="Test Worker",
                contract_id=contract_id,
                contract_revision=1,
                base_url="https://worker.invalid",
                execution_timeout_seconds=1,
                max_concurrency=32,
                status="ready",
                accepting_tasks=True,
                created_at=now - timedelta(seconds=10),
                updated_at=now,
            )
        )
        session.add(
            OrchestratorPlanModel(
                plan_id=plan_id,
                run_id=run_id,
                sequence=1,
                idempotency_key=f"s8-scanner-plan-{plan_id}",
                request_hash=f"s8-scanner-hash-{plan_id}",
                goal="S8 timeout scanner batch",
                status="running",
                expected_task_count=12,
                terminal_task_count=0,
                root_wait_generation=0,
                created_at=now - timedelta(seconds=10),
            )
        )
        await session.flush()
        for index in range(12):
            task_id = str(uuid4())
            execution_id = str(uuid4())
            task_ids.append(task_id)
            execution_ids.append(execution_id)
            session.add(
                OrchestratorTaskModel(
                    task_id=task_id,
                    operation_id=str(uuid4()),
                    plan_id=plan_id,
                    task_ref=f"scanner-task-{index}",
                    contract_id=contract_id,
                    contract_revision=1,
                    objective="timeout scanner test",
                    input_json={"index": index},
                    acceptance_criteria="timeout is terminal",
                    status="running",
                    execution_generation=1,
                    cancellation_epoch=0,
                    active_execution_id=execution_id,
                    created_at=now - timedelta(seconds=10),
                    updated_at=now,
                )
            )
            # PostgreSQL FK checks are immediate; flush each parent task before
            # inserting its execution and command rows.
            await session.flush()
            session.add(
                OrchestratorExecutionModel(
                    execution_id=execution_id,
                    task_id=task_id,
                    instance_id=instance_id,
                    generation=1,
                    status="running",
                    execution_timeout_seconds=1,
                    cancellation_epoch=0,
                    capacity_reserved=True,
                    started_at=now - timedelta(seconds=5),
                    deadline_at=now - timedelta(seconds=1),
                    created_at=now - timedelta(seconds=5),
                    updated_at=now,
                )
            )
            session.add(
                OrchestratorWorkerCommandModel(
                    command_id=str(uuid4()),
                    execution_id=execution_id,
                    instance_id=instance_id,
                    generation=1,
                    idempotency_key=f"s8-scanner-command-{execution_id}",
                    command_type="execute",
                    request_hash=f"s8-scanner-command-hash-{execution_id}",
                    command_json={"execution_id": execution_id},
                    status="awaiting_result",
                    attempt=0,
                    available_at=now - timedelta(seconds=1),
                    created_at=now - timedelta(seconds=5),
                    updated_at=now,
                )
            )

    initial_due = len(
        await repository.find_timeout_candidates(batch_size=100, queue_max_wait_seconds=300)
    )
    assert initial_due == len(execution_ids)
    service = WorkerTimeoutService(repository)
    started = time.perf_counter()
    handled = 0
    batches = 0
    while True:
        batches += 1
        current = await service.scan_once(batch_size=4, queue_max_wait_seconds=300)
        handled += current
        if current == 0:
            break
        assert batches < 10
    elapsed = time.perf_counter() - started
    final_due = len(
        await repository.find_timeout_candidates(batch_size=100, queue_max_wait_seconds=300)
    )
    repeat_handled = await service.scan_once(batch_size=4, queue_max_wait_seconds=300)
    assert handled == len(execution_ids)
    assert final_due == 0
    assert repeat_handled == 0
    async with sessions() as session:
        terminal = list(
            await session.scalars(
                select(OrchestratorExecutionModel.status).where(
                    OrchestratorExecutionModel.execution_id.in_(execution_ids)
                )
            )
        )
    assert set(terminal) == {"failed"}
    record_property("s8.scanner_batch_size", 4)
    record_property("s8.scanner_initial_due", initial_due)
    record_property("s8.scanner_final_due", final_due)
    record_property("s8.scanner_handled", handled)
    record_property("s8.scanner_batches", batches - 1)
    record_property("s8.scanner_elapsed_seconds", round(elapsed, 6))
    record_property(
        "s8.scanner_tasks_per_second",
        round(handled / elapsed, 3) if elapsed > 0 else 0,
    )
    record_property("s8.scanner_repeat_handled", repeat_handled)


async def _https_ready(client: object, origin: str) -> bool:
    try:
        response = await client.get(f"{origin}/openapi.json")
        return response.status_code == 200
    except Exception:
        return False


async def _worker_ready(client: object, origin: str) -> bool:
    try:
        response = await client.get(
            f"{origin}/worker-api/v1/health",
            headers={
                "Authorization": f"Bearer {_ORCHESTRATOR_TOKEN}",
                "X-Athena-Protocol-Version": "1.0",
            },
        )
        return response.status_code == 200 and response.json().get("status") == "ready"
    except Exception:
        return False
