"""真实 PostgreSQL 与 ASGI 验证执行授权和 S3 资源读写。"""

from __future__ import annotations

import hashlib
import asyncio
import os
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from application.orchestration.worker_execution_grants import (
    WorkerExecutionGrantSigner,
)
from application.orchestration.worker_resources import WorkerResourceService
from domain.orchestration.worker_resource_entities import WorkerExecutionResourceScope
from interfaces.http.controllers.worker_resources_controller import (
    build_worker_resources_router,
)
from interfaces.http.worker_protocol import (
    WorkerProtocolHttp,
    install_worker_protocol_handlers,
)
from infrastructure.persistence.postgres.models import (
    AgentRunModel,
    Base,
    OrchestratorExecutionModel,
    OrchestratorPlanModel,
    OrchestratorTaskModel,
    ProjectModel,
    SessionModel,
    WorkerContractModel,
    WorkerInstanceModel,
    WorkerResourceModel,
)
from infrastructure.persistence.postgres.repositories.worker_resource_repository import (
    PostgresWorkerResourceRepository,
)


_DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
_RESOURCE_TABLES = [
    ProjectModel.__table__,
    SessionModel.__table__,
    AgentRunModel.__table__,
    WorkerContractModel.__table__,
    WorkerInstanceModel.__table__,
    OrchestratorPlanModel.__table__,
    OrchestratorTaskModel.__table__,
    OrchestratorExecutionModel.__table__,
    WorkerResourceModel.__table__,
]


@pytest_asyncio.fixture
async def resource_store(tmp_path):
    """只在随机 schema 创建资源所需真实表，并在用例后清理。"""
    if not _DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"worker_resource_s3_{uuid4().hex}"
    base_url = make_url(_DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    admin_engine = create_async_engine(base_url)
    test_engine = create_async_engine(
        base_url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection, tables=_RESOURCE_TABLES
                )
            )
        yield async_sessionmaker(test_engine, expire_on_commit=False), tmp_path
    finally:
        await test_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


async def _seed_execution(
    session_factory: async_sessionmaker,
    *,
    run_id: str,
    plan_id: str,
    task_id: str,
    execution_id: str,
    instance_id: str,
    contract_id: str,
    input_json: dict[str, object] | None = None,
    service_principal: str = "worker-service",
) -> None:
    """插入一条有完整父子身份、仍在执行且含 deadline 的目标事实。"""
    now = datetime.now(timezone.utc)
    async with session_factory() as session, session.begin():
        if await session.get(ProjectModel, f"project-{run_id}") is None:
            session.add(
                ProjectModel(
                    id=f"project-{run_id}",
                    name="Worker resource test",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                SessionModel(
                    id=f"session-{run_id}",
                    project_id=f"project-{run_id}",
                    title="Worker resource test",
                    created_at=now,
                    updated_at=now,
                    last_activity_at=now,
                )
            )
            session.add(
                AgentRunModel(
                    run_id=run_id,
                    session_id=f"session-{run_id}",
                    message_id=f"message-{run_id}",
                    sequence=1,
                    status="running",
                    root_thread_id=f"root-{run_id}",
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
        if await session.get(WorkerContractModel, (contract_id, 1)) is None:
            session.add(
                WorkerContractModel(
                    contract_id=contract_id,
                    revision=1,
                    can_do_json=["store and read test resources"],
                    cannot_do_json=["cross-run resource access"],
                    input_schema_json={"type": "object"},
                    output_schema_json={"type": "object"},
                    fingerprint_hash=f"fingerprint-{contract_id}",
                    created_at=now,
                )
            )
            await session.flush()
        if await session.get(WorkerInstanceModel, instance_id) is None:
            session.add(
                WorkerInstanceModel(
                    instance_id=instance_id,
                    service_principal=service_principal,
                    instance_key=str(uuid4()),
                    contract_id=contract_id,
                    contract_revision=1,
                    base_url="https://worker.invalid",
                    execution_timeout_seconds=600,
                    max_concurrency=4,
                    status="ready",
                    accepting_tasks=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
        if await session.get(OrchestratorPlanModel, plan_id) is None:
            session.add(
                OrchestratorPlanModel(
                    plan_id=plan_id,
                    run_id=run_id,
                    sequence=1,
                    idempotency_key=f"plan-{plan_id}",
                    request_hash=f"hash-{plan_id}",
                    goal="test resource access",
                    status="running",
                    expected_task_count=4,
                    terminal_task_count=0,
                    root_wait_generation=1,
                    created_at=now,
                )
            )
            await session.flush()
        session.add(
            OrchestratorTaskModel(
                task_id=task_id,
                operation_id=str(uuid4()),
                plan_id=plan_id,
                task_ref=f"task-{task_id}",
                contract_id=contract_id,
                contract_revision=1,
                objective="access one resource",
                input_json=input_json or {},
                acceptance_criteria="resource is authorized",
                status="running",
                execution_generation=1,
                cancellation_epoch=0,
                active_execution_id=execution_id,
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            OrchestratorExecutionModel(
                execution_id=execution_id,
                task_id=task_id,
                instance_id=instance_id,
                generation=1,
                status="running",
                execution_timeout_seconds=600,
                cancellation_epoch=0,
                capacity_reserved=True,
                started_at=now,
                deadline_at=now + timedelta(minutes=10),
                created_at=now,
                updated_at=now,
            )
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_resource_http_upload_download_and_idempotent_conflict(
    resource_store,
) -> None:
    """真实执行身份通过双重凭据上传、读取、重放和隔离不同实例。"""
    session_factory, storage_root = resource_store
    run_id, plan_id, task_id, execution_id, instance_id = (
        f"id-{uuid4()}" for _ in range(5)
    )
    contract_id = f"id-{uuid4()}"
    await _seed_execution(
        session_factory,
        run_id=run_id,
        plan_id=plan_id,
        task_id=task_id,
        execution_id=execution_id,
        instance_id=instance_id,
        contract_id=contract_id,
    )
    repository = PostgresWorkerResourceRepository(session_factory, storage_root)
    service = WorkerResourceService(
        repository,
        WorkerExecutionGrantSigner(SecretStr("g" * 32)),
        max_bytes=16,
    )
    token = await service.issue_execution_grant(execution_id)
    protocol = WorkerProtocolHttp({"worker-service": "test-worker-token"})
    app = FastAPI()
    app.include_router(build_worker_resources_router(lambda: service, protocol), prefix="/api")
    install_worker_protocol_handlers(app)
    body = b"smallfile"
    resource_id = str(uuid4())
    sha256 = hashlib.sha256(body).hexdigest()
    headers = {
        "Authorization": "Bearer test-worker-token",
        "X-Athena-Protocol-Version": "1.0",
        "X-Athena-Execution-Grant": token,
        "Content-Type": "application/octet-stream",
        "Content-SHA256": sha256,
        "Idempotency-Key": resource_id,
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://athena.test"
    ) as client:
        created = await client.put(f"/api/worker-resources/{resource_id}", content=body, headers=headers)
        replay = await client.put(f"/api/worker-resources/{resource_id}", content=body, headers=headers)
        uppercase_id = resource_id.upper()
        uppercase_replay = await client.put(
            f"/api/worker-resources/{uppercase_id}",
            content=body,
            headers={**headers, "Idempotency-Key": uppercase_id},
        )
        unauthenticated_bad_path = await client.get("/api/worker-resources/not-a-uuid")
        authenticated_bad_path = await client.get(
            "/api/worker-resources/not-a-uuid",
            headers={
                "Authorization": "Bearer test-worker-token",
                "X-Athena-Protocol-Version": "1.0",
                "X-Athena-Execution-Grant": token,
            },
        )
        downloaded = await client.get(
            f"/api/worker-resources/{resource_id}",
            headers={
                "Authorization": "Bearer test-worker-token",
                "X-Athena-Protocol-Version": "1.0",
                "X-Athena-Execution-Grant": token,
            },
        )
        conflicting = await client.put(
            f"/api/worker-resources/{resource_id}",
            content=b"different",
            headers={**headers, "Content-SHA256": hashlib.sha256(b"different").hexdigest()},
        )

    other_run, other_plan, other_task, other_execution, other_instance = (
        f"id-{uuid4()}" for _ in range(5)
    )
    await _seed_execution(
        session_factory,
        run_id=other_run,
        plan_id=other_plan,
        task_id=other_task,
        execution_id=other_execution,
        instance_id=other_instance,
        contract_id=contract_id,
    )
    other_instance_token = await service.issue_execution_grant(other_execution)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://athena.test"
    ) as client:
        cross_run_read = await client.get(
            f"/api/worker-resources/{resource_id}",
            headers={
                "Authorization": "Bearer test-worker-token",
                "X-Athena-Protocol-Version": "1.0",
                "X-Athena-Execution-Grant": other_instance_token,
            },
        )
        cross_instance_replay = await client.put(
            f"/api/worker-resources/{resource_id}",
            content=body,
            headers={**headers, "X-Athena-Execution-Grant": other_instance_token},
        )

    assert created.status_code == 201
    assert replay.status_code == 200
    assert uppercase_replay.status_code == 200
    assert unauthenticated_bad_path.status_code == 401
    assert authenticated_bad_path.status_code == 400
    assert created.json()["resource_id"] == resource_id
    assert created.json()["sha256"] == sha256
    assert downloaded.status_code == 200
    assert downloaded.content == body
    assert downloaded.headers["content-sha256"] == sha256
    assert conflicting.status_code == 409
    assert cross_run_read.status_code == 403
    assert cross_instance_replay.status_code == 403
    async with session_factory() as session:
        metadata = await session.get(WorkerResourceModel, resource_id)
    assert metadata is not None
    assert metadata.execution_id == execution_id
    assert metadata.worker_instance_id == instance_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_grant_revocation_cross_task_reference_and_upload_limits(resource_store) -> None:
    """取消代次立即撤销 Grant，输入明确引用的同 Run 完成资源才可读。"""
    session_factory, storage_root = resource_store
    run_id = f"id-{uuid4()}"
    plan_id = f"id-{uuid4()}"
    contract_id = f"id-{uuid4()}"
    source_instance, reader_instance = f"id-{uuid4()}", f"id-{uuid4()}"
    source_task, source_execution = f"id-{uuid4()}", f"id-{uuid4()}"
    resource_id = str(uuid4())
    await _seed_execution(
        session_factory,
        run_id=run_id,
        plan_id=plan_id,
        task_id=source_task,
        execution_id=source_execution,
        instance_id=source_instance,
        contract_id=contract_id,
    )
    repository = PostgresWorkerResourceRepository(session_factory, storage_root)
    service = WorkerResourceService(
        repository,
        WorkerExecutionGrantSigner(SecretStr("r" * 32)),
        max_bytes=4,
    )
    source_token = await service.issue_execution_grant(source_execution)
    with pytest.raises(ValueError, match="too_large"):
        await service.put_resource(
            service_principal="worker-service",
            grant_token=source_token,
            resource_id=str(uuid4()),
            media_type="application/octet-stream",
            expected_sha256=hashlib.sha256(b"oversized").hexdigest(),
            file_name=None,
            chunks=_chunks(b"oversized"),
        )
    data = b"blob"
    write = await service.put_resource(
        service_principal="worker-service",
        grant_token=source_token,
        resource_id=resource_id,
        media_type="application/octet-stream",
        expected_sha256=hashlib.sha256(data).hexdigest(),
        file_name=None,
        chunks=_chunks(data),
    )
    async with session_factory() as session, session.begin():
        task = await session.get(OrchestratorTaskModel, source_task)
        execution = await session.get(OrchestratorExecutionModel, source_execution)
        assert task is not None and execution is not None
        task.status = "completed"
        task.output_json = {"resource_id": resource_id}
        execution.status = "completed"
        execution.capacity_reserved = False
        reader_task, reader_execution = f"id-{uuid4()}", f"id-{uuid4()}"
    await _seed_execution(
        session_factory,
        run_id=run_id,
        plan_id=plan_id,
        task_id=reader_task,
        execution_id=reader_execution,
        instance_id=reader_instance,
        contract_id=contract_id,
        input_json={"artifact": {"resource_id": resource_id}},
    )
    reader_token = await service.issue_execution_grant(reader_execution)
    claims = WorkerExecutionGrantSigner(SecretStr("r" * 32)).verify(reader_token)
    assert claims.read_resource_ids == (resource_id,)
    download = await service.get_resource(
        service_principal="worker-service",
        grant_token=reader_token,
        resource_id=resource_id,
    )
    assert download.reference == write.reference
    assert download.path.read_bytes() == data

    async with session_factory() as session, session.begin():
        reader = await session.get(OrchestratorTaskModel, reader_task)
        assert reader is not None
        reader.cancellation_epoch = 1
        reader.status = "cancelled"
    with pytest.raises(ValueError, match="scope_revoked"):
        await service.get_resource(
            service_principal="worker-service",
            grant_token=reader_token,
            resource_id=resource_id,
        )

    async with session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(WorkerResourceModel))
    assert count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_duplicate_cannot_remove_ready_blob_after_revocation(
    resource_store, monkeypatch
) -> None:
    """同内容重复上传在先请求发布后遇到撤销时必须保留 ready 资源。"""
    session_factory, storage_root = resource_store
    run_id, plan_id, task_id, execution_id, instance_id = (
        f"id-{uuid4()}" for _ in range(5)
    )
    contract_id = f"id-{uuid4()}"
    await _seed_execution(
        session_factory,
        run_id=run_id,
        plan_id=plan_id,
        task_id=task_id,
        execution_id=execution_id,
        instance_id=instance_id,
        contract_id=contract_id,
    )
    repository = PostgresWorkerResourceRepository(session_factory, storage_root)
    service = WorkerResourceService(
        repository,
        WorkerExecutionGrantSigner(SecretStr("c" * 32)),
        max_bytes=16,
    )
    token = await service.issue_execution_grant(execution_id)
    pending_seen = {"first": asyncio.Event(), "second": asyncio.Event()}
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    original_hook = repository._pending_record_committed

    async def hold_after_pending(resource_id: str) -> None:
        task_name = asyncio.current_task().get_name()
        if task_name in pending_seen:
            pending_seen[task_name].set()
            await (release_first if task_name == "first" else release_second).wait()
        await original_hook(resource_id)

    monkeypatch.setattr(repository, "_pending_record_committed", hold_after_pending)
    body = b"same-content"
    resource_id = str(uuid4())
    digest = hashlib.sha256(body).hexdigest()

    async def upload() -> object:
        return await service.put_resource(
            service_principal="worker-service",
            grant_token=token,
            resource_id=resource_id,
            media_type="application/octet-stream",
            expected_sha256=digest,
            file_name=None,
            chunks=_chunks(body),
        )

    first = asyncio.create_task(upload(), name="first")
    await pending_seen["first"].wait()
    second = asyncio.create_task(upload(), name="second")
    await pending_seen["second"].wait()
    release_first.set()
    first_result = await first
    async with session_factory() as session, session.begin():
        task = await session.get(OrchestratorTaskModel, task_id)
        assert task is not None
        task.status = "cancelled"
        task.cancellation_epoch += 1
    release_second.set()
    second_result = await second

    assert first_result.disposition == "created"
    assert second_result.disposition == "replayed"
    async with session_factory() as session:
        row = await session.get(WorkerResourceModel, resource_id)
    assert row is not None and row.status == "ready"
    assert repository._resource_path(resource_id).read_bytes() == body


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_status", ["pending", "ready"])
async def test_delayed_phase_two_cannot_mutate_reassigned_resource(
    resource_store, monkeypatch, replacement_status: str
) -> None:
    """旧请求重新读取记录后必须验证归属，不能触碰新执行的 pending 或 ready。"""
    session_factory, storage_root = resource_store
    contract_id = f"id-{uuid4()}"
    first_run, first_plan, first_task, first_execution, first_instance = (
        f"id-{uuid4()}" for _ in range(5)
    )
    second_run, second_plan, second_task, second_execution, second_instance = (
        f"id-{uuid4()}" for _ in range(5)
    )
    await _seed_execution(
        session_factory,
        run_id=first_run,
        plan_id=first_plan,
        task_id=first_task,
        execution_id=first_execution,
        instance_id=first_instance,
        contract_id=contract_id,
    )
    await _seed_execution(
        session_factory,
        run_id=second_run,
        plan_id=second_plan,
        task_id=second_task,
        execution_id=second_execution,
        instance_id=second_instance,
        contract_id=contract_id,
    )
    repository = PostgresWorkerResourceRepository(session_factory, storage_root)
    service = WorkerResourceService(
        repository,
        WorkerExecutionGrantSigner(SecretStr("o" * 32)),
        max_bytes=16,
    )
    first_token = await service.issue_execution_grant(first_execution)
    pending = asyncio.Event()
    release = asyncio.Event()
    original_hook = repository._pending_record_committed
    resource_id = str(uuid4())
    body = b"identical"
    digest = hashlib.sha256(body).hexdigest()

    async def pause_after_pending(candidate_id: str) -> None:
        if candidate_id == resource_id:
            pending.set()
            await release.wait()
        await original_hook(candidate_id)

    monkeypatch.setattr(repository, "_pending_record_committed", pause_after_pending)
    old_upload = asyncio.create_task(
        service.put_resource(
            service_principal="worker-service",
            grant_token=first_token,
            resource_id=resource_id,
            media_type="application/octet-stream",
            expected_sha256=digest,
            file_name=None,
            chunks=_chunks(body),
        )
    )
    await pending.wait()
    async with session_factory() as session, session.begin():
        task = await session.get(OrchestratorTaskModel, first_task)
        row = await session.get(WorkerResourceModel, resource_id)
        assert task is not None and row is not None
        task.status = "cancelled"
        task.cancellation_epoch += 1
        row.worker_instance_id = second_instance
        row.run_id = second_run
        row.plan_id = second_plan
        row.task_id = second_task
        row.execution_id = second_execution
        row.status = replacement_status
    if replacement_status == "ready":
        target = repository._resource_path(resource_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    release.set()

    with pytest.raises(ValueError, match="owner_scope_mismatch"):
        await old_upload
    async with session_factory() as session:
        row = await session.get(WorkerResourceModel, resource_id)
    assert row is not None
    assert row.execution_id == second_execution
    assert row.status == replacement_status
    if replacement_status == "ready":
        assert repository._resource_path(resource_id).read_bytes() == body


@pytest.mark.integration
@pytest.mark.asyncio
async def test_grant_expiring_after_validation_is_rechecked_before_publication(
    resource_store, monkeypatch
) -> None:
    """流式上传等待期间 Grant 过期时，最终提交不得发布资源。"""
    session_factory, storage_root = resource_store
    run_id, plan_id, task_id, execution_id, instance_id = (
        f"id-{uuid4()}" for _ in range(5)
    )
    contract_id = f"id-{uuid4()}"
    await _seed_execution(
        session_factory,
        run_id=run_id,
        plan_id=plan_id,
        task_id=task_id,
        execution_id=execution_id,
        instance_id=instance_id,
        contract_id=contract_id,
    )
    repository = PostgresWorkerResourceRepository(session_factory, storage_root)
    signer = WorkerExecutionGrantSigner(SecretStr("e" * 32))
    service = WorkerResourceService(repository, signer, max_bytes=16)
    scope = (await repository.load_execution_resource_context(execution_id)).scope
    token = signer.issue(
        replace(scope, expires_at=datetime.now(timezone.utc) + timedelta(milliseconds=300))
    )
    pending = asyncio.Event()
    release = asyncio.Event()
    original_hook = repository._pending_record_committed

    async def pause_after_pending(resource_id: str) -> None:
        pending.set()
        await release.wait()
        await original_hook(resource_id)

    monkeypatch.setattr(repository, "_pending_record_committed", pause_after_pending)
    body = b"expires"
    resource_id = str(uuid4())
    upload = asyncio.create_task(
        service.put_resource(
            service_principal="worker-service",
            grant_token=token,
            resource_id=resource_id,
            media_type="application/octet-stream",
            expected_sha256=hashlib.sha256(body).hexdigest(),
            file_name=None,
            chunks=_chunks(body),
        )
    )
    await pending.wait()
    await asyncio.sleep(0.4)
    release.set()
    with pytest.raises(ValueError, match="scope_inactive"):
        await upload
    async with session_factory() as session:
        row = await session.get(WorkerResourceModel, resource_id)
    assert row is None
    assert not repository._resource_path(resource_id).exists()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_revoked_cleanup_keeps_resource_id_locked_until_unlink_finishes(
    resource_store, monkeypatch
) -> None:
    """撤销清理完成前，另一 Execution 不能接管相同资源 ID。"""
    session_factory, storage_root = resource_store
    contract_id = f"id-{uuid4()}"
    first_run, first_plan, first_task, first_execution, first_instance = (
        f"id-{uuid4()}" for _ in range(5)
    )
    second_run, second_plan, second_task, second_execution, second_instance = (
        f"id-{uuid4()}" for _ in range(5)
    )
    await _seed_execution(
        session_factory,
        run_id=first_run,
        plan_id=first_plan,
        task_id=first_task,
        execution_id=first_execution,
        instance_id=first_instance,
        contract_id=contract_id,
    )
    await _seed_execution(
        session_factory,
        run_id=second_run,
        plan_id=second_plan,
        task_id=second_task,
        execution_id=second_execution,
        instance_id=second_instance,
        contract_id=contract_id,
    )
    repository = PostgresWorkerResourceRepository(session_factory, storage_root)
    service = WorkerResourceService(
        repository,
        WorkerExecutionGrantSigner(SecretStr("f" * 32)),
        max_bytes=16,
    )
    first_token = await service.issue_execution_grant(first_execution)
    second_token = await service.issue_execution_grant(second_execution)
    pending = asyncio.Event()
    continue_upload = asyncio.Event()
    unlink_started = asyncio.Event()
    continue_unlink = asyncio.Event()
    original_pending_hook = repository._pending_record_committed
    original_unlink = repository._unlink_resource_blob
    resource_id = str(uuid4())
    body = b"first-content"

    async def pause_pending(candidate_id: str) -> None:
        if candidate_id == resource_id:
            pending.set()
            await continue_upload.wait()
        await original_pending_hook(candidate_id)

    async def pause_unlink(path) -> None:
        unlink_started.set()
        await continue_unlink.wait()
        await original_unlink(path)

    monkeypatch.setattr(repository, "_pending_record_committed", pause_pending)
    monkeypatch.setattr(repository, "_unlink_resource_blob", pause_unlink)

    first_upload = asyncio.create_task(
        service.put_resource(
            service_principal="worker-service",
            grant_token=first_token,
            resource_id=resource_id,
            media_type="application/octet-stream",
            expected_sha256=hashlib.sha256(body).hexdigest(),
            file_name=None,
            chunks=_chunks(body),
        )
    )
    await pending.wait()
    async with session_factory() as session, session.begin():
        task = await session.get(OrchestratorTaskModel, first_task)
        assert task is not None
        task.status = "cancelled"
        task.cancellation_epoch += 1
    continue_upload.set()
    await unlink_started.wait()

    async def upload_second() -> object:
        return await service.put_resource(
            service_principal="worker-service",
            grant_token=second_token,
            resource_id=resource_id,
            media_type="application/octet-stream",
            expected_sha256=hashlib.sha256(b"second-content").hexdigest(),
            file_name=None,
            chunks=_chunks(b"second-content"),
        )

    second_upload = asyncio.create_task(upload_second())
    await asyncio.sleep(0.05)
    assert not second_upload.done()
    continue_unlink.set()
    with pytest.raises(ValueError, match="scope_inactive"):
        await first_upload
    result = await second_upload

    assert result.disposition == "created"
    assert repository._resource_path(resource_id).read_bytes() == b"second-content"


async def _chunks(value: bytes):
    """把字节切分成多块，验证仓储使用有界流式处理。"""
    for offset in range(0, len(value), 2):
        yield value[offset : offset + 2]
