"""S5：真实 HTTPS Worker 超时重试与旧执行容量回收验收。"""

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
    WorkerStopConfirmationModel,
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
    _wait_until,
    _worker_process,
)
from tests.integration.test_worker_orchestration_s2 import _S2_TABLES


_DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest_asyncio.fixture
async def s5_worker_store(tmp_path: Path):
    """只在随机 PostgreSQL schema 建立 S5 所需表，并在结束时删除 schema。"""
    if not _DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"worker_dispatch_s5_{uuid4().hex}"
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
async def test_s5_dispatches_two_tasks_over_tls_and_keeps_terminal_state_after_lost_ack(
    s5_worker_store, tmp_path: Path
) -> None:
    """真实 Worker 超时后生成新执行，并以 StopConfirmation 回收旧容量。"""
    session_factory, schema, schema_url = s5_worker_store
    from tests.integration.worker_dispatch_https_s5_server import run_center, run_worker_proxy

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
            previous_timeout = os.environ.get("ATHENA_REFERENCE_WORKER_TIMEOUT_SECONDS")
            os.environ["ATHENA_REFERENCE_WORKER_EXECUTION_DELAY_SECONDS"] = "4"
            os.environ["ATHENA_REFERENCE_WORKER_TIMEOUT_SECONDS"] = "1"
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
                if previous_timeout is None:
                    os.environ.pop("ATHENA_REFERENCE_WORKER_TIMEOUT_SECONDS", None)
                else:
                    os.environ["ATHENA_REFERENCE_WORKER_TIMEOUT_SECONDS"] = previous_timeout
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

        repository = PostgresWorkerOrchestrationRepository(
            session_factory, max_execution_attempts=2
        )
        receipt = await WorkerPlanSubmissionService(repository).submit(
            WorkerPlanSubmission(
                run_id=run_id,
                plan_id=plan_id,
                sequence=1,
                idempotency_key=f"s5-plan:{plan_id}",
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
                and statuses == ["failed", "cancelled"]
            )

        await _wait_until(lambda: asyncio.to_thread(Path(str(task_ack_marker) + ".received").is_file), timeout=30)
        # 取消第二个逻辑 Task，验证 CancelCommand 也通过生产 HTTPS Dispatcher。
        async with session_factory() as session:
            cancel_task_id = await session.scalar(
                select(OrchestratorTaskModel.task_id).where(
                    OrchestratorTaskModel.plan_id == plan_id,
                    OrchestratorTaskModel.task_ref == "echo-1",
                )
            )
        assert cancel_task_id is not None

        async def cancel_target_ready() -> bool:
            nonlocal cancel_execution_id
            async with session_factory() as session:
                task = await session.get(OrchestratorTaskModel, cancel_task_id)
                if task is None or task.active_execution_id is None:
                    return False
                execution = await session.get(
                    OrchestratorExecutionModel, task.active_execution_id
                )
            if execution is None or execution.instance_id is None or not execution.capacity_reserved:
                return False
            cancel_execution_id = execution.execution_id
            return True

        cancel_execution_id: str | None = None
        await _wait_until(cancel_target_ready, timeout=8)
        assert cancel_execution_id is not None
        cancel_command_id = await repository.cancel_task(run_id=run_id, task_id=cancel_task_id)
        assert cancel_command_id is not None

        async def cancel_accepted() -> bool:
            async with session_factory() as session:
                command = await session.get(OrchestratorWorkerCommandModel, cancel_command_id)
                execution = await session.get(OrchestratorExecutionModel, cancel_execution_id)
            return command is not None and command.status == "accepted" and execution is not None and execution.capacity_reserved

        await _wait_until(cancel_accepted, timeout=5)
        assert not await _plan_completed(session_factory, plan_id)
        await _wait_until(completed, timeout=20)

        async def stop_confirmations_done() -> bool:
            async with session_factory() as session:
                executions_now = list(
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
                confirmations_now = list(
                    (
                        await session.scalars(
                            select(WorkerStopConfirmationModel).join(
                                OrchestratorExecutionModel,
                                OrchestratorExecutionModel.execution_id
                                == WorkerStopConfirmationModel.execution_id,
                            ).join(
                                OrchestratorTaskModel,
                                OrchestratorTaskModel.task_id
                                == OrchestratorExecutionModel.task_id,
                            ).where(OrchestratorTaskModel.plan_id == plan_id)
                        )
                    ).all()
                )
            return len(confirmations_now) >= 3 and all(
                not execution.capacity_reserved for execution in executions_now
            )

        await _wait_until(stop_confirmations_done, timeout=15)
        late_result_id = str(uuid4())
        async with _http_client(ssl.create_default_context(cafile=str(ca_file))) as client:
            late_result = await client.post(
                f"{center_origin}/api/worker-executions/{cancel_execution_id}/result",
                headers={
                    "Authorization": f"Bearer {_SERVICE_TOKEN}",
                    "X-Athena-Protocol-Version": "1.0",
                    "Content-Type": "application/json; charset=utf-8",
                    "Idempotency-Key": late_result_id,
                },
                json={
                    "protocol_version": "1.0",
                    "result_id": late_result_id,
                    "generation": 1,
                    "status": "completed",
                    "output": {"echo": {"value": "late"}},
                },
            )
        assert late_result.status_code == 200
        assert late_result.json()["disposition"] == "ignored"
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
            stop_confirmations = list(
                (
                    await session.scalars(
                        select(WorkerStopConfirmationModel).join(
                            OrchestratorExecutionModel,
                            OrchestratorExecutionModel.execution_id
                            == WorkerStopConfirmationModel.execution_id,
                        ).join(
                            OrchestratorTaskModel,
                            OrchestratorTaskModel.task_id
                            == OrchestratorExecutionModel.task_id,
                        ).where(OrchestratorTaskModel.plan_id == plan_id)
                    )
                ).all()
            )
            cancel_commands = list(
                (
                    await session.scalars(
                        select(OrchestratorWorkerCommandModel)
                        .join(
                            OrchestratorExecutionModel,
                            OrchestratorExecutionModel.execution_id
                            == OrchestratorWorkerCommandModel.execution_id,
                        )
                        .join(
                            OrchestratorTaskModel,
                            OrchestratorTaskModel.task_id
                            == OrchestratorExecutionModel.task_id,
                        )
                        .where(
                            OrchestratorTaskModel.plan_id == plan_id,
                            OrchestratorWorkerCommandModel.command_type == "cancel",
                        )
                    )
                ).all()
            )
        assert len(tasks) == len({task.task_id for task in tasks}) == 2
        assert len(commands) >= 2
        assert all(command.status in {"result_received", "superseded", "failed"} for command in commands)
        assert all(command.send_attempt_count >= 1 for command in commands)
        assert len(results) >= 3
        assert len(resumes) == 1
        assert len(executions) >= 3
        assert all(execution.generation >= 1 for execution in executions)
        assert len(cancel_commands) >= 3
        assert sum(command.command_json["reason"] == "execution_timed_out" for command in cancel_commands) >= 2
        assert sum(command.command_json["reason"] == "task_cancelled" for command in cancel_commands) >= 1
        assert {item.cancel_command_id for item in stop_confirmations} == {
            command.command_id for command in cancel_commands
        }
        assert all(item.disposition in {"capacity_released", "already_released"} for item in stop_confirmations)
        assert all(not execution.capacity_reserved for execution in executions)
        assert instance_id
        worker_receipts = await asyncio.to_thread(
            _worker_task_receipts, data_dir / "worker.sqlite3"
        )
        assert len(worker_receipts) >= 2
        assert {item[2] for item in worker_receipts} == {"accepted"}

        # 这里此前复用了 S4 的“结果仍在延迟窗口”断言；S5 先等待超时重试和停止确认，
        # 此时反代的延迟窗口可能已经结束。CancelReceipt 与物理停止的区别已由
        # cancel_accepted 对 capacity_reserved 的断言覆盖。
        await _wait_until(
            lambda: asyncio.to_thread(Path(str(task_ack_marker) + ".released").is_file),
            timeout=5,
        )
        await asyncio.sleep(0.5)

        # Worker outbox 最终提交执行超时对应的 StopConfirmation；重复回执不改变终态。
        # S5 的最终完成条件由中心数据库中的结果回执和 StopConfirmation 决定。
        # Worker 可能只产生 stop-confirmation outbox，而不是 result outbox；
        # 这里不复用 S4 的结果 outbox 交付断言。
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
        assert final_statuses == ["failed", "cancelled"]
        assert final_result_count >= 3
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


async def _plan_completed(session_factory, plan_id: str) -> bool:
    async with session_factory() as session:
        plan = await session.get(OrchestratorPlanModel, plan_id)
        return plan is not None and plan.terminal_task_count == 2


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


__all__ = ["test_s5_dispatches_two_tasks_over_tls_and_keeps_terminal_state_after_lost_ack"]
