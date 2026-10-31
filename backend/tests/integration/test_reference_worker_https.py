"""两个独立 TLS 进程与随机 PostgreSQL schema 的 S3 端到端验收。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from application.orchestration.worker_execution_grants import WorkerExecutionGrantSigner
from application.orchestration.worker_resources import WorkerResourceService
from application.orchestration.worker_plan_submission import WorkerPlanSubmissionService
from domain.orchestration.worker_entities import WorkerPlanSubmission, WorkerTaskSpec
from infrastructure.persistence.postgres.models import (
    AgentRunModel,
    Base,
    OrchestratorExecutionModel,
    OrchestratorPlanModel,
    OrchestratorTaskModel,
    OrchestratorWorkerCommandModel,
    OrchestratorRootResumeJobModel,
    WorkerInstanceModel,
    WorkerResourceModel,
    WorkerResultReceiptModel,
)
from infrastructure.persistence.postgres.repositories.worker_orchestration_repository import (
    PostgresWorkerOrchestrationRepository,
)
from infrastructure.persistence.postgres.repositories.worker_resource_repository import (
    PostgresWorkerResourceRepository,
)
from tests.integration.test_worker_orchestration_s2 import _S2_TABLES
from tests.integration.test_worker_resources_s3 import _seed_execution


_DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
_SERVICE_TOKEN = "reference-worker-test-service-token"
_ORCHESTRATOR_TOKEN = "reference-worker-test-orchestrator-token"
_GRANT_SECRET = "reference-worker-test-execution-grant-secret-32-bytes"
_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"resource": {"type": "object"}},
    "required": ["resource"],
    "additionalProperties": False,
}


@pytest_asyncio.fixture
async def https_worker_store(tmp_path):
    """只在临时 schema 创建协议所需实际表，并在用例结束时清理。"""
    if not _DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"reference_worker_https_{uuid4().hex}"
    base_url = make_url(_DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    admin_engine = create_async_engine(base_url)
    schema_url = base_url.update_query_dict(
        {"options": f"-c search_path={schema}"}
    )
    schema_engine = create_async_engine(schema_url)
    tables = [*_S2_TABLES, WorkerResourceModel.__table__]
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with schema_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection, tables=tables
                )
            )
        yield async_sessionmaker(schema_engine, expire_on_commit=False), schema, schema_url
    finally:
        await schema_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


def _free_port() -> int:
    """取得本机临时 TCP 端口。"""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _create_test_certificates(root: Path) -> tuple[Path, Path, Path]:
    """生成仅供验收使用的短期 CA 和 IP SAN HTTPS 证书。"""
    ca_key = root / "test-ca.key"
    ca_cert = root / "test-ca.crt"
    server_key = root / "server.key"
    server_csr = root / "server.csr"
    server_cert = root / "server.crt"
    extensions = root / "server.ext"
    extensions.write_text("subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\n")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(ca_key), "-out", str(ca_cert), "-subj",
            "/CN=Athena Worker Integration Test CA", "-days", "2",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        ],
        check=True, capture_output=True,
    )
    subprocess.run(
        [
            "openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout",
            str(server_key), "-out", str(server_csr), "-subj", "/CN=127.0.0.1",
        ],
        check=True, capture_output=True,
    )
    subprocess.run(
        [
            "openssl", "x509", "-req", "-in", str(server_csr), "-CA",
            str(ca_cert), "-CAkey", str(ca_key), "-CAcreateserial", "-out",
            str(server_cert), "-days", "2", "-extfile", str(extensions),
        ],
        check=True, capture_output=True,
    )
    return ca_cert, server_cert, server_key


async def _wait_until(predicate, *, timeout: float = 15.0) -> None:
    """在限定时间内轮询异步验收条件。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("integration condition did not become true before timeout")


def _worker_process(
    *,
    base_url: str,
    public_url: str,
    data_dir: Path,
    capability_file: Path,
    certificate: Path,
    private_key: Path,
    ca_file: Path,
    port: int,
) -> subprocess.Popen[str]:
    """启动真实 `python -m reference_worker` 子进程。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    worker_log = (data_dir / "worker.log").open("w", encoding="utf-8")
    environment = os.environ.copy()
    environment.update(
        {
            "ATHENA_REFERENCE_WORKER_ORCHESTRATOR_URL": base_url,
            "ATHENA_REFERENCE_WORKER_PUBLIC_BASE_URL": public_url,
            "ATHENA_REFERENCE_WORKER_SERVICE_TOKEN": _SERVICE_TOKEN,
            "ATHENA_REFERENCE_WORKER_ORCHESTRATOR_TOKEN": _ORCHESTRATOR_TOKEN,
            "ATHENA_REFERENCE_WORKER_DATA_DIR": str(data_dir),
            "ATHENA_REFERENCE_WORKER_CAPABILITY_FILE": str(capability_file),
            "ATHENA_REFERENCE_WORKER_TLS_CERTIFICATE": str(certificate),
            "ATHENA_REFERENCE_WORKER_TLS_PRIVATE_KEY": str(private_key),
            "ATHENA_REFERENCE_WORKER_CA_CERTIFICATE": str(ca_file),
            "ATHENA_REFERENCE_WORKER_HOST": "127.0.0.1",
            "ATHENA_REFERENCE_WORKER_PORT": str(port),
            "ATHENA_REFERENCE_WORKER_RESULT_RETRY_INITIAL_SECONDS": "5",
            "ATHENA_REFERENCE_WORKER_RESULT_RETRY_MAX_SECONDS": "10",
            "ATHENA_REFERENCE_WORKER_RESULT_RETRY_JITTER_RATIO": "0",
        }
    )
    return subprocess.Popen(
        [sys.executable, "-m", "reference_worker"],
        cwd=Path(__file__).parents[2],
        env=environment,
        stdout=worker_log,
        stderr=subprocess.STDOUT,
        text=True,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reference_worker_tls_registration_resource_restart_and_health(
    https_worker_store, tmp_path
) -> None:
    """真实 Worker 与真实中心路由经 TLS 完成收件、授权资源和重启恢复。"""
    session_factory, schema, schema_url = https_worker_store
    from tests.integration.reference_worker_https_server import run_center

    ca_file, certificate, private_key = _create_test_certificates(tmp_path)
    center_port, worker_port = _free_port(), _free_port()
    center_origin = f"https://127.0.0.1:{center_port}"
    worker_origin = f"https://127.0.0.1:{worker_port}"
    storage_root = tmp_path / "center-resources"
    data_dir = tmp_path / "worker-data"
    capability_file = tmp_path / "capability.json"
    capability_file.write_text(
        json.dumps(
            {
                "can_do": ["Read and copy one authorized resource."],
                "cannot_do": ["Run arbitrary code."],
                "input_schema": {
                    "type": "object",
                    "properties": {"resource_reference": {"type": "object"}},
                    "required": ["resource_reference"],
                    "additionalProperties": False,
                },
                "output_schema": _OUTPUT_SCHEMA,
            }
        ),
        encoding="utf-8",
    )
    center = multiprocessing.get_context("spawn").Process(
        target=run_center,
        kwargs={
            "database_url": schema_url.render_as_string(hide_password=False),
            "schema": schema,
            "storage_root": str(storage_root),
            "service_token": _SERVICE_TOKEN,
            "orchestrator_token": _ORCHESTRATOR_TOKEN,
            "grant_secret": _GRANT_SECRET,
            "worker_origin": worker_origin,
            "ca_file": str(ca_file),
            "certificate": str(certificate),
            "private_key": str(private_key),
            "host": "127.0.0.1",
            "port": center_port,
        },
    )
    worker: subprocess.Popen[str] | None = None
    try:
        center.start()
        center_probe_errors: list[str] = []
        tls_context = ssl.create_default_context(cafile=str(ca_file))
        async with httpx.AsyncClient(verify=tls_context, timeout=2) as client:
            try:
                await _wait_until(
                    lambda: _https_ready(client, center_origin, center_probe_errors),
                    timeout=20,
                )
            except AssertionError as exc:
                raise AssertionError(
                    "center HTTPS service did not become ready "
                    f"(alive={center.is_alive()}, exitcode={center.exitcode}, "
                    f"last_probe_error={center_probe_errors[-1:]})"
                ) from exc
        worker = _worker_process(
            base_url=center_origin,
            public_url=worker_origin,
            data_dir=data_dir,
            capability_file=capability_file,
            certificate=certificate,
            private_key=private_key,
            ca_file=ca_file,
            port=worker_port,
        )
        async with httpx.AsyncClient(verify=tls_context, timeout=3) as client:
            try:
                await _wait_until(
                    lambda: _worker_ready(client, worker_origin), timeout=30
                )
            except AssertionError as exc:
                worker_log = data_dir / "worker.log"
                tail = worker_log.read_text(encoding="utf-8")[-4000:]
                raise AssertionError(
                    f"Worker HTTPS service did not become ready; log tail:\n{tail}"
                ) from exc
            registration = await _registered_instance(session_factory)
            instance_id, instance_key, contract_id = registration
            registration_replay = await _replay_registration(
                client,
                center_origin,
                worker_origin,
                data_dir / "worker.sqlite3",
                instance_key,
                capability_file,
            )
            assert registration_replay.status_code == 200
            assert registration_replay.json()["worker_instance_key"] == instance_key

            command = _make_task_command(instance_key, contract_id)
            invalid_input = {**command, "input": {"unexpected": True}}
            wrong_identity = await client.post(
                f"{worker_origin}/worker-api/v1/tasks",
                headers=_worker_headers(command, token="wrong"),
                json=command,
            )
            rejected_input = await client.post(
                f"{worker_origin}/worker-api/v1/tasks",
                headers=_worker_headers(invalid_input),
                json=invalid_input,
            )
            assert wrong_identity.status_code == 401
            assert rejected_input.status_code == 422

            run_id, plan_id, task_id, execution_id = (str(uuid4()) for _ in range(4))
            source_resource_id = str(uuid4())
            source_bytes = b"bytes transferred over authenticated TLS"
            source_digest = hashlib.sha256(source_bytes).hexdigest()
            resource_reference = {
                "resource_id": source_resource_id,
                "media_type": "application/octet-stream",
                "size_bytes": len(source_bytes),
                "sha256": source_digest,
            }
            await _seed_execution(
                session_factory,
                run_id=run_id,
                plan_id=plan_id,
                task_id=task_id,
                execution_id=execution_id,
                instance_id=instance_id,
                contract_id=contract_id,
                input_json={"resource_reference": resource_reference},
                service_principal="worker-service",
            )
            async with session_factory() as session, session.begin():
                plan = await session.get(OrchestratorPlanModel, plan_id)
                assert plan is not None
                plan.expected_task_count = 1

            resource_service = WorkerResourceService(
                PostgresWorkerResourceRepository(session_factory, storage_root),
                WorkerExecutionGrantSigner(SecretStr(_GRANT_SECRET)),
                max_bytes=1_048_576,
            )
            bootstrap_grant = await resource_service.issue_execution_grant(execution_id)
            await resource_service.put_resource(
                service_principal="worker-service",
                grant_token=bootstrap_grant,
                resource_id=source_resource_id,
                media_type="application/octet-stream",
                expected_sha256=source_digest,
                file_name=None,
                chunks=_chunks(source_bytes),
            )
            unauthorized_resource = await client.get(
                f"{center_origin}/api/worker-resources/{source_resource_id}",
                headers={
                    "Authorization": f"Bearer {_SERVICE_TOKEN}",
                    "X-Athena-Protocol-Version": "1.0",
                    "X-Athena-Execution-Grant": "invalid-execution-grant",
                },
            )
            assert unauthorized_resource.status_code == 403
            execution_grant = await resource_service.issue_execution_grant(execution_id)
            grant_claims = WorkerExecutionGrantSigner(SecretStr(_GRANT_SECRET)).verify(
                execution_grant
            )
            command = _make_task_command(
                instance_key,
                contract_id,
                run_id=run_id,
                plan_id=plan_id,
                task_id=task_id,
                execution_id=execution_id,
                resource_reference=resource_reference,
                grant_token=execution_grant,
                grant_expires_at=grant_claims.expires_at.isoformat().replace("+00:00", "Z"),
            )
            task_headers = _worker_headers(command)
            accepted = await client.post(
                f"{worker_origin}/worker-api/v1/tasks",
                headers=task_headers,
                json=command,
            )
            replay = await client.post(
                f"{worker_origin}/worker-api/v1/tasks",
                headers=task_headers,
                json=command,
            )
            assert accepted.status_code == 202
            assert replay.status_code == 200
            assert replay.json()["receipt_id"] == accepted.json()["receipt_id"]

            await _wait_until(
                lambda: _execution_completed(session_factory, execution_id), timeout=30
            )
            await _wait_until(
                lambda: asyncio.to_thread(
                    _result_outbox_pending, data_dir / "worker.sqlite3"
                ),
                timeout=10,
            )
            assert _result_outbox_attempted(data_dir / "worker.sqlite3")
            _terminate(worker)
            worker = _worker_process(
                base_url=center_origin,
                public_url=worker_origin,
                data_dir=data_dir,
                capability_file=capability_file,
                certificate=certificate,
                private_key=private_key,
                ca_file=ca_file,
                port=worker_port,
            )
            await _wait_until(lambda: _worker_ready(client, worker_origin), timeout=30)
            replay_after_restart = await client.post(
                f"{worker_origin}/worker-api/v1/tasks",
                headers=task_headers,
                json=command,
            )
            assert replay_after_restart.status_code == 200
            assert replay_after_restart.json()["receipt_id"] == accepted.json()["receipt_id"]
            await _wait_until(
                lambda: asyncio.to_thread(
                    _result_outbox_delivered, data_dir / "worker.sqlite3"
                ),
                timeout=20,
            )
            await _wait_until(
                lambda: _resource_copy_exists(
                    session_factory, storage_root, source_resource_id, source_digest
                ),
                timeout=10,
            )
            await _terminate_async(worker)
            worker = None
            await _wait_until(
                lambda: _instance_not_ready(session_factory, instance_id), timeout=10
            )
    finally:
        if worker is not None:
            await _terminate_async(worker)
        if center.is_alive():
            center.terminate()
            center.join(timeout=5)
        if center.is_alive():
            center.kill()
            center.join(timeout=5)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dispatcher_runs_submitted_plan_over_tls_and_deduplicates_lost_result_ack(
    https_worker_store, tmp_path
) -> None:
    """从真实 Plan 提交经生产 Dispatcher 下发两个并发任务并收敛丢失回执。"""
    session_factory, schema, schema_url = https_worker_store
    from tests.integration.reference_worker_https_server import run_center

    ca_file, certificate, private_key = _create_test_certificates(tmp_path)
    center_port, worker_port = _free_port(), _free_port()
    center_origin = f"https://127.0.0.1:{center_port}"
    worker_origin = f"https://127.0.0.1:{worker_port}"
    data_dir = tmp_path / "worker-dispatch-data"
    capability_file = tmp_path / "dispatch-capability.json"
    capability_file.write_text(
        json.dumps(
            {
                "can_do": ["Echo one JSON object."],
                "cannot_do": ["Run arbitrary code."],
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
        ),
        encoding="utf-8",
    )
    center = multiprocessing.get_context("spawn").Process(
        target=run_center,
        kwargs={
            "database_url": schema_url.render_as_string(hide_password=False),
            "schema": schema,
            "storage_root": str(tmp_path / "dispatch-resources"),
            "service_token": _SERVICE_TOKEN,
            "orchestrator_token": _ORCHESTRATOR_TOKEN,
            "grant_secret": _GRANT_SECRET,
            "worker_origin": worker_origin,
            "ca_file": str(ca_file),
            "certificate": str(certificate),
            "private_key": str(private_key),
            "host": "127.0.0.1",
            "port": center_port,
        },
    )
    worker: subprocess.Popen[str] | None = None
    try:
        center.start()
        tls_context = ssl.create_default_context(cafile=str(ca_file))
        async with httpx.AsyncClient(verify=tls_context, timeout=3) as client:
            await _wait_until(lambda: _https_ready(client, center_origin, []), timeout=20)
            worker = _worker_process(
                base_url=center_origin,
                public_url=worker_origin,
                data_dir=data_dir,
                capability_file=capability_file,
                certificate=certificate,
                private_key=private_key,
                ca_file=ca_file,
                port=worker_port,
            )
            await _wait_until(lambda: _worker_ready(client, worker_origin), timeout=30)

        instance_id, instance_key, contract_id = await _registered_instance(session_factory)
        run_id, plan_id = str(uuid4()), str(uuid4())
        now = datetime.now(UTC)
        async with session_factory() as session, session.begin():
            session.add(
                AgentRunModel(
                    run_id=run_id,
                    session_id=f"session-{run_id}",
                    message_id=f"message-{run_id}",
                    sequence=1,
                    status="running",
                    request_json={},
                    root_thread_id=f"root-{run_id}",
                    root_wait_generation=0,
                    orchestrator_plan_sequence=0,
                    cancellation_epoch=0,
                    cancellation_external_effect_status="none",
                    created_at=now,
                    updated_at=now,
                )
            )
        repository = PostgresWorkerOrchestrationRepository(session_factory)
        submission_service = WorkerPlanSubmissionService(repository)
        accepted_plan = await submission_service.submit(
            WorkerPlanSubmission(
                run_id=run_id,
                plan_id=plan_id,
                sequence=1,
                idempotency_key=f"dispatch-plan:{plan_id}",
                goal="并行回显两个输入值",
                tasks=tuple(
                    WorkerTaskSpec(
                        task_ref=f"echo-{index}",
                        contract_id=contract_id,
                        contract_revision=1,
                        objective="回显输入对象",
                        input={"value": f"value-{index}"},
                        acceptance_criteria="输出 echo 与输入一致",
                    )
                    for index in range(2)
                ),
                created_at=now,
            )
        )
        assert accepted_plan.accepted and not accepted_plan.replayed

        async def tasks_completed() -> bool:
            async with session_factory() as session:
                statuses = list(
                    (
                        await session.scalars(
                            select(OrchestratorTaskModel.status).where(
                                OrchestratorTaskModel.plan_id == plan_id
                            )
                        )
                    ).all()
                )
            return statuses == ["completed", "completed"]

        await _wait_until(tasks_completed, timeout=30)
        async with session_factory() as session:
            commands = list(
                (
                    await session.scalars(
                        select(OrchestratorWorkerCommandModel).where(
                            OrchestratorWorkerCommandModel.command_type == "execute"
                        )
                    )
                ).all()
            )
            results = list((await session.scalars(select(WorkerResultReceiptModel))).all())
            resumes = list(
                (
                    await session.scalars(
                        select(OrchestratorRootResumeJobModel).where(
                            OrchestratorRootResumeJobModel.plan_id == plan_id
                        )
                    )
                ).all()
            )
            plan = await session.get(OrchestratorPlanModel, plan_id)
        assert len(commands) == 2
        assert all(command.status == "result_received" for command in commands)
        assert all(command.send_attempt_count == 1 for command in commands)
        assert len(results) == 2
        assert len(resumes) == 1
        assert plan is not None and plan.terminal_task_count == 2
        assert instance_id and instance_key

        await _wait_until(
            lambda: asyncio.to_thread(
                _result_outbox_attempted, data_dir / "worker.sqlite3"
            ),
            timeout=15,
        )
        await _wait_until(
            lambda: asyncio.to_thread(
                _result_outbox_delivered, data_dir / "worker.sqlite3"
            ),
            timeout=20,
        )
    finally:
        if worker is not None:
            await _terminate_async(worker)
        if center.is_alive():
            center.terminate()
            center.join(timeout=5)
        if center.is_alive():
            center.kill()
            center.join(timeout=5)


async def _https_ready(
    client: httpx.AsyncClient, origin: str, errors: list[str]
) -> bool:
    """以真实 CA 校验请求确认中心 TLS 服务就绪。"""
    try:
        response = await client.get(f"{origin}/openapi.json")
        return response.status_code == 200
    except httpx.HTTPError as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        return False


async def _worker_ready(client: httpx.AsyncClient, origin: str) -> bool:
    """读取 Worker 健康端点并等待注册完成。"""
    try:
        response = await client.get(
            f"{origin}/worker-api/v1/health",
            headers={
                "Authorization": f"Bearer {_ORCHESTRATOR_TOKEN}",
                "X-Athena-Protocol-Version": "1.0",
            },
        )
        return response.status_code == 200 and response.json().get("status") == "ready"
    except httpx.HTTPError:
        return False


async def _replay_registration(
    client: httpx.AsyncClient,
    center_origin: str,
    worker_origin: str,
    database_path: Path,
    instance_key: str,
    capability_file: Path,
) -> httpx.Response:
    """以 Worker 持久 request ID 重放真实注册并返回中心回执。"""
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT value_json FROM local_settings WHERE key='registration'"
        ).fetchone()
    assert row is not None
    registration = json.loads(row[0])
    request_id = registration["registration_request_id"]
    body = {
        "protocol_version": "1.0",
        "registration_request_id": request_id,
        "worker_instance_key": instance_key,
        "base_url": worker_origin,
        "execution_timeout_seconds": 300,
        "max_concurrency": 4,
        "capability": json.loads(capability_file.read_text(encoding="utf-8")),
    }
    return await client.post(
        f"{center_origin}/api/worker-instances",
        headers={
            "Authorization": f"Bearer {_SERVICE_TOKEN}",
            "X-Athena-Protocol-Version": "1.0",
            "Content-Type": "application/json; charset=utf-8",
            "Idempotency-Key": request_id,
        },
        json=body,
    )


async def _registered_instance(session_factory) -> tuple[str, str, str]:
    """读取 TLS 注册事务创建的实例键及冻结能力契约。"""
    async with session_factory() as session:
        row = await session.scalar(select(WorkerInstanceModel))
        assert row is not None
        return row.instance_id, row.instance_key, row.contract_id


def _make_task_command(
    instance_key: str,
    contract_id: str,
    *,
    run_id: str | None = None,
    plan_id: str | None = None,
    task_id: str | None = None,
    execution_id: str | None = None,
    resource_reference: dict[str, object] | None = None,
    grant_token: str = "opaque-test-grant-token",
    grant_expires_at: str = "2026-10-31T23:59:59Z",
) -> dict[str, object]:
    """构造一条 S1 TaskCommand 请求。"""
    return {
        "protocol_version": "1.0",
        "command_id": str(uuid4()),
        "run_id": run_id or str(uuid4()),
        "plan_id": plan_id or str(uuid4()),
        "task_id": task_id or str(uuid4()),
        "execution_id": execution_id or str(uuid4()),
        "generation": 1,
        "cancellation_epoch": 0,
        "operation_id": str(uuid4()),
        "contract_id": contract_id,
        "contract_revision": 1,
        "objective": "Fetch and copy the authorized resource bytes.",
        "acceptance_criteria": "Output contains the copied ResourceReference.",
        "input": {"resource_reference": resource_reference or {}},
        "execution_grant": {"token": grant_token, "expires_at": grant_expires_at},
    }


def _worker_headers(command: dict[str, object], *, token: str = _ORCHESTRATOR_TOKEN) -> dict[str, str]:
    """为 Worker 收件请求绑定 S1 身份和幂等键。"""
    return {
        "Authorization": f"Bearer {token}",
        "X-Athena-Protocol-Version": "1.0",
        "Content-Type": "application/json; charset=utf-8",
        "Idempotency-Key": str(command["command_id"]),
    }


async def _chunks(body: bytes):
    """提供一次性资源上传字节流。"""
    yield body


async def _execution_completed(session_factory, execution_id: str) -> bool:
    async with session_factory() as session:
        row = await session.get(OrchestratorExecutionModel, execution_id)
        return row is not None and row.status == "completed"


async def _resource_copy_exists(
    session_factory, storage_root: Path, source_id: str, digest: str
) -> bool:
    async with session_factory() as session:
        rows = list((await session.scalars(select(WorkerResourceModel))).all())
        copies = [row for row in rows if row.resource_id != source_id]
        if not copies:
            return False
        copy = copies[0]
        if copy.sha256 != digest:
            return False
        content = (storage_root / copy.resource_id[:2] / copy.resource_id).read_bytes()
        return hashlib.sha256(content).hexdigest() == digest


def _result_outbox_pending(database_path: Path) -> bool:
    if not database_path.is_file():
        return False
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT 1 FROM outbox WHERE kind='result' AND state='pending' LIMIT 1"
        ).fetchone()
        return row is not None


def _result_outbox_attempted(database_path: Path) -> bool:
    """确认丢失回执后结果已至少投递一次且仍待确认。"""
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT 1 FROM outbox WHERE kind='result' AND state='pending' AND attempt>=1 LIMIT 1"
        ).fetchone()
        return row is not None


def _result_outbox_delivered(database_path: Path) -> bool:
    if not database_path.is_file():
        return False
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT 1 FROM outbox WHERE kind='result' AND state='delivered' LIMIT 1"
        ).fetchone()
        return row is not None


async def _instance_not_ready(session_factory, instance_id: str) -> bool:
    async with session_factory() as session:
        row = await session.get(WorkerInstanceModel, instance_id)
        return row is not None and row.status == "not_ready" and not row.accepting_tasks


def _terminate(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


async def _terminate_async(process: subprocess.Popen[str]) -> None:
    await asyncio.to_thread(_terminate, process)
