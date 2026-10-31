"""S4：真实双 HTTPS 进程经 Dispatcher 完成一轮并发 Plan。"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import json
import sqlite3
import ssl
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
import httpx
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from application.orchestration.worker_plan_submission import WorkerPlanSubmissionService
from domain.orchestration.worker_entities import WorkerPlanSubmission, WorkerTaskSpec
from infrastructure.persistence.postgres.models import (
    AgentRunModel,
    Base,
    OrchestratorExecutionModel,
    OrchestratorPlanModel,
    OrchestratorRootResumeJobModel,
    OrchestratorTaskModel,
    OrchestratorWorkerCommandModel,
    WorkerInstanceModel,
    WorkerResultReceiptModel,
    WorkerResourceModel,
)
from infrastructure.persistence.postgres.repositories.worker_orchestration_repository import (
    PostgresWorkerOrchestrationRepository,
)

from tests.integration.test_reference_worker_https import (
    _GRANT_SECRET,
    _ORCHESTRATOR_TOKEN,
    _SERVICE_TOKEN,
    _create_test_certificates,
    _free_port,
    _result_outbox_delivered,
    _wait_until,
    _worker_process,
)
from tests.integration.test_worker_orchestration_s2 import _S2_TABLES


_DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest_asyncio.fixture
async def s4_worker_store(tmp_path: Path):
    """只在随机 PostgreSQL schema 建立 S4 所需表，并在结束时删除 schema。"""
    if not _DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"worker_dispatch_s4_{uuid4().hex}"
    base_url = make_url(_DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    admin_engine = create_async_engine(base_url)
    schema_url = base_url.update_query_dict({"options": f"-c search_path={schema}"})
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_s4_dispatches_two_tasks_over_tls_and_keeps_terminal_state_after_lost_ack(
    s4_worker_store, tmp_path: Path
) -> None:
    """提交一轮双任务 Plan，验证 TLS 派发、结果重投和终态幂等性。"""
    session_factory, schema, schema_url = s4_worker_store
    from tests.integration.worker_dispatch_https_s4_server import run_center, run_worker_proxy

    ca_file, certificate, private_key = _create_test_certificates(tmp_path)
    center_port, worker_port, proxy_port = _free_port(), _free_port(), _free_port()
    center_origin = f"https://127.0.0.1:{center_port}"
    proxy_origin = f"https://127.0.0.1:{proxy_port}"
    worker_upstream_origin = f"https://127.0.0.1:{worker_port}"
    task_ack_marker = tmp_path / "delayed-task-ack"
    data_dir = tmp_path / "worker-data"
    capability_file = tmp_path / "capability.json"
    capability_file.write_text(
        """{
          "can_do": ["Echo one JSON object."],
          "cannot_do": ["Run arbitrary code."],
          "input_schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": false
          },
          "output_schema": {
            "type": "object",
            "properties": {"echo": {"type": "object"}},
            "required": ["echo"],
            "additionalProperties": false
          }
        }""",
        encoding="utf-8",
    )
    center = multiprocessing.get_context("spawn").Process(
        target=run_center,
        kwargs={
            "database_url": schema_url.render_as_string(hide_password=False),
            "schema": schema,
            "storage_root": str(tmp_path / "resources"),
            "service_token": _SERVICE_TOKEN,
            "orchestrator_token": _ORCHESTRATOR_TOKEN,
            "grant_secret": _GRANT_SECRET,
            "worker_origin": proxy_origin,
            "ca_file": str(ca_file),
            "certificate": str(certificate),
            "private_key": str(private_key),
            "host": "127.0.0.1",
            "port": center_port,
        },
    )
    worker: subprocess.Popen[str] | None = None
    proxy: multiprocessing.context.SpawnProcess | None = None
    try:
        center.start()
        tls_context = ssl.create_default_context(cafile=str(ca_file))
        async with _http_client(tls_context) as client:
            await _wait_until(
                lambda: _center_ready(client, center_origin), timeout=20
            )
            previous_delay = os.environ.get(
                "ATHENA_REFERENCE_WORKER_EXECUTION_DELAY_SECONDS"
            )
            os.environ["ATHENA_REFERENCE_WORKER_EXECUTION_DELAY_SECONDS"] = "4"
            try:
                worker = _worker_process(
                    base_url=center_origin,
                    public_url=proxy_origin,
                    data_dir=data_dir,
                    capability_file=capability_file,
                    certificate=certificate,
                    private_key=private_key,
                    ca_file=ca_file,
                    port=worker_port,
                )
            finally:
                if previous_delay is None:
                    os.environ.pop("ATHENA_REFERENCE_WORKER_EXECUTION_DELAY_SECONDS", None)
                else:
                    os.environ["ATHENA_REFERENCE_WORKER_EXECUTION_DELAY_SECONDS"] = previous_delay
            proxy = multiprocessing.get_context("spawn").Process(
                target=run_worker_proxy,
                kwargs={
                    "upstream_origin": worker_upstream_origin,
                    "ca_file": str(ca_file),
                    "certificate": str(certificate),
                    "private_key": str(private_key),
                    "marker_file": str(task_ack_marker),
                    "delay_seconds": 8.0,
                    "host": "127.0.0.1",
                    "port": proxy_port,
                },
            )
            proxy.start()
            await _wait_until(
                lambda: _worker_ready(client, proxy_origin), timeout=30
            )

        instance_id, contract_id = await _registered_instance(session_factory)
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
        receipt = await WorkerPlanSubmissionService(repository).submit(
            WorkerPlanSubmission(
                run_id=run_id,
                plan_id=plan_id,
                sequence=1,
                idempotency_key=f"s4-plan:{plan_id}",
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
        assert receipt.accepted and not receipt.replayed

        async def completed() -> bool:
            async with session_factory() as session:
                plan = await session.get(OrchestratorPlanModel, plan_id)
                statuses = list(
                    (
                        await session.scalars(
                            select(OrchestratorTaskModel.status).where(
                                OrchestratorTaskModel.plan_id == plan_id
                            ).order_by(OrchestratorTaskModel.task_ref)
                        )
                    ).all()
                )
            return (
                plan is not None
                and plan.status == "completed"
                and plan.terminal_task_count == 2
                and statuses == ["completed", "completed"]
            )

        await _wait_until(lambda: asyncio.to_thread(Path(str(task_ack_marker) + ".received").is_file), timeout=30)
        # 代理尚未释放 TaskCommand 202；先要求结果已由另一个 TLS 请求提交并被中心接纳。
        await _wait_until(completed, timeout=7)
        async with session_factory() as session:
            commands = list(
                (
                    await session.scalars(
                        select(OrchestratorWorkerCommandModel).where(
                            OrchestratorWorkerCommandModel.execution_id.in_(
                                select(OrchestratorExecutionModel.execution_id).join(
                                    OrchestratorTaskModel,
                                    OrchestratorTaskModel.task_id
                                    == OrchestratorExecutionModel.task_id,
                                ).where(OrchestratorTaskModel.plan_id == plan_id)
                            ),
                            OrchestratorWorkerCommandModel.command_type == "execute",
                        )
                    )
                )
            )
            results = list(
                (
                    await session.scalars(
                        select(WorkerResultReceiptModel).join(
                            OrchestratorTaskModel,
                            OrchestratorTaskModel.task_id == WorkerResultReceiptModel.task_id,
                        ).where(OrchestratorTaskModel.plan_id == plan_id)
                    )
                ).all()
            )
            resumes = list(
                (
                    await session.scalars(
                        select(OrchestratorRootResumeJobModel).where(
                            OrchestratorRootResumeJobModel.plan_id == plan_id
                        )
                    )
                ).all()
            )
            executions = list(
                (
                    await session.scalars(
                        select(OrchestratorExecutionModel).join(
                            OrchestratorTaskModel,
                            OrchestratorTaskModel.task_id
                            == OrchestratorExecutionModel.task_id,
                        ).where(OrchestratorTaskModel.plan_id == plan_id)
                    )
                ).all()
            )
            tasks = list(
                (
                    await session.scalars(
                        select(OrchestratorTaskModel).where(
                            OrchestratorTaskModel.plan_id == plan_id
                        )
                    )
                ).all()
            )
        assert len(tasks) == len({task.task_id for task in tasks}) == 2
        assert len(commands) == 2
        assert all(command.status == "result_received" for command in commands)
        assert max(command.send_attempt_count for command in commands) >= 2
        assert all(command.send_attempt_count >= 1 for command in commands)
        assert len(results) == 2
        assert len(resumes) == 1
        assert all(not execution.capacity_reserved for execution in executions)
        assert instance_id
        worker_receipts = await asyncio.to_thread(
            _worker_task_receipts, data_dir / "worker.sqlite3"
        )
        assert len(worker_receipts) == 2
        assert {item[0] for item in worker_receipts} == {
            command.command_id for command in commands
        }
        assert all(item[1] and item[2] == "accepted" for item in worker_receipts)

        assert not Path(str(task_ack_marker) + ".released").exists()
        await _wait_until(
            lambda: asyncio.to_thread(Path(str(task_ack_marker) + ".released").is_file),
            timeout=5,
        )
        await asyncio.sleep(0.5)

        # 结果回执第一次被中心丢弃，Worker 必须重投；重复回执不得新增任务或回退终态。
        await _wait_until(
            lambda: asyncio.to_thread(_result_outbox_delivered, data_dir / "worker.sqlite3"),
            timeout=20,
        )
        async with session_factory() as session:
            final_statuses = list(
                (
                    await session.scalars(
                        select(OrchestratorTaskModel.status).where(
                            OrchestratorTaskModel.plan_id == plan_id
                        ).order_by(OrchestratorTaskModel.task_ref)
                    )
                ).all()
            )
            final_result_count = len(
                (
                    await session.scalars(
                        select(WorkerResultReceiptModel).join(
                            OrchestratorTaskModel,
                            OrchestratorTaskModel.task_id == WorkerResultReceiptModel.task_id,
                        ).where(OrchestratorTaskModel.plan_id == plan_id)
                    )
                ).all()
            )
        assert final_statuses == ["completed", "completed"]
        assert final_result_count == 2
    finally:
        if worker is not None:
            await asyncio.to_thread(_terminate, worker)
        if proxy is not None and proxy.is_alive():
            proxy.terminate()
            proxy.join(timeout=5)
        if center.is_alive():
            center.terminate()
            center.join(timeout=5)
        if center.is_alive():
            center.kill()
            center.join(timeout=5)


class _http_client:
    """延迟导入 httpx，避免测试未配置环境时初始化客户端。"""

    def __init__(self, verify: ssl.SSLContext):
        self._verify = verify
        self._client = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(verify=self._verify, timeout=3)
        return await self._client.__aenter__()

    async def __aexit__(self, *args):
        return await self._client.__aexit__(*args)


async def _center_ready(client, origin: str) -> bool:
    try:
        return (await client.get(f"{origin}/openapi.json")).status_code == 200
    except httpx.HTTPError:
        return False


async def _worker_ready(client, origin: str) -> bool:
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


async def _registered_instance(session_factory) -> tuple[str, str]:
    async with session_factory() as session:
        row = await session.scalar(select(WorkerInstanceModel))
        assert row is not None
        return row.instance_id, row.contract_id


def _terminate(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _worker_task_receipts(database_path: Path) -> list[tuple[str, dict[str, object], str]]:
    """读取 Worker SQLite 中持久化的任务命令身份和首次收件回执。"""
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT command_id,receipt_json,state FROM protocol_commands "
            "WHERE command_type='task' ORDER BY command_id"
        ).fetchall()
    return [(row[0], json.loads(row[1]), row[2]) for row in rows]


__all__ = ["test_s4_dispatches_two_tasks_over_tls_and_keeps_terminal_state_after_lost_ack"]
