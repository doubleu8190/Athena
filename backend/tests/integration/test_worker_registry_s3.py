"""S3 Worker 注册健康状态的 PostgreSQL 验收。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from domain.orchestration.worker_registry import WorkerHealthProbeResult
from application.orchestration.worker_registration import WorkerRegistrationService
from infrastructure.persistence.postgres.models import WorkerContractModel, WorkerInstanceModel
from infrastructure.persistence.postgres.repositories.worker_orchestration_repository import (
    PostgresWorkerOrchestrationRepository,
)
from infrastructure.persistence.postgres.repositories.worker_registry_repository import (
    PostgresWorkerRegistryRepository,
)
from tests.integration.test_worker_orchestration_s2 import worker_store


@pytest.mark.integration
@pytest.mark.asyncio
async def test_health_failure_threshold_and_recovery_require_consecutive_successes(worker_store) -> None:
    """瞬态失败保留 READY；摘除后须连续两次 ready 才重新可用。"""
    session_factory, _ = worker_store
    repository = PostgresWorkerRegistryRepository(session_factory)
    now = datetime.now(timezone.utc)
    instance_id = str(uuid4())
    contract_id = str(uuid4())
    async with session_factory() as session, session.begin():
        session.add(
            WorkerContractModel(
                contract_id=contract_id,
                revision=1,
                can_do_json=["test"],
                cannot_do_json=[],
                input_schema_json={"type": "object"},
                output_schema_json={"type": "object"},
                fingerprint_hash=str(uuid4()),
                created_at=now,
            )
        )
        session.add(
            WorkerInstanceModel(
                instance_id=instance_id,
                service_principal="worker-a",
                instance_key=str(uuid4()),
                contract_id=contract_id,
                contract_revision=1,
                base_url="https://worker.example",
                execution_timeout_seconds=30,
                max_concurrency=2,
                status="ready",
                accepting_tasks=True,
                consecutive_health_failures=0,
                consecutive_health_successes=2,
                health_identity_valid=True,
                created_at=now,
                updated_at=now,
            )
        )
    target = (await repository.list_health_probe_targets())[0]
    transition = await repository.record_health_failure(target, failure_threshold=3)
    assert transition is not None and transition.status == "ready"
    assert len(await repository.list_available_workers()) == 1
    for _ in range(2):
        target = (await repository.list_health_probe_targets())[0]
        transition = await repository.record_health_failure(target, failure_threshold=3)
    assert transition is not None and transition.status == "not_ready"
    assert await repository.list_available_workers() == []
    result = WorkerHealthProbeResult(
        instance_key=target.instance_key,
        status="ready",
        accepting_tasks=True,
        checked_at=now,
    )
    target = (await repository.list_health_probe_targets())[0]
    transition = await repository.record_health_success(target, result, recovery_threshold=2)
    assert transition is not None and transition.status == "not_ready"
    target = (await repository.list_health_probe_targets())[0]
    transition = await repository.record_health_success(target, result, recovery_threshold=2)
    assert transition is not None and transition.status == "ready"
    assert len(await repository.list_available_workers()) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_identity_mismatch_requires_full_recovery_streak_and_stale_snapshot_is_ignored(worker_store) -> None:
    """错误实例键立即隔离，重新注册后旧探测不能修改新配置。"""
    session_factory, _ = worker_store
    repository = PostgresWorkerRegistryRepository(session_factory)
    now = datetime.now(timezone.utc)
    instance_id = str(uuid4())
    contract_id = str(uuid4())
    async with session_factory() as session, session.begin():
        session.add(
            WorkerContractModel(
                contract_id=contract_id,
                revision=1,
                can_do_json=[],
                cannot_do_json=[],
                input_schema_json={"type": "object"},
                output_schema_json={"type": "object"},
                fingerprint_hash=str(uuid4()),
                created_at=now,
            )
        )
        session.add(
            WorkerInstanceModel(
                instance_id=instance_id,
                service_principal="worker-a",
                instance_key=str(uuid4()),
                contract_id=contract_id,
                contract_revision=1,
                base_url="https://worker.example",
                execution_timeout_seconds=30,
                max_concurrency=2,
                status="ready",
                accepting_tasks=True,
                consecutive_health_failures=0,
                consecutive_health_successes=2,
                health_identity_valid=True,
                created_at=now,
                updated_at=now,
            )
        )
    target = (await repository.list_health_probe_targets())[0]
    transition = await repository.record_identity_mismatch(target)
    assert transition is not None and transition.status == "not_ready"
    assert await repository.list_available_workers() == []
    result = WorkerHealthProbeResult(
        instance_key=target.instance_key,
        status="ready",
        accepting_tasks=True,
        checked_at=now,
    )
    target = (await repository.list_health_probe_targets())[0]
    transition = await repository.record_health_success(target, result, recovery_threshold=2)
    assert transition is not None and transition.status == "not_ready"
    async with session_factory() as session, session.begin():
        row = await session.scalar(
            select(WorkerInstanceModel).where(WorkerInstanceModel.instance_id == instance_id)
        )
        assert row is not None
        row.base_url = "https://new-worker.example"
        row.updated_at = datetime.now(timezone.utc)
    assert await repository.record_health_success(target, result, recovery_threshold=2) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_first_registration_keeps_transactional_deduplication(worker_store) -> None:
    """两个请求同时查无回执后竞争注册，事务仍只创建一份并返回稳定重放。"""
    session_factory, _ = worker_store
    repository = PostgresWorkerOrchestrationRepository(session_factory)
    registration = WorkerRegistrationService(repository)
    body = json.loads(
        (Path(__file__).parents[2] / "docs/worker-protocol-v1.examples.json").read_text(
            encoding="utf-8"
        )
    )["examples"]["registration_request"]["payload"]
    body["registration_request_id"] = str(uuid4())
    body["worker_instance_key"] = str(uuid4())

    preflights = await asyncio.gather(
        *(
            registration.find_replay(body, service_principal="worker-a")
            for _ in range(2)
        )
    )
    assert preflights == [None, None]

    outcomes = await asyncio.gather(
        *(
            registration.register_with_replay(body, service_principal="worker-a")
            for _ in range(2)
        )
    )
    assert sorted(outcome.replayed for outcome in outcomes) == [False, True]
    assert outcomes[0].response == outcomes[1].response
