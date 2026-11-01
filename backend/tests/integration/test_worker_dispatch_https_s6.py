"""S6：通过真实 HTTPS Worker 验证批量审批暂停和恢复。"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import ssl
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from application.orchestration.worker_plan_submission import WorkerPlanSubmissionService
from domain.orchestration.worker_entities import WorkerPlanSubmission, WorkerTaskSpec
from infrastructure.persistence.postgres.models import (
    AgentRunModel,
    ApprovalRecordModel,
    Base,
    OrchestratorExecutionModel,
    OrchestratorPlanModel,
    OrchestratorTaskModel,
    OrchestratorWorkerCommandModel,
    WorkerInstanceModel,
    WorkerApprovalRequestModel,
    WorkerResourceModel,
)
from infrastructure.persistence.postgres.repositories.worker_orchestration_repository import (
    PostgresWorkerOrchestrationRepository,
)
from tests.integration.test_reference_worker_https import (
    _ORCHESTRATOR_TOKEN,
    _SERVICE_TOKEN,
    _create_test_certificates,
    _free_port,
    _terminate,
    _wait_until,
    _worker_process,
)
from tests.integration.test_worker_orchestration_s2 import _S2_TABLES


_DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest_asyncio.fixture
async def s6_worker_store():
    """在随机 PostgreSQL schema 创建本测试需要的表并在结束后删除。"""
    if not _DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"worker_dispatch_s6_{uuid4().hex}"
    base_url = make_url(_DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    admin_engine = create_async_engine(base_url)
    schema_url = base_url.update_query_dict({"options": f"-c search_path={schema}"})
    schema_engine = create_async_engine(schema_url)
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with schema_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[*_S2_TABLES, WorkerResourceModel.__table__],
                )
            )
        yield async_sessionmaker(schema_engine, expire_on_commit=False), schema, schema_url
    finally:
        await schema_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


@pytest.mark.parametrize("lose_resume_receipt", [False, True], ids=["normal", "resume-ack-lost"])
@pytest.mark.integration
@pytest.mark.asyncio
async def test_s6_batch_approval_pauses_and_resumes_real_worker(
    s6_worker_store, tmp_path: Path, lose_resume_receipt: bool
) -> None:
    """验证批量审批恢复；可选地丢失首个恢复回执并核对幂等重投。"""
    session_factory, schema, schema_url = s6_worker_store
    from tests.integration.worker_dispatch_https_s6_server import run_center, run_worker_proxy

    ca_file, certificate, private_key = _create_test_certificates(tmp_path)
    center_port, worker_port = _free_port(), _free_port()
    center_origin = f"https://127.0.0.1:{center_port}"
    worker_origin = f"https://127.0.0.1:{worker_port}"
    proxy_port = _free_port() if lose_resume_receipt else None
    dispatch_origin = (
        f"https://127.0.0.1:{proxy_port}" if proxy_port is not None else worker_origin
    )
    proxy_marker = tmp_path / "resume-proxy"
    data_dir = tmp_path / "worker-data"
    capability_file = tmp_path / "capability.json"
    capability_file.write_text(
        """{
          "can_do": ["Echo an object after approval decisions."],
          "cannot_do": ["Run arbitrary code."],
          "input_schema": {
            "type": "object",
            "properties": {
              "value": {"type": "string"},
              "approval_items": {
                "type": "array", "minItems": 1, "maxItems": 3,
                "items": {
                  "type": "object",
                  "properties": {
                    "tool_name": {"type": "string"},
                    "summary": {"type": "string"},
                    "risk_level": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                    "arguments": {"type": "object"}
                  },
                  "required": ["tool_name", "summary", "risk_level", "arguments"],
                  "additionalProperties": false
                }
              }
            },
            "required": ["value", "approval_items"],
            "additionalProperties": false
          },
          "output_schema": {
            "type": "object",
            "properties": {
              "echo": {"type": "object"},
              "decisions": {"type": "array", "items": {
                "type": "object",
                "properties": {
                  "item_id": {"type": "string"},
                  "decision": {"type": "string", "enum": ["approved", "denied", "expired"]}
                },
                "required": ["item_id", "decision"],
                "additionalProperties": false
              }}
            },
            "required": ["echo", "decisions"],
            "additionalProperties": false
          }
        }""",
        encoding="utf-8",
    )
    proxy = None
    if proxy_port is not None:
        proxy = multiprocessing.get_context("spawn").Process(
            target=run_worker_proxy,
            kwargs={
                "upstream_origin": worker_origin,
                "ca_file": str(ca_file),
                "certificate": str(certificate),
                "private_key": str(private_key),
                "marker_file": str(proxy_marker),
                "delay_seconds": 3.5,
                "delay_resume_first_response": True,
                "delay_first_task_response": False,
                "host": "127.0.0.1",
                "port": proxy_port,
            },
        )
    center = multiprocessing.get_context("spawn").Process(
        target=run_center,
        kwargs={
            "database_url": schema_url.render_as_string(hide_password=False),
            "schema": schema,
            "storage_root": str(tmp_path / "resources"),
            "service_token": _SERVICE_TOKEN,
            "orchestrator_token": _ORCHESTRATOR_TOKEN,
            "grant_secret": "s6-test-only-grant-secret-long-enough",
            "worker_origin": dispatch_origin,
            "ca_file": str(ca_file),
            "certificate": str(certificate),
            "private_key": str(private_key),
            "host": "127.0.0.1",
            "port": center_port,
        },
    )
    worker: subprocess.Popen[str] | None = None
    try:
        if proxy is not None:
            proxy.start()
        center.start()
        tls_context = ssl.create_default_context(cafile=str(ca_file))
        async with httpx.AsyncClient(verify=tls_context, timeout=5) as client:
            await _wait_until(lambda: _center_ready(client, center_origin), timeout=20)
            previous_execution_delay = os.environ.get(
                "ATHENA_REFERENCE_WORKER_EXECUTION_DELAY_SECONDS"
            )
            if lose_resume_receipt:
                # 保留恢复后的执行窗口，使首个 HTTP 202 超时后必然发生同命令重投。
                os.environ["ATHENA_REFERENCE_WORKER_EXECUTION_DELAY_SECONDS"] = "4"
            worker = _worker_process(
                base_url=center_origin,
                public_url=dispatch_origin,
                data_dir=data_dir,
                capability_file=capability_file,
                certificate=certificate,
                private_key=private_key,
                ca_file=ca_file,
                port=worker_port,
            )
            if lose_resume_receipt:
                if previous_execution_delay is None:
                    os.environ.pop("ATHENA_REFERENCE_WORKER_EXECUTION_DELAY_SECONDS", None)
                else:
                    os.environ[
                        "ATHENA_REFERENCE_WORKER_EXECUTION_DELAY_SECONDS"
                    ] = previous_execution_delay
            await _wait_until(lambda: _worker_ready(client, dispatch_origin), timeout=30)

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
            submission = WorkerPlanSubmission(
                run_id=run_id,
                plan_id=plan_id,
                sequence=1,
                idempotency_key=f"s6-plan:{plan_id}",
                goal="经过批量审批后回显输入",
                tasks=(
                    WorkerTaskSpec(
                        task_ref="approval-batch",
                        contract_id=contract_id,
                        contract_revision=1,
                        objective="请求审批并回显决定",
                        input={
                            "value": "s6-approval",
                            "approval_items": [
                                {"tool_name": "deploy-alpha", "summary": "Alpha", "risk_level": "high", "arguments": {"target": "alpha"}},
                                {"tool_name": "deploy-beta", "summary": "Beta", "risk_level": "medium", "arguments": {"target": "beta"}},
                                {"tool_name": "deploy-gamma", "summary": "Gamma", "risk_level": "low", "arguments": {"target": "gamma"}},
                            ],
                        },
                        acceptance_criteria="返回完整审批决定集合",
                    ),
                ),
                created_at=now,
            )
            plan_receipt = await WorkerPlanSubmissionService(repository).submit(submission)
            assert plan_receipt.accepted

            async def approvals_ready() -> bool:
                async with session_factory() as session:
                    rows = list(
                        (await session.scalars(
                            select(ApprovalRecordModel)
                            .where(ApprovalRecordModel.plan_id == plan_id)
                            .order_by(ApprovalRecordModel.tool_name)
                        )).all()
                    )
                return len(rows) == 3 and all(row.status == "pending" for row in rows)

            await _wait_until(approvals_ready, timeout=30)
            async with session_factory() as session:
                rows = list(
                    (await session.scalars(
                        select(ApprovalRecordModel)
                        .where(ApprovalRecordModel.plan_id == plan_id)
                        .order_by(ApprovalRecordModel.tool_name)
                    )).all()
                )
                approval_request = await session.scalar(
                    select(WorkerApprovalRequestModel).where(
                        WorkerApprovalRequestModel.plan_id == plan_id
                    )
                )
                execution = await session.get(
                    OrchestratorExecutionModel, rows[0].execution_id
                )
                task = await session.get(OrchestratorTaskModel, rows[0].task_id)
                assert execution is not None and task is not None
                paused_deadline = execution.deadline_at
                remaining_seconds = execution.remaining_seconds
                assert execution.status == "waiting_approval"
                assert paused_deadline is None
                assert remaining_seconds is not None and remaining_seconds > 0
                assert task.status == "waiting_approval"
                assert not execution.capacity_reserved
                approval_ids = [row.approval_id for row in rows]
                item_ids = [row.item_id for row in rows]
                assert approval_request is not None
                assert approval_request.status == "pending"
                assert approval_request.resume_command_id is None
                assert len(approval_request.request_json["items"]) == 3
                assert approval_request.expires_at > now
                paused_remaining_seconds = remaining_seconds

            # 扫描器至少运行数轮后，审批暂停仍不能被执行超时裁决。
            await asyncio.sleep(0.5)
            async with session_factory() as session:
                paused_again = await session.get(
                    OrchestratorExecutionModel, execution.execution_id
                )
                assert paused_again is not None
                assert paused_again.status == "waiting_approval"
                assert paused_again.deadline_at is None
                assert paused_again.remaining_seconds is not None
                assert paused_again.remaining_seconds > 0

            # 通过现有用户决议 API 写入前两项；批次不完整时不生成 ResumeCommand。
            for approval_id, decision in zip(approval_ids[:2], ("approved", "denied"), strict=True):
                response = await client.post(
                    f"{center_origin}/api/approvals/{approval_id}/decision",
                    json={"decision": decision},
                )
                assert response.status_code == 202
            async with session_factory() as session:
                resume_count = await session.scalar(
                    select(func.count(OrchestratorWorkerCommandModel.command_id)).where(
                        OrchestratorWorkerCommandModel.command_type == "resume",
                        OrchestratorWorkerCommandModel.execution_id == execution.execution_id,
                    )
                )
                assert resume_count == 0

            # 加速测试中的全局 120 秒过期：保留生产 expiry worker 和用户决议链路。
            async with session_factory() as session, session.begin():
                pending_row = await session.get(ApprovalRecordModel, approval_ids[2])
                assert pending_row is not None and pending_row.status == "pending"
                pending_row.expires_at = datetime.now(UTC) - timedelta(seconds=1)

            async def task_completed() -> bool:
                async with session_factory() as session:
                    task = await session.get(OrchestratorTaskModel, rows[0].task_id)
                    plan = await session.get(OrchestratorPlanModel, plan_id)
                return task is not None and task.status == "completed" and plan is not None and plan.status == "completed"

            async def resume_command_created() -> bool:
                async with session_factory() as session:
                    count = await session.scalar(
                        select(func.count(OrchestratorWorkerCommandModel.command_id)).where(
                            OrchestratorWorkerCommandModel.command_type == "resume",
                            OrchestratorWorkerCommandModel.execution_id == rows[0].execution_id,
                        )
                    )
                return count == 1

            try:
                await _wait_until(resume_command_created, timeout=8)
            except AssertionError:
                async with session_factory() as session:
                    approval_states = list(
                        (
                            await session.execute(
                                select(ApprovalRecordModel.item_id, ApprovalRecordModel.status)
                                .where(ApprovalRecordModel.plan_id == plan_id)
                                .order_by(ApprovalRecordModel.item_id)
                            )
                        ).all()
                    )
                    request_state = await session.scalar(
                        select(WorkerApprovalRequestModel.status).where(
                            WorkerApprovalRequestModel.plan_id == plan_id
                        )
                    )
                    command_count = await session.scalar(
                        select(func.count(OrchestratorWorkerCommandModel.command_id)).where(
                            OrchestratorWorkerCommandModel.command_type == "resume",
                            OrchestratorWorkerCommandModel.execution_id == rows[0].execution_id,
                        )
                    )
                raise AssertionError(
                    "resume command was not created; "
                    f"approval_states={approval_states}, request_state={request_state}, "
                    f"resume_command_count={command_count}"
                )
            try:
                await _wait_until(task_completed, timeout=20)
            except AssertionError:
                async with session_factory() as session:
                    command = await session.scalar(
                        select(OrchestratorWorkerCommandModel).where(
                            OrchestratorWorkerCommandModel.command_type == "resume",
                            OrchestratorWorkerCommandModel.execution_id == rows[0].execution_id,
                        )
                    )
                    current_task = await session.get(OrchestratorTaskModel, rows[0].task_id)
                command_summary = None
                worker_error = None
                if command is not None:
                    command_summary = {
                        "status": command.status,
                        "row_command_id": command.command_id,
                        "body_command_id": command.command_json.get("command_id"),
                        "keys": sorted(command.command_json),
                        "approval_batch_id": command.command_json.get("approval_batch_id"),
                        "decision_item_ids": [
                            item.get("item_id")
                            for item in command.command_json.get("decisions", [])
                            if isinstance(item, dict)
                        ],
                    }
                    async with httpx.AsyncClient(
                        verify=tls_context, timeout=5
                    ) as diagnostic_client:
                        diagnostic_response = await diagnostic_client.post(
                            f"{worker_origin}/worker-api/v1/executions/{rows[0].execution_id}/resume",
                            headers={
                                "Authorization": f"Bearer {_ORCHESTRATOR_TOKEN}",
                                "X-Athena-Protocol-Version": "1.0",
                                "Content-Type": "application/json; charset=utf-8",
                                "Idempotency-Key": str(command.command_json["command_id"]),
                            },
                            json=command.command_json,
                        )
                    worker_error = {
                        "status_code": diagnostic_response.status_code,
                        "body": diagnostic_response.json(),
                    }
                raise AssertionError(
                    f"task did not complete; task_status={None if current_task is None else current_task.status}; "
                    f"resume_command={command_summary}; worker_response={worker_error}"
                )
            if lose_resume_receipt:
                await _wait_until(
                    lambda: asyncio.to_thread(Path(str(proxy_marker) + ".resume-released").exists),
                    timeout=10,
                )
                attempts = sorted(tmp_path.glob("resume-proxy.resume-*.json"))
                assert len(attempts) >= 2
                attempt_payloads = [
                    json.loads(path.read_text(encoding="utf-8")) for path in attempts
                ]
                first_attempt = attempt_payloads[0]
                assert all(
                    attempt["idempotency_key"] == first_attempt["idempotency_key"]
                    and attempt["body"] == first_attempt["body"]
                    for attempt in attempt_payloads[1:]
                )
                assert all(
                    attempt["body"]["execution_grant"]
                    == first_attempt["body"]["execution_grant"]
                    for attempt in attempt_payloads[1:]
                )
                assert all(
                    attempt["body"]["command_id"] == first_attempt["body"]["command_id"]
                    for attempt in attempt_payloads[1:]
                )
            async with session_factory() as session:
                task = await session.get(OrchestratorTaskModel, rows[0].task_id)
                execution = await session.get(OrchestratorExecutionModel, rows[0].execution_id)
                resume_commands = list(
                    (await session.scalars(
                        select(OrchestratorWorkerCommandModel).where(
                            OrchestratorWorkerCommandModel.command_type == "resume",
                            OrchestratorWorkerCommandModel.execution_id == rows[0].execution_id,
                        )
                    )).all()
                )
                result = task.output_json if task is not None else None
            assert task is not None and task.status == "completed"
            assert execution is not None and execution.deadline_at is not None
            # 剩余时长作为暂停快照保留，用于审计并验证恢复没有重置计时。
            assert execution.remaining_seconds == pytest.approx(
                paused_remaining_seconds, abs=2
            )
            assert execution.capacity_reserved is False
            assert len(resume_commands) == 1
            assert resume_commands[0].status in {"accepted", "result_received"}
            if lose_resume_receipt:
                assert resume_commands[0].send_attempt_count >= 2
                assert resume_commands[0].status == "result_received"
            assert resume_commands[0].command_json["decisions"] == sorted(
                resume_commands[0].command_json["decisions"],
                key=lambda item: item["item_id"],
            )
            resumed_at = resume_commands[0].command_json["execution_grant"]["expires_at"]
            assert datetime.fromisoformat(resumed_at.replace("Z", "+00:00")) > now
            assert execution.started_at is not None
            assert (execution.deadline_at - execution.started_at).total_seconds() == pytest.approx(
                paused_remaining_seconds, abs=2
            )
            assert isinstance(result, dict)
            assert result["echo"]["value"] == "s6-approval"
            decisions = result["decisions"]
            assert {
                item["item_id"]: item["decision"] for item in decisions
            } == {
                item_ids[0]: "approved",
                item_ids[1]: "denied",
                item_ids[2]: "expired",
            }
            assert instance_id
    finally:
        if worker is not None:
            await asyncio.to_thread(_terminate, worker)
        if center.is_alive():
            center.terminate()
            center.join(timeout=5)
        if center.is_alive():
            center.kill()
            center.join(timeout=5)
        if proxy is not None and proxy.is_alive():
            proxy.terminate()
            proxy.join(timeout=5)
        if proxy is not None and proxy.is_alive():
            proxy.kill()
            proxy.join(timeout=5)


async def _center_ready(client: httpx.AsyncClient, origin: str) -> bool:
    try:
        return (await client.get(f"{origin}/openapi.json")).status_code == 200
    except httpx.HTTPError:
        return False


async def _worker_ready(client: httpx.AsyncClient, origin: str) -> bool:
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


__all__ = ["test_s6_batch_approval_pauses_and_resumes_real_worker"]
