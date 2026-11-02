"""S7 Root 等待身份、能力事实和恢复作业的 PostgreSQL 验收。"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from domain.leases import LeaseKey, LeaseLostError
from domain.orchestration.worker_entities import (
    WorkerPlanSubmission,
    WorkerResultSubmission,
    WorkerTaskSpec,
)
from domain.runs import RootPlanWait
from infrastructure.persistence.postgres.models import (
    AgentRunModel,
    Base,
    ExecutionLeaseModel,
    OrchestratorExecutionModel,
    OrchestratorPlanModel,
    OrchestratorRootResumeJobModel,
    OrchestratorTaskModel,
    ProjectModel,
    SessionModel,
    WorkerContractModel,
    WorkerInstanceModel,
    WorkerResourceModel,
)
from infrastructure.persistence.postgres.repositories.root_resume_job_repository import PostgresRootResumeJobRepository
from infrastructure.persistence.postgres.repositories.run_repository import PostgresRunRepository
from infrastructure.persistence.postgres.repositories.worker_orchestration_repository import PostgresWorkerOrchestrationRepository
from tests.integration.test_worker_dispatch_s4 import _submit_plan
from tests.integration.test_worker_orchestration_s2 import worker_store


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_root_plan_submission_rejects_stale_execution_lease(worker_store) -> None:
    """旧 Root execution lease 不能在同一 Run 写入下一轮 Plan。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    project_id, session_id, run_id, contract_id = (str(uuid4()) for _ in range(4))
    async with session_factory() as session, session.begin():
        session.add(ProjectModel(id=project_id, name="S7 lease", created_at=now, updated_at=now))
        session.add(SessionModel(
            id=session_id, project_id=project_id, title="S7 lease", active_run_id=run_id,
            last_run_sequence=1, created_at=now, updated_at=now, last_activity_at=now,
        ))
        session.add(AgentRunModel(
            run_id=run_id, session_id=session_id, message_id=str(uuid4()), sequence=1,
            status="running", request_json={}, root_thread_id=f"run:{run_id}",
            root_wait_generation=0, orchestrator_plan_sequence=0, cancellation_epoch=0,
            cancellation_external_effect_status="none", created_at=now, updated_at=now,
        ))
        session.add(WorkerContractModel(
            contract_id=contract_id, revision=1, can_do_json=["summarize"],
            cannot_do_json=[], input_schema_json={"type": "object"},
            output_schema_json={"type": "object"}, fingerprint_hash=str(uuid4()),
            created_at=now,
        ))
    runs = PostgresRunRepository(session_factory)
    old = await runs.acquire_execution_lease(run_id, owner="old-root", lease_seconds=0.1)
    assert old is not None
    await asyncio.sleep(0.2)
    current = await runs.acquire_execution_lease(run_id, owner="new-root", lease_seconds=30)
    assert current is not None
    repository = PostgresWorkerOrchestrationRepository(session_factory)
    submission = WorkerPlanSubmission(
        run_id=run_id, plan_id=str(uuid4()), sequence=1,
        idempotency_key=str(uuid4()), goal="stale lease", tasks=(WorkerTaskSpec(
            task_ref="task", contract_id=contract_id, contract_revision=1,
            objective="summarize", input={}, acceptance_criteria="object",
        ),), created_at=now,
    )
    with pytest.raises(LeaseLostError):
        await repository.submit_root_plan(submission, lease=old)
    accepted = await repository.submit_root_plan(submission, lease=current)
    assert accepted.accepted
    runs = PostgresRunRepository(session_factory)
    first_wait = RootPlanWait(run_id, submission.plan_id, 1, 1, "interrupt-one", "checkpoint-one")
    assert await runs.arm_root_wait(run_id, wait=first_wait, lease=current)
    async with session_factory() as session, session.begin():
        first_plan = await session.get(OrchestratorPlanModel, submission.plan_id)
        assert first_plan is not None
        first_plan.status = "completed"
        first_plan.completed_at = now
    jobs = PostgresRootResumeJobRepository(session_factory)
    old_job = await jobs.claim_next("root-first-plan", lease_seconds=30)
    assert old_job is not None and old_job.plan_id == submission.plan_id
    second = WorkerPlanSubmission(
        run_id=run_id, plan_id=str(uuid4()), sequence=2,
        idempotency_key=str(uuid4()), goal="second round", tasks=(WorkerTaskSpec(
            task_ref="task-two", contract_id=contract_id, contract_revision=1,
            objective="summarize", input={}, acceptance_criteria="object",
        ),), created_at=now,
    )
    second_receipt = await repository.submit_root_plan(second, lease=current)
    assert second_receipt.accepted
    async with session_factory() as session:
        run = await session.get(AgentRunModel, run_id)
        assert run is not None
        assert run.root_wait_generation == 2
        assert run.root_wait_interrupt_id is None
        assert run.root_wait_checkpoint_id is None
        assert run.root_wait_armed_at is None
    second_wait = RootPlanWait(run_id, second.plan_id, 2, 2, "interrupt-two", "checkpoint-two")
    assert await runs.arm_root_wait(run_id, wait=second_wait, lease=current)
    assert await jobs.complete(old_job)
    async with session_factory() as session:
        run = await session.get(AgentRunModel, run_id)
        assert run is not None
        assert run.root_wait_interrupt_id == "interrupt-two"
        assert run.root_wait_checkpoint_id == "checkpoint-two"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wait_job_armed_before_worker_result_survives_claim_poll(worker_store) -> None:
    """checkpoint 先 arm 后收到最后一个结果时，job 保持待领并在 Plan 完成后可领取。"""
    session_factory, _ = worker_store
    contract_id = str(uuid4())
    worker_repository, plan_id = await _submit_plan(
        session_factory,
        task_contracts=(contract_id,),
        available_contracts=(contract_id,),
    )
    async with session_factory() as session, session.begin():
        run = await session.scalar(select(AgentRunModel).where(
            AgentRunModel.run_id == (await session.scalar(
                select(OrchestratorPlanModel.run_id).where(OrchestratorPlanModel.plan_id == plan_id)
            ))
        ))
        assert run is not None
        session_id = run.session_id
        project_id = str(uuid4())
        now = datetime.now(timezone.utc)
        session.add(ProjectModel(id=project_id, name="S7 wait race", created_at=now, updated_at=now))
        await session.flush()
        session.add(SessionModel(
            id=session_id, project_id=project_id, title="S7 wait race", active_run_id=run.run_id,
            last_run_sequence=1, created_at=now, updated_at=now, last_activity_at=now,
        ))
        task = await session.scalar(select(OrchestratorTaskModel).where(
            OrchestratorTaskModel.plan_id == plan_id
        ))
        assert task is not None
        execution = await session.scalar(select(OrchestratorExecutionModel).where(
            OrchestratorExecutionModel.task_id == task.task_id
        ))
        assert execution is not None
        instance_id = f"instance-{contract_id}"
        execution.instance_id = instance_id
        execution.status = "running"
        execution.capacity_reserved = True
        execution.started_at = now
        execution.deadline_at = now + timedelta(minutes=1)
        task.status = "running"
        task.active_execution_id = execution.execution_id
        run_id = run.run_id
    jobs = PostgresRootResumeJobRepository(session_factory)
    runs = PostgresRunRepository(session_factory)
    lease = await runs.acquire_execution_lease(run_id, owner="root-wait-race", lease_seconds=30)
    assert lease is not None
    wait = RootPlanWait(run_id, plan_id, 1, 1, "interrupt-race", "checkpoint-race")
    assert await runs.arm_root_wait(run_id, wait=wait, lease=lease)
    assert await jobs.claim_next("root-before-result", lease_seconds=30) is None
    async with session_factory() as session:
        job = await session.scalar(select(OrchestratorRootResumeJobModel).where(
            OrchestratorRootResumeJobModel.run_id == run_id
        ))
        plan = await session.get(OrchestratorPlanModel, plan_id)
        assert job is not None and job.status == "pending"
        assert plan is not None and plan.status == "running"
    async with session_factory() as session:
        execution = await session.scalar(select(OrchestratorExecutionModel).where(
            OrchestratorExecutionModel.task_id == task.task_id
        ))
        assert execution is not None
        execution_id, generation = execution.execution_id, execution.generation
    await worker_repository.record_result(submission=WorkerResultSubmission(
        execution_id=execution_id, instance_id=instance_id, generation=generation,
        result_id=str(uuid4()), status="completed", output={"ok": True},
    ))
    await asyncio.sleep(0.3)
    claimed = await jobs.claim_next("root-after-result", lease_seconds=30)
    assert claimed is not None
    assert claimed.run_id == run_id and claimed.plan_id == plan_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_root_plan_facts_wait_arm_and_resume_claim_are_current_and_idempotent() -> None:
    """本轮 facts 完整可读，checkpoint 重复登记只保留一个恢复作业且并发领取唯一。"""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"root_worker_s7_{uuid4().hex}"
    base_url = make_url(DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    admin_engine = create_async_engine(base_url)
    engine_a = create_async_engine(base_url.update_query_dict({"options": f"-c search_path={schema}"}))
    engine_b = create_async_engine(base_url.update_query_dict({"options": f"-c search_path={schema}"}))
    now = datetime.now(timezone.utc)
    project_id, session_id, run_id, plan_id = (str(uuid4()) for _ in range(4))
    task_id, execution_id, contract_id, instance_id, job_id = (str(uuid4()) for _ in range(5))
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine_a.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[
                ProjectModel.__table__, SessionModel.__table__, AgentRunModel.__table__,
                WorkerContractModel.__table__, WorkerInstanceModel.__table__,
                OrchestratorPlanModel.__table__, OrchestratorTaskModel.__table__,
                OrchestratorExecutionModel.__table__, WorkerResourceModel.__table__,
                OrchestratorRootResumeJobModel.__table__, ExecutionLeaseModel.__table__,
            ]))
        sessions_a = async_sessionmaker(engine_a, expire_on_commit=False)
        sessions_b = async_sessionmaker(engine_b, expire_on_commit=False)
        async with sessions_a() as session, session.begin():
            session.add(ProjectModel(id=project_id, name="S7", created_at=now, updated_at=now))
            session.add(SessionModel(id=session_id, project_id=project_id, title="S7", active_run_id=run_id, last_run_sequence=1, created_at=now, updated_at=now, last_activity_at=now))
            session.add(AgentRunModel(run_id=run_id, session_id=session_id, message_id=str(uuid4()), sequence=1, status="running", request_json={}, root_thread_id=f"run:{run_id}", root_wait_generation=1, orchestrator_plan_sequence=1, cancellation_epoch=0, cancellation_external_effect_status="none", created_at=now, updated_at=now))
            await session.flush()
            session.add(WorkerContractModel(contract_id=contract_id, revision=1, can_do_json=["summarize"], cannot_do_json=["send"], input_schema_json={"type": "object"}, output_schema_json={"type": "object"}, fingerprint_hash="s7", created_at=now))
            await session.flush()
            session.add(WorkerInstanceModel(instance_id=instance_id, service_principal="test", instance_key=str(uuid4()), worker_name="Test Worker", contract_id=contract_id, contract_revision=1, base_url="https://worker.invalid", execution_timeout_seconds=30, max_concurrency=1, status="ready", accepting_tasks=True, created_at=now, updated_at=now))
            session.add(OrchestratorPlanModel(plan_id=plan_id, run_id=run_id, sequence=1, idempotency_key="plan-1", request_hash="hash", goal="summarize source", status="completed", expected_task_count=1, terminal_task_count=1, root_wait_generation=2, created_at=now, completed_at=now))
            await session.flush()
            session.add(OrchestratorTaskModel(task_id=task_id, operation_id=str(uuid4()), plan_id=plan_id, task_ref="summary", contract_id=contract_id, contract_revision=1, objective="summarize", input_json={"source": "text"}, acceptance_criteria="one summary", status="completed", output_json={"summary": "done"}, execution_generation=1, cancellation_epoch=0, active_execution_id=execution_id, created_at=now, updated_at=now))
            await session.flush()
            session.add(OrchestratorExecutionModel(execution_id=execution_id, task_id=task_id, instance_id=instance_id, generation=1, status="completed", cancellation_epoch=0, capacity_reserved=False, started_at=now, deadline_at=now, result_json={"summary": "done"}, created_at=now, updated_at=now))
            await session.flush()
            session.add(OrchestratorRootResumeJobModel(job_id=job_id, run_id=run_id, plan_id=plan_id, root_wait_generation=2, status="pending", attempt=0, available_at=now, created_at=now, updated_at=now))

        worker_repo = PostgresWorkerOrchestrationRepository(sessions_a)
        assert await worker_repo.read_completed_plan(run_id, plan_id, 2) is None
        assert len(await worker_repo.list_authorized_capabilities(run_id)) == 1
        assert await worker_repo.read_completed_plan(run_id, plan_id, 1) is None

        run_repo = PostgresRunRepository(sessions_a)
        jobs_a = PostgresRootResumeJobRepository(sessions_a)
        jobs_b = PostgresRootResumeJobRepository(sessions_b)
        # 任务结果可先于 Root checkpoint 落库；generation 尚未推进时不得丢弃恢复作业。
        assert await jobs_a.claim_next("root-early", lease_seconds=30) is None
        async with sessions_a() as session, session.begin():
            run = await session.get(AgentRunModel, run_id)
            assert run is not None
            assert run.root_wait_generation == 1
            pending = await session.scalar(select(OrchestratorRootResumeJobModel).where(OrchestratorRootResumeJobModel.job_id == job_id))
            assert pending is not None and pending.status == "pending"

        async with sessions_a() as session, session.begin():
            run = await session.get(AgentRunModel, run_id)
            assert run is not None
            run.root_wait_generation = 2
            pending = await session.get(OrchestratorRootResumeJobModel, job_id)
            assert pending is not None
            pending.available_at = now - timedelta(seconds=1)
        facts = await worker_repo.read_completed_plan(run_id, plan_id, 2)
        assert facts is not None
        assert facts.tasks[0].task_ref == "summary"
        assert facts.tasks[0].objective == "summarize"
        assert facts.tasks[0].acceptance_criteria == "one summary"
        assert facts.tasks[0].output == {"summary": "done"}
        assert facts.tasks[0].attempts[0].generation == 1
        run_lease = await run_repo.acquire_execution_lease(run_id, owner="root-arm", lease_seconds=30)
        assert run_lease is not None
        wait = RootPlanWait(run_id, plan_id, 1, 2, "interrupt-2", "checkpoint-2")
        armed = await asyncio.gather(
            run_repo.arm_root_wait(run_id, wait=wait, lease=run_lease),
            run_repo.arm_root_wait(run_id, wait=wait, lease=run_lease),
        )
        assert armed == [True, True]
        claims = await asyncio.gather(
            jobs_a.claim_next("root-a", lease_seconds=30),
            jobs_b.claim_next("root-b", lease_seconds=30),
        )
        claimed = [lease for lease in claims if lease is not None]
        assert len(claimed) == 1
        lease = claimed[0]
        assert lease.root_wait_generation == 2
        assert lease.interrupt_id == "interrupt-2"
        assert lease.checkpoint_id == "checkpoint-2"
        owner = jobs_a if claims[0] is not None else jobs_b
        assert await owner.is_wait_current(lease)
        assert await owner.retry(lease, delay_seconds=0)
        async with sessions_a() as session:
            persisted = (await session.scalars(select(OrchestratorRootResumeJobModel).where(OrchestratorRootResumeJobModel.run_id == run_id))).all()
            assert len(persisted) == 1
            assert persisted[0].status == "pending"
            root_lease = await session.get(ExecutionLeaseModel, ("root_resume", job_id))
            assert root_lease is not None and root_lease.state == "released"

        # Root 已提交下一 Plan，但进程在确认旧恢复 job 前崩溃时，旧 job 可安全收敛。
        next_lease = await jobs_a.claim_next("root-before-ack", lease_seconds=30)
        assert next_lease is not None
        next_plan_id = str(uuid4())
        async with sessions_a() as session, session.begin():
            session.add(OrchestratorPlanModel(
                plan_id=next_plan_id, run_id=run_id, sequence=2,
                idempotency_key="plan-2", request_hash="hash-2",
                goal="next round", status="running", expected_task_count=0,
                terminal_task_count=0, root_wait_generation=3,
                created_at=now,
            ))
            run = await session.get(AgentRunModel, run_id)
            assert run is not None
            run.orchestrator_plan_sequence = 2
            run.root_wait_generation = 3
        assert not await jobs_a.is_wait_current(next_lease)
        assert await jobs_a.complete(next_lease)

        # Run 停止后，未消费的恢复工作必须被收敛，不能再次恢复 Root。
        stopped_job_id = str(uuid4())
        async with sessions_a() as session, session.begin():
            session.add(OrchestratorRootResumeJobModel(
                job_id=stopped_job_id, run_id=run_id, plan_id=next_plan_id,
                root_wait_generation=3, status="pending", attempt=0,
                available_at=now, created_at=now, updated_at=now,
            ))
            await session.flush()
            await jobs_a._leases.ensure_in_transaction(
                session, LeaseKey("root_resume", stopped_job_id)
            )
            run = await session.get(AgentRunModel, run_id)
            assert run is not None
            run.status = "stopping"
        assert await jobs_a.claim_next("root-after-stop", lease_seconds=30) is None
        async with sessions_a() as session:
            stopped_job = await session.get(OrchestratorRootResumeJobModel, stopped_job_id)
            root_lease = await session.get(ExecutionLeaseModel, ("root_resume", stopped_job_id))
            assert stopped_job is not None and stopped_job.status == "cancelled"
            assert root_lease is not None and root_lease.state == "completed"
        assert await worker_repo.list_authorized_capabilities(run_id) == ()
        assert await worker_repo.read_completed_plan(run_id, plan_id, 2) is None

        # 同一取消事务也需关闭从未领取过的恢复 lease。
        pending_job_id = str(uuid4())
        async with sessions_a() as session, session.begin():
            session.add(OrchestratorRootResumeJobModel(
                job_id=pending_job_id, run_id=run_id, plan_id=plan_id,
                root_wait_generation=1, status="pending", attempt=0,
                available_at=now, created_at=now, updated_at=now,
            ))
            await session.flush()
            await jobs_a._leases.ensure_in_transaction(
                session, LeaseKey("root_resume", pending_job_id)
            )
            run = await session.get(AgentRunModel, run_id)
            assert run is not None
            await run_repo._cancel_root_resume_jobs(
                session, run.run_id, now
            )
        async with sessions_a() as session:
            pending_job = await session.get(
                OrchestratorRootResumeJobModel, pending_job_id
            )
            pending_lease = await session.get(
                ExecutionLeaseModel, ("root_resume", pending_job_id)
            )
            assert pending_job is not None and pending_job.status == "cancelled"
            assert pending_lease is not None and pending_lease.state == "completed"
    finally:
        await engine_a.dispose()
        await engine_b.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()
