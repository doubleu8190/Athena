"""PostgreSQL acceptance for concurrent writes to one structured fact group."""

from __future__ import annotations

import asyncio
import os
from collections import Counter
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from application.memory.extraction import DefaultMemoryWriteTrigger, LLMMemoryResolver
from application.memory.workflow import MemoryWriteWorkflow
from domain.memory import (
    CompletedTurn,
    MemoryCandidate,
    MemoryFact,
    MemoryListRequest,
    MemoryResolution,
    MemoryResolutionProposal,
    MemorySourceReference,
    MemoryStateChangedError,
)
from infrastructure.persistence.postgres.models import (
    Base,
    MemoryModel,
    MemoryRelationModel,
    MemorySourceSuppressionModel,
)
from infrastructure.persistence.postgres.repositories import PostgresMemoryRepository


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

        async def extract(self, turn: CompletedTurn) -> list[MemoryCandidate]:
            """返回给定完成轮次对应的结构化事实候选。

            参数：turn 是待处理的合成用户轮次。
            返回：只含该轮候选的列表。
            异常：turn_id 不在固定夹具中时抛出 KeyError。
            """
            return [candidates[turn.turn_id]]

    class ConflictLLM:
        """对已有单值事实给出保守的矛盾保留决策。"""

        async def invoke(self, messages: list[object], **kwargs: object) -> object:
            """把已有事实 ID 作为目标，要求保留新旧两个值。

            参数：messages 是解析提示词；kwargs 是调用方模型选项。
            返回：含目标 ID 和 contradicts 关系的结构化决策。
            异常：事实列表为空时抛出 AssertionError。
            """
            page = await repository.list_memories(MemoryListRequest(limit=20))
            assert page.items
            return MemoryResolutionProposal(
                action="CREATE",
                target_memory_id=page.items[0].id,
                relation="contradicts",
                reason_code="ambiguous_conflicting_assertions",
            )

    class SnapshotBarrierResolver:
        """让两份空事实组快照同时进入提交阶段。"""

        def __init__(self) -> None:
            """初始化解析器和一次性并发屏障。

            参数：无。
            返回：无。
            异常：不主动抛出业务异常。
            """
            self._resolver = LLMMemoryResolver(ConflictLLM(), repository)
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
            DefaultMemoryWriteTrigger(),
            FixedExtractor(),
            shared_resolver,
        )
        for turn_id in source_turn_ids
    }
    turns = {
        turn_id: CompletedTurn(
            turn_id=turn_id,
            session_id=session_id,
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
                        MemoryModel.__table__,
                        MemoryRelationModel.__table__,
                        MemorySourceSuppressionModel.__table__,
                    ],
                    checkfirst=False,
                )
            )

        outcomes = await asyncio.gather(
            *(
                workflows[turn_id].process_completed_turn(turns[turn_id])
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
            turns[failed_turn_id]
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
