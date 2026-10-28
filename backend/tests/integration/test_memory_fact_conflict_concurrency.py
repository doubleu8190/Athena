"""PostgreSQL acceptance for concurrent writes to one structured fact group."""

from __future__ import annotations

import asyncio
import os
from collections import Counter
from datetime import datetime, timezone
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from application.memory.extraction import MemoryFactResolver
from application.memory.workflow import MemoryWriteWorkflow
from domain.memory import (
    CompletedTurn,
    MemoryCandidate,
    MemoryFact,
    MemoryListRequest,
    MemoryResolution,
    MemorySourceReference,
    MemoryStateChangedError,
    MemoryWriteCommand,
    ResolutionAction,
)
from domain.leases import LeaseGrant
from infrastructure.persistence.postgres.models import (
    Base,
    ExecutionLeaseModel,
    MemoryModel,
    MemoryRelationModel,
    MemorySourceSuppressionModel,
)
from infrastructure.persistence.postgres.repositories import PostgresMemoryRepository
from tests.fakes.memory_pipeline import Embedder, dependencies
from tests.fakes.leases import acquire_postgres_lease


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_conflicting_fact_writes_preserve_both_values() -> None:
    """并发决策遇到过期快照时重试，并保留两个值及矛盾关系。"""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"memory_fact_race_{uuid4().hex}"
    url = make_url(DATABASE_URL)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(url)
    engine = create_async_engine(
        url.update_query_dict({"options": f"-c search_path={schema},public"})
    )
    created = False
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = PostgresMemoryRepository(factory)
    now = datetime.now(timezone.utc).isoformat()
    source_turn_ids = (f"turn-{uuid4().hex}", f"turn-{uuid4().hex}")
    session_id = f"session-{uuid4().hex}"
    candidates = {
        source_turn_ids[0]: _candidate(source_turn_ids[0], session_id, "UTC", now),
        source_turn_ids[1]: _candidate(
            source_turn_ids[1], session_id, "Asia/Shanghai", now
        ),
    }

    class FixedExtractor:
        """按轮次返回固定候选，隔离并发写入行为与模型抽取质量。"""

        async def extract(
            self, turn: CompletedTurn, *, lease: LeaseGrant
        ) -> list[MemoryCandidate]:
            """返回给定完成轮次对应的结构化事实候选。

            参数：turn 是待处理的合成用户轮次。
            返回：只含该轮候选的列表。
            异常：turn_id 不在固定夹具中时抛出 KeyError。
            """
            return [candidates[turn.turn_id]]

    class SnapshotBarrierResolver:
        """让两份空事实组快照同时进入提交阶段。"""

        def __init__(self) -> None:
            """初始化解析器和一次性并发屏障。

            参数：无。
            返回：无。
            异常：不主动抛出业务异常。
            """
            self._resolver = MemoryFactResolver(repository)
            self._lock = asyncio.Lock()
            self._empty_snapshot_count = 0
            self._both_empty_snapshots_ready = asyncio.Event()

        async def resolve(
            self, values: list[MemoryCandidate]
        ) -> list[MemoryResolution]:
            """解析候选并同步空快照，稳定制造写入竞态。

            参数：values 是本轮冻结的候选列表。
            返回：带事实组快照的解析结果。
            异常：解析模型或仓储读取失败时向上传播。
            """
            resolutions = await self._resolver.resolve(values)
            if resolutions and not resolutions[0].expected_memory_ids:
                async with self._lock:
                    self._empty_snapshot_count += 1
                    if self._empty_snapshot_count == 2:
                        self._both_empty_snapshots_ready.set()
                await self._both_empty_snapshots_ready.wait()
            return resolutions

    shared_resolver = SnapshotBarrierResolver()
    workflows = {
        turn_id: MemoryWriteWorkflow(
            repository,
            FixedExtractor(),
            shared_resolver,
            Embedder(),
            **dependencies(),
        )
        for turn_id in source_turn_ids
    }
    turns = {
        turn_id: CompletedTurn(
            turn_id=turn_id,
            session_id=session_id,
            project_id="project-1",
            user_text=f"请记住：默认时区是{candidates[turn_id].fact.fact_value}。",
        )
        for turn_id in source_turn_ids
    }

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[
                        ExecutionLeaseModel.__table__,
                        MemoryModel.__table__,
                        MemoryRelationModel.__table__,
                        MemorySourceSuppressionModel.__table__,
                    ],
                    checkfirst=False,
                )
            )

        leases = {
            turn_id: await acquire_postgres_lease(
                factory, "memory_processing", turn_id
            )
            for turn_id in source_turn_ids
        }

        outcomes = await asyncio.gather(
            *(
                workflows[turn_id].process_completed_turn(
                    turns[turn_id], lease=leases[turn_id]
                )
                for turn_id in source_turn_ids
            ),
            return_exceptions=True,
        )
        failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
        assert len(failures) == 1
        assert isinstance(failures[0], MemoryStateChangedError)

        failed_index = outcomes.index(failures[0])
        failed_turn_id = source_turn_ids[failed_index]
        assert await workflows[failed_turn_id].process_completed_turn(
            turns[failed_turn_id], lease=leases[failed_turn_id]
        ) == 1

        page = await repository.list_memories(MemoryListRequest(limit=20))
        active_facts = [item for item in page.items if item.status.value == "active"]
        assert len(active_facts) == 2
        assert {item.content for item in active_facts} == {
            "timezone: UTC",
            "timezone: Asia/Shanghai",
        }
        assert {item.validity_status for item in active_facts} == {"valid"}

        async with factory() as session:
            persisted_facts = (
                await session.scalars(
                    select(MemoryModel).where(MemoryModel.status == "active")
                )
            ).all()
            versions_by_logical_id = Counter(
                item.logical_memory_id for item in persisted_facts
            )
            assert max(versions_by_logical_id.values()) == 1

        relations = await repository.related_entries(active_facts[0].id)
        assert len(relations) == 1
        assert relations[0].kind == "contradicts"
        assert relations[0].related_memory_id == active_facts[1].id

        async with factory() as session:
            persisted = (
                await session.scalars(
                    select(MemoryRelationModel).where(
                        MemoryRelationModel.kind == "contradicts"
                    )
                )
            ).all()
            assert len(persisted) == 1
        # 关系失败必须回滚新版本和旧版本状态，向量不能单独落库。
        target = active_facts[0]
        turn_id = source_turn_ids[0]
        corrected = replace(candidates[turn_id], content="timezone: Asia/Tokyo",
            fact=replace(candidates[turn_id].fact, fact_value="Asia/Tokyo", claim_intent="correct"))
        invalid_relation = MemoryResolution(ResolutionAction.REVISE, corrected,
            target_memory_id=target.id, relation="invalid",
            expected_memory_ids=tuple(sorted(item.id for item in active_facts)))
        with pytest.raises(ValueError, match="memory_relation_kind_invalid"):
            await repository.apply_resolution(invalid_relation, operation_id="rollback-check",
                embedding=[1.0] + [0.0] * 1023, lease=leases[turn_id])
        assert await repository.get("rollback-check") is None
        assert (await repository.get(target.id)).status.value == "active"
        async with factory() as session:
            rows = (await session.scalars(select(MemoryModel))).all()
            assert len(rows) == 2
            assert all(row.embedding is not None for row in rows)
            assert all("session_id" not in __import__("json").loads(row.metadata_json) for row in rows)
        # 重放已提交候选不会重新决策或创建关系。
        for turn_id in source_turn_ids:
            assert await workflows[turn_id].process_completed_turn(turns[turn_id], lease=leases[turn_id]) == 0

        # 只修改正文时应保留有效管理属性，而不是把索引列清成空值。
        manual_id = await repository.create_memory(MemoryWriteCommand(
            "管理条目", {"session_id": session_id, "type": "note", "category": "other", "confidence": 0.8},
            operation_key="manual-note",
        ))
        revision_id = await repository.revise(manual_id, "修订后的管理条目")
        revised = await repository.get(revision_id)
        assert revised.metadata["type"] == "note"
        assert revised.metadata["category"] == "other"
        assert revised.metadata["confidence"] == 0.8

    finally:
        await engine.dispose()
        if created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()


def _candidate(
    turn_id: str,
    session_id: str,
    value: str,
    observed_at: str,
) -> MemoryCandidate:
    """构造同一单值事实身份、不同来源和值的候选。

    参数：turn_id 是来源轮次；session_id 是写入会话；value 是默认时区；
    observed_at 是可解析的来源时间。
    返回：含结构化来源证据的候选事实。
    异常：事实契约字段无效时由 Pydantic 抛出 ValidationError。
    """
    fact = MemoryFact(
        scope_kind="global",
        scope_id="global",
        subject_key="user:self",
        fact_key="timezone",
        fact_value=value,
        confidence=0.95,
        asserted_at=observed_at,
        source_refs=(
            MemorySourceReference(
                source_type="turn",
                source_id=turn_id,
                excerpt=f"默认时区是{value}",
                observed_at=observed_at,
            ),
        ),
    )
    return MemoryCandidate(
        content=f"timezone: {value}",
        source_turn_id=turn_id,
        memory_type="fact",
        category="fact",
        confidence=0.95,
        session_id=session_id,
        fact=fact,
    )
