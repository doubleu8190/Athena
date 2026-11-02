"""S10 注册中心管理查询的 PostgreSQL 验收。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from infrastructure.persistence.postgres.models import (
    AgentRunModel,
    OrchestratorExecutionModel,
    OrchestratorPlanModel,
    OrchestratorTaskModel,
    WorkerContractModel,
    WorkerInstanceModel,
)
from infrastructure.persistence.postgres.repositories.worker_registry_repository import (
    PostgresWorkerRegistryRepository,
)
from tests.integration.test_worker_orchestration_s2 import worker_store


@pytest.mark.integration
@pytest.mark.asyncio
async def test_registry_snapshot_includes_unavailable_disabled_and_full_instances(worker_store) -> None:
    """管理快照保留各种状态，并用有效 reservation 统计满载容量。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    contract_id = str(uuid4())
    available_id = str(uuid4())
    unavailable_id = str(uuid4())
    disabled_id = str(uuid4())
    run_id = str(uuid4())
    plan_id = str(uuid4())
    task_id = str(uuid4())
    execution_id = str(uuid4())

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
        session.add(
            WorkerContractModel(
                contract_id=contract_id,
                revision=1,
                can_do_json=["summarize"],
                cannot_do_json=["execute code"],
                input_schema_json={"type": "object", "properties": {"text": {"type": "string"}}},
                output_schema_json={"type": "object", "properties": {"summary": {"type": "string"}}},
                fingerprint_hash=str(uuid4()),
                created_at=now,
            )
        )
        for instance_id, instance_key, status, accepting, identity_valid in (
            (available_id, "worker-full", "ready", True, True),
            (unavailable_id, "worker-unavailable", "not_ready", False, False),
            (disabled_id, "worker-disabled", "disabled", False, True),
        ):
            session.add(
                WorkerInstanceModel(
                    instance_id=instance_id,
                    service_principal="worker-service",
                    instance_key=instance_key,
                    worker_name="Test Worker",
                    contract_id=contract_id,
                    contract_revision=1,
                    base_url="https://worker.example",
                    execution_timeout_seconds=45,
                    max_concurrency=1,
                    status=status,
                    accepting_tasks=accepting,
                    health_identity_valid=identity_valid,
                    consecutive_health_failures=2 if not identity_valid else 0,
                    consecutive_health_successes=0,
                    last_health_check_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
        session.add(
            OrchestratorPlanModel(
                plan_id=plan_id,
                run_id=run_id,
                sequence=1,
                idempotency_key="registry-test-plan",
                request_hash="registry-test-hash",
                goal="fill the worker",
                status="running",
                expected_task_count=1,
                terminal_task_count=0,
                root_wait_generation=0,
                created_at=now,
            )
        )
        await session.flush()
        session.add(
            OrchestratorTaskModel(
                task_id=task_id,
                operation_id=str(uuid4()),
                plan_id=plan_id,
                task_ref="registry-task",
                contract_id=contract_id,
                contract_revision=1,
                objective="test registry capacity",
                input_json={},
                acceptance_criteria="visible in registry",
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
                instance_id=available_id,
                generation=1,
                status="running",
                execution_timeout_seconds=45,
                cancellation_epoch=0,
                capacity_reserved=True,
                created_at=now,
                updated_at=now,
            )
        )

    snapshots = await PostgresWorkerRegistryRepository(session_factory).list_registry_instances()
    by_key = {snapshot.instance_key: snapshot for snapshot in snapshots}

    assert set(by_key) == {"worker-full", "worker-unavailable", "worker-disabled"}
    full = by_key["worker-full"]
    assert full.status == "ready"
    assert full.current_concurrency == 1
    assert full.available_concurrency == 0
    assert full.can_do == ("summarize",)
    assert full.output_schema["type"] == "object"
    assert by_key["worker-unavailable"].status == "not_ready"
    assert by_key["worker-unavailable"].health_identity_valid is False
    assert by_key["worker-disabled"].status == "disabled"
    assert by_key["worker-disabled"].last_health_check_at is None
