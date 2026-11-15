"""记忆检索、批量决策与同步控制的 PostgreSQL 关键场景。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from integration.test_memory_lifecycle_support import (
    DATABASE_URL,
    DatabaseHarness,
    HeadFixture,
    add_extract_job,
    build_batch,
    isolated_database,
    seed_head_fixture,
)
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.auth.model import AuthSessionModel
from adapter.memory.model import (
    MemoryJobModel,
    MemoryModel,
    MemoryScopeStateModel,
    MemorySettingModel,
    MemorySourceModel,
)
from adapter.memory.repository import MemoryRepository
from adapter.memory.repository.control_source import MemoryControlSourceRepository
from adapter.message.model import ConversationMessageModel
from adapter.run.model import RunModel
from adapter.session.model import SessionModel
from app.memory.service import MemoryService
from app.memory.worker import MemoryWorker, MemoryWorkerConfig
from domain.memory.lifecycle import (
    ApplyBatchCommand,
    ControlCommand,
    ControlPayload,
    DecisionAction,
    ExtractBatchPayload,
    ExtractionDecision,
    JobKind,
    LoadPolicy,
    MemoryCandidate,
    MemoryKind,
    MemoryPendingRun,
    MemorySourceMessage,
    PreparedBatchChange,
    PreparedMemoryCreate,
)
from domain.memory.model import AccessContext

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="test DB not configured"),
]

_EMBEDDING_PROFILE = "bge-m3-1024-v1"
_EMBEDDING = (0.1,) * 1024


class _BlockingEmbedding:
    """在向量阶段暂停，给鉴权撤销留下真实并发窗口。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """通知测试已读到来源，并等待撤销事务提交。"""
        self.started.set()
        await self.release.wait()
        return [list(_EMBEDDING) for _ in texts]


class _Reranker:
    async def score(self, _query: str, contents: list[str]) -> list[float]:
        """同步控制不使用重排。"""
        return [1.0 for _ in contents]


class _Counter:
    def count(self, text: str) -> int:
        """按字符数提供确定 token 估计。"""
        return len(text)


@dataclass(frozen=True, slots=True)
class _WorkerProfiles:
    """为 Worker 提供不调用供应商的默认模型版本。"""

    profile_id: str

    async def get_default_runtime(self) -> object:
        """返回只携带固定版本 ID 的最小运行配置。"""
        return SimpleNamespace(version=SimpleNamespace(id=self.profile_id))


class _EmptyExtraction:
    """在重新领取时完成空候选批次，验证 Worker 可继续推进。"""

    async def complete_extraction(
        self,
        batch: ExtractBatchPayload,
        runtime: object,
        sources: tuple[MemorySourceMessage, ...],
    ) -> ExtractBatchPayload:
        """将真实来源批次标记为提取完成。"""
        del runtime, sources
        return batch.model_copy(update={"extraction_complete": True})

    async def complete_decisions(
        self,
        batch: ExtractBatchPayload,
        runtime: object,
        sources: tuple[MemorySourceMessage, ...],
    ) -> ExtractBatchPayload:
        """空候选无需模型决策，直接标记决策完成。"""
        del runtime, sources
        return batch.model_copy(update={"decisions_complete": True})

    async def prepare_changes(
        self,
        batch: ExtractBatchPayload,
        sources: tuple[MemorySourceMessage, ...],
    ) -> tuple[PreparedBatchChange, ...]:
        """空候选批次没有需要向量化的变更。"""
        del batch, sources
        return ()


class _GenerationRaceRepository(MemoryRepository):
    """在首次读取来源前提交 generation 变化，复现真实 Worker 路径。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(factory)
        self._bump_generation = True

    async def load_batch_sources(
        self,
        batch: ExtractBatchPayload,
    ) -> tuple[MemorySourceMessage, ...]:
        """首次调用推进 generation，验证异步提取不会因无关 generation 变化而作废。"""
        if self._bump_generation:
            self._bump_generation = False
            async with self._factory() as session, session.begin():
                setting = await session.get(MemorySettingModel, batch.owner_id)
                assert setting is not None
                setting.generation += 1
        return await super().load_batch_sources(batch)


@dataclass(frozen=True, slots=True)
class LifecycleHarness:
    """一个隔离 schema 中的记忆仓储与最小 Run 夹具。"""

    database: DatabaseHarness
    fixture: HeadFixture
    memory: MemoryRepository


@dataclass(frozen=True, slots=True)
class BatchItem:
    """同一条候选的模型决策与事务外准备计划。"""

    candidate: MemoryCandidate
    decision: ExtractionDecision
    change: PreparedBatchChange


@asynccontextmanager
async def lifecycle_harness(
    owner_id: str,
    *,
    pending_count: int,
    generation: int = 1,
) -> AsyncIterator[LifecycleHarness]:
    """为单个场景建立独立 head schema。"""
    async with isolated_database() as database:
        fixture = await seed_head_fixture(
            database.factory,
            owner_id=owner_id,
            pending_count=pending_count,
            generation=generation,
        )
        yield LifecycleHarness(
            database=database,
            fixture=fixture,
            memory=MemoryRepository(database.factory),
        )


async def insert_memory(
    factory: async_sessionmaker[AsyncSession],
    *,
    memory_id: str,
    namespace_id: str,
    scope_key: str,
    content: str,
    now: datetime,
    status: str = "active",
    profile_id: str = _EMBEDDING_PROFILE,
    expires_at: datetime | None = None,
    source_refs: tuple[str, ...] = (),
) -> None:
    """直接写入检索夹具，避免普通新增推进当前 generation。"""
    async with factory() as session, session.begin():
        session.add(
            MemoryModel(
                id=memory_id,
                namespace_id=namespace_id,
                scope_key=scope_key,
                content=content,
                search_document=func.to_tsvector("athena_memory_chinese", content),
                embedding=[0.1] * 1024,
                embedding_profile_id=profile_id,
                request_id=f"request:{memory_id}",
                request_hash=hashlib.sha256(memory_id.encode()).hexdigest(),
                status=status,
                origin="explicit",
                version=1,
                expires_at=expires_at,
                kind=MemoryKind.PREFERENCE.value,
                load_policy=LoadPolicy.ON_DEMAND.value,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
                explicit_long_term=False,
                last_evidenced_at=now,
                superseded_by_id=None,
                created_at=now,
                updated_at=now,
                deleted_time=None,
            )
        )
        for source_ref in source_refs:
            session.add(
                MemorySourceModel(
                    id=(
                        "msrc_"
                        + hashlib.sha256(f"{memory_id}:message:{source_ref}".encode()).hexdigest()[
                            :24
                        ]
                    ),
                    memory_id=memory_id,
                    source_kind="message",
                    source_ref=source_ref,
                    source_at=now,
                )
            )


def make_item(
    fixture: HeadFixture,
    pending: MemoryPendingRun,
    *,
    candidate_index: int,
    action: DecisionAction,
    suffix: str,
    target_memory_id: str | None = None,
    target_version: int | None = None,
) -> BatchItem:
    """构造服务端已补齐 scope、来源和版本的决策计划。"""
    source = pending.messages[0]
    candidate = MemoryCandidate(
        content=f"用户偏好候选 {suffix}",
        kind=MemoryKind.PREFERENCE,
        scope="project",
        source_message_ids=(source.message_id,),
        evidence=source.text,
        proposed_load_policy=LoadPolicy.ON_DEMAND,
    )
    scope_key = f"project:{fixture.project_id}"
    decision = ExtractionDecision(
        candidate_index=candidate_index,
        candidate=candidate,
        action=action,
        content=candidate.content,
        namespace_id=fixture.owner_id,
        scope_key=scope_key,
        source_message_ids=candidate.source_message_ids,
        target_memory_id=target_memory_id,
        target_version=target_version,
    )
    create = (
        PreparedMemoryCreate(
            memory_id=f"memory_{suffix}",
            namespace_id=fixture.owner_id,
            request_id=f"request_{suffix}",
            scope_key=scope_key,
            content=candidate.content,
            content_hash=hashlib.sha256(candidate.content.encode()).hexdigest(),
            embedding=_EMBEDDING,
            embedding_profile_id=_EMBEDDING_PROFILE,
            kind=candidate.kind,
            load_policy=candidate.proposed_load_policy,
            explicit_long_term=False,
            last_evidenced_at=source.source_at,
            expires_at=candidate.expires_at,
            source_evidence=(source,),
            origin="extracted",
        )
        if action in {DecisionAction.ADD, DecisionAction.UPDATE}
        else None
    )
    change = PreparedBatchChange(
        action=action,
        namespace_id=fixture.owner_id,
        scope_key=scope_key,
        candidate_index=candidate_index,
        candidate=candidate,
        content=candidate.content,
        target_memory_id=target_memory_id,
        target_version=target_version,
        source_evidence=(source,),
        create=create,
    )
    return BatchItem(candidate, decision, change)


def test_retrieve_similar_fuses_channels_and_keeps_scope_and_settings() -> None:
    """后台检索允许 archived，同时保持 owner、scope、expiry 和 read_enabled 边界。"""

    async def scenario() -> None:
        async with lifecycle_harness("owner_retrieval", pending_count=0) as harness:
            fixture = harness.fixture
            project_scope = f"project:{fixture.project_id}"
            expired_at = fixture.now - timedelta(minutes=1)
            await insert_memory(
                harness.database.factory,
                memory_id="memory_active",
                namespace_id=fixture.owner_id,
                scope_key="personal",
                content="用户喜欢乌龙茶",
                now=fixture.now,
            )
            await insert_memory(
                harness.database.factory,
                memory_id="memory_archived",
                namespace_id=fixture.owner_id,
                scope_key="personal",
                content="用户也喜欢乌龙茶",
                now=fixture.now,
                status="archived",
            )
            await insert_memory(
                harness.database.factory,
                memory_id="memory_profile_mismatch",
                namespace_id=fixture.owner_id,
                scope_key="personal",
                content="旧向量 profile 仍可全文命中乌龙茶",
                now=fixture.now,
                profile_id="old-profile",
            )
            await insert_memory(
                harness.database.factory,
                memory_id="memory_expired",
                namespace_id=fixture.owner_id,
                scope_key="personal",
                content="过期乌龙茶记忆",
                now=fixture.now,
                expires_at=expired_at,
            )
            await insert_memory(
                harness.database.factory,
                memory_id="memory_project",
                namespace_id=fixture.owner_id,
                scope_key=project_scope,
                content="项目乌龙茶记忆",
                now=fixture.now,
            )
            other_owner_id = "owner_retrieval_other"
            async with harness.database.factory() as session, session.begin():
                session.add(
                    MemoryScopeStateModel(
                        namespace_id=other_owner_id,
                        scope_key="personal",
                        enabled=True,
                        last_maintained_day=None,
                        created_at=fixture.now,
                        updated_at=fixture.now,
                    )
                )
            await insert_memory(
                harness.database.factory,
                memory_id="memory_other_owner",
                namespace_id=other_owner_id,
                scope_key="personal",
                content="其他用户乌龙茶记忆",
                now=fixture.now,
            )
            access = AccessContext(
                fixture.owner_id,
                ("personal", project_scope),
                ("personal", project_scope),
            )
            retrieved = await harness.memory.retrieve_similar(
                access,
                "personal",
                "乌龙茶",
                _EMBEDDING,
                _EMBEDDING_PROFILE,
            )
            retrieved_by_id = {item.id: item for item in retrieved}
            assert {
                "memory_active",
                "memory_archived",
                "memory_profile_mismatch",
            } <= set(retrieved_by_id)
            assert "memory_expired" not in retrieved_by_id
            assert "memory_project" not in retrieved_by_id
            assert "memory_other_owner" not in retrieved_by_id
            assert retrieved_by_id["memory_archived"].status == "archived"
            assert retrieved_by_id["memory_active"].namespace_id == fixture.owner_id
            assert len({item.id for item in retrieved}) == len(retrieved)

            ordinary_keyword = await harness.memory.search_keyword(
                fixture.owner_id,
                ("personal",),
                "乌龙茶",
                10,
            )
            ordinary_ids = {item.id for item, _score in ordinary_keyword}
            assert "memory_active" in ordinary_ids
            assert "memory_profile_mismatch" in ordinary_ids
            assert "memory_archived" not in ordinary_ids
            assert "memory_expired" not in ordinary_ids

            await harness.memory.update_settings(fixture.owner_id, False, True)
            assert (
                await harness.memory.retrieve_similar(
                    access,
                    "personal",
                    "乌龙茶",
                    _EMBEDDING,
                    _EMBEDDING_PROFILE,
                )
                == ()
            )

    asyncio.run(scenario())


def test_update_archived_target_copies_sources_and_ignore_completes_run() -> None:
    """UPDATE 新建 active 行并保留旧来源；IGNORE 只完成 Run，不改证据。"""

    async def scenario() -> None:
        async with lifecycle_harness(
            "owner_update",
            pending_count=1,
            generation=3,
        ) as harness:
            fixture = harness.fixture
            project_scope = f"project:{fixture.project_id}"
            await insert_memory(
                harness.database.factory,
                memory_id="memory_target",
                namespace_id=fixture.owner_id,
                scope_key=project_scope,
                content="旧的乌龙茶偏好",
                now=fixture.now - timedelta(days=10),
                status="archived",
                source_refs=("old-source-a", "old-source-b"),
            )
            await insert_memory(
                harness.database.factory,
                memory_id="memory_ignore_survivor",
                namespace_id=fixture.owner_id,
                scope_key=project_scope,
                content="IGNORE 不应改变这一条",
                now=fixture.now - timedelta(days=20),
                status="archived",
            )
            job = await harness.memory.claim_job(kinds=(JobKind.EXTRACT_PENDING,))
            assert job is None
            await add_extract_job(
                harness.database.factory,
                owner_id=fixture.owner_id,
                job_id="job_update",
            )
            job = await harness.memory.claim_job(kinds=(JobKind.EXTRACT_PENDING,))
            assert job is not None
            pending = await harness.memory.list_pending_runs(
                fixture.owner_id,
                limit=20,
                token_budget=1_000_000,
            )
            update_item = make_item(
                fixture,
                pending[0],
                candidate_index=0,
                action=DecisionAction.UPDATE,
                suffix="updated",
                target_memory_id="memory_target",
                target_version=1,
            )
            ignore_item = make_item(
                fixture,
                pending[0],
                candidate_index=1,
                action=DecisionAction.IGNORE,
                suffix="ignored",
            )
            batch = build_batch(
                fixture,
                pending,
                batch_id="batch_update",
                candidates=(update_item.candidate, ignore_item.candidate),
                decisions=(update_item.decision, ignore_item.decision),
            )
            await harness.memory.load_batch_sources(batch)
            result = await harness.memory.apply_batch(
                ApplyBatchCommand(
                    job_id=job.id,
                    batch=batch,
                    changes=(update_item.change, ignore_item.change),
                )
            )

            assert result.updated == 1
            assert result.ignored == 1
            assert result.created == 0
            async with harness.database.factory() as session:
                old_target = await session.get(MemoryModel, "memory_target")
                new_target = await session.get(MemoryModel, "memory_updated")
                ignored = await session.get(MemoryModel, "memory_ignore_survivor")
                run = await session.get(RunModel, fixture.run_ids[0])
                memory_count = await session.scalar(select(func.count()).select_from(MemoryModel))
                new_sources = set(
                    (
                        await session.scalars(
                            select(MemorySourceModel.source_ref).where(
                                MemorySourceModel.memory_id == "memory_updated"
                            )
                        )
                    ).all()
                )

            assert memory_count == 3
            assert old_target is not None
            assert old_target.status == "superseded"
            assert old_target.version == 2
            assert old_target.superseded_by_id == "memory_updated"
            assert new_target is not None
            assert new_target.status == "active"
            assert new_target.version == 1
            assert new_target.content == "用户偏好候选 updated"
            assert {"old-source-a", "old-source-b", fixture.message_ids[0]} <= new_sources
            assert ignored is not None
            assert ignored.status == "archived"
            assert ignored.version == 1
            assert ignored.last_evidenced_at == fixture.now - timedelta(days=20)
            assert run is not None
            assert run.memory_extraction_status == "completed"
            assert run.memory_extraction_batch_id == "batch_update"

    asyncio.run(scenario())


def test_update_keeps_newest_evidence_from_target_and_copied_sources() -> None:
    """乱序 UPDATE 不得让替代行证据倒退，也不得因此提前归档。"""

    async def scenario() -> None:
        async with lifecycle_harness(
            "owner_update_evidence",
            pending_count=2,
            generation=2,
        ) as harness:
            fixture = harness.fixture
            project_scope = f"project:{fixture.project_id}"
            candidate_at = (
                fixture.now - timedelta(days=120),
                fixture.now - timedelta(days=110),
            )
            target_source_at = fixture.now - timedelta(days=3)
            target_last_evidence_at = fixture.now - timedelta(days=2)
            async with harness.database.factory() as session, session.begin():
                for message_id, source_at in zip(
                    fixture.message_ids,
                    candidate_at,
                    strict=True,
                ):
                    message = await session.get(ConversationMessageModel, message_id)
                    assert message is not None
                    message.created_at = source_at
                    message.completed_at = source_at
            await insert_memory(
                harness.database.factory,
                memory_id="memory_target_last_evidence",
                namespace_id=fixture.owner_id,
                scope_key=project_scope,
                content="旧目标自身证据较新",
                now=fixture.now - timedelta(days=10),
            )
            await insert_memory(
                harness.database.factory,
                memory_id="memory_target_copied_source",
                namespace_id=fixture.owner_id,
                scope_key=project_scope,
                content="旧目标复制来源较新",
                now=target_source_at,
                source_refs=("target-source",),
            )
            async with harness.database.factory() as session, session.begin():
                target_last_evidence = await session.get(
                    MemoryModel,
                    "memory_target_last_evidence",
                )
                target_copied_source = await session.get(
                    MemoryModel,
                    "memory_target_copied_source",
                )
                assert target_last_evidence is not None
                assert target_copied_source is not None
                target_last_evidence.last_evidenced_at = target_last_evidence_at
                target_copied_source.last_evidenced_at = fixture.now - timedelta(days=20)
            await add_extract_job(
                harness.database.factory,
                owner_id=fixture.owner_id,
                job_id="job_update_evidence",
            )
            job = await harness.memory.claim_job(kinds=(JobKind.EXTRACT_PENDING,))
            assert job is not None
            pending = await harness.memory.list_pending_runs(
                fixture.owner_id,
                limit=20,
                token_budget=1_000_000,
            )
            last_evidence_item = make_item(
                fixture,
                pending[0],
                candidate_index=0,
                action=DecisionAction.UPDATE,
                suffix="last_evidence",
                target_memory_id="memory_target_last_evidence",
                target_version=1,
            )
            copied_source_item = make_item(
                fixture,
                pending[1],
                candidate_index=1,
                action=DecisionAction.UPDATE,
                suffix="copied_source",
                target_memory_id="memory_target_copied_source",
                target_version=1,
            )
            batch = build_batch(
                fixture,
                pending,
                batch_id="batch_update_evidence",
                candidates=(last_evidence_item.candidate, copied_source_item.candidate),
                decisions=(last_evidence_item.decision, copied_source_item.decision),
            )
            await harness.memory.load_batch_sources(batch)
            result = await harness.memory.apply_batch(
                ApplyBatchCommand(
                    job_id=job.id,
                    batch=batch,
                    changes=(last_evidence_item.change, copied_source_item.change),
                )
            )

            assert result.updated == 2
            async with harness.database.factory() as session:
                last_evidence_row = await session.get(
                    MemoryModel,
                    "memory_last_evidence",
                )
                copied_source_row = await session.get(
                    MemoryModel,
                    "memory_copied_source",
                )
            assert last_evidence_row is not None
            assert copied_source_row is not None
            assert last_evidence_row.last_evidenced_at == target_last_evidence_at
            assert copied_source_row.last_evidenced_at == target_source_at

            maintained = await harness.memory.maintain_scope(
                fixture.owner_id,
                project_scope,
                fixture.now.date().isoformat(),
                fact_max_age_days=30,
                other_max_age_days=30,
            )
            assert maintained.age_archived == 0
            async with harness.database.factory() as session:
                rows = (
                    await session.scalars(
                        select(MemoryModel).where(
                            MemoryModel.id.in_(("memory_last_evidence", "memory_copied_source"))
                        )
                    )
                ).all()
            assert {row.status for row in rows} == {"active"}

    asyncio.run(scenario())


def test_correct_keeps_late_target_source_evidence() -> None:
    """早于旧来源的纠正证据不得让 correct 替代行证据时间倒退。"""

    async def scenario() -> None:
        async with lifecycle_harness(
            "owner_correct_evidence",
            pending_count=1,
            generation=2,
        ) as harness:
            fixture = harness.fixture
            project_scope = f"project:{fixture.project_id}"
            correction_at = fixture.now - timedelta(days=120)
            target_source_at = fixture.now - timedelta(days=3)
            target_last_evidence_at = fixture.now - timedelta(days=20)
            async with harness.database.factory() as session, session.begin():
                message = await session.get(
                    ConversationMessageModel,
                    fixture.message_ids[0],
                )
                assert message is not None
                message.created_at = correction_at
                message.completed_at = correction_at
            await insert_memory(
                harness.database.factory,
                memory_id="memory_correct_target",
                namespace_id=fixture.owner_id,
                scope_key=project_scope,
                content="旧目标来源较新",
                now=target_source_at,
                source_refs=("correct-target-source",),
            )
            async with harness.database.factory() as session, session.begin():
                target = await session.get(MemoryModel, "memory_correct_target")
                assert target is not None
                target.last_evidenced_at = target_last_evidence_at

            pending = await harness.memory.list_pending_runs(
                fixture.owner_id,
                limit=20,
                token_budget=1_000_000,
            )
            source = pending[0].messages[0]
            assert source.source_at == correction_at
            receipt = ControlPayload(
                owner_id=fixture.owner_id,
                run_id=fixture.run_ids[0],
                tool_call_id="call_correct_evidence",
                operation="correct",
                arguments_digest="a" * 64,
                auth_session_id=fixture.auth_session_id,
                namespace_id=fixture.owner_id,
                scope_key=project_scope,
            )
            applied = await harness.memory.apply_control(
                ControlCommand(
                    receipt=receipt,
                    decision_generation=2,
                    access_scope_key=project_scope,
                    operation="correct",
                    content="用户偏好候选 correct",
                    kind=MemoryKind.PREFERENCE,
                    load_policy=LoadPolicy.ON_DEMAND,
                    evidence_sources=(source,),
                    evidenced_at=source.source_at,
                    expires_at=None,
                    target_memory_id="memory_correct_target",
                    expected_version=1,
                    clear_scope=False,
                    create_memory_id="memory_corrected_evidence",
                    embedding=_EMBEDDING,
                    embedding_profile_id=_EMBEDDING_PROFILE,
                )
            )

            async with harness.database.factory() as session:
                corrected = await session.get(MemoryModel, "memory_corrected_evidence")
                target = await session.get(MemoryModel, "memory_correct_target")
                source_times = [
                    source_at
                    for source_at in (
                        await session.scalars(
                            select(MemorySourceModel.source_at).where(
                                MemorySourceModel.memory_id == "memory_corrected_evidence"
                            )
                        )
                    ).all()
                    if source_at is not None
                ]
            assert applied.memory_id == "memory_corrected_evidence"
            assert corrected is not None
            assert target is not None
            assert target.status == "superseded"
            assert target.superseded_by_id == corrected.id
            assert corrected.last_evidenced_at == target_source_at
            assert len(source_times) == 2
            assert max(source_times) == target_source_at

    asyncio.run(scenario())


def test_add_candidates_coexist_without_semantic_grouping() -> None:
    """同一 scope 的两个 ADD 在同一事务中各自落一行。"""

    async def scenario() -> None:
        async with lifecycle_harness("owner_add", pending_count=2) as harness:
            fixture = harness.fixture
            await add_extract_job(
                harness.database.factory,
                owner_id=fixture.owner_id,
                job_id="job_add",
            )
            job = await harness.memory.claim_job(kinds=(JobKind.EXTRACT_PENDING,))
            assert job is not None
            pending = await harness.memory.list_pending_runs(
                fixture.owner_id,
                limit=20,
                token_budget=1_000_000,
            )
            first = make_item(
                fixture,
                pending[0],
                candidate_index=0,
                action=DecisionAction.ADD,
                suffix="add_one",
            )
            second = make_item(
                fixture,
                pending[1],
                candidate_index=1,
                action=DecisionAction.ADD,
                suffix="add_two",
            )
            batch = build_batch(
                fixture,
                pending,
                batch_id="batch_add",
                candidates=(first.candidate, second.candidate),
                decisions=(first.decision, second.decision),
            )
            await harness.memory.load_batch_sources(batch)
            result = await harness.memory.apply_batch(
                ApplyBatchCommand(
                    job_id=job.id,
                    batch=batch,
                    changes=(first.change, second.change),
                )
            )
            assert result.created == 2
            assert result.updated == 0
            assert result.ignored == 0
            async with harness.database.factory() as session:
                rows = (await session.scalars(select(MemoryModel).order_by(MemoryModel.id))).all()
                runs = (await session.scalars(select(RunModel).order_by(RunModel.id))).all()
            assert [row.id for row in rows] == ["memory_add_one", "memory_add_two"]
            assert all(row.status == "active" for row in rows)
            assert all(row.memory_extraction_status == "completed" for row in runs)

    asyncio.run(scenario())


def test_invalid_update_rolls_back_entire_batch() -> None:
    """未知、跨 scope、旧版本和过期目标都不能留下半批次写入。"""

    async def scenario() -> None:
        cases = (
            ("unknown", None, "missing_target", 1),
            ("cross_scope", "personal", "memory_cross_scope", 1),
            ("version", "project", "memory_version", 2),
            ("expired", "project", "memory_expired_target", 1),
        )
        for case_name, target_scope, target_id, target_version in cases:
            async with lifecycle_harness(
                f"owner_reject_{case_name}",
                pending_count=1,
                generation=2,
            ) as harness:
                fixture = harness.fixture
                project_scope = f"project:{fixture.project_id}"
                if target_scope is not None:
                    await insert_memory(
                        harness.database.factory,
                        memory_id=target_id,
                        namespace_id=fixture.owner_id,
                        scope_key=("personal" if target_scope == "personal" else project_scope),
                        content="不应被更新的目标",
                        now=fixture.now - timedelta(days=2),
                        expires_at=(
                            fixture.now - timedelta(minutes=1) if case_name == "expired" else None
                        ),
                    )
                await add_extract_job(
                    harness.database.factory,
                    owner_id=fixture.owner_id,
                    job_id=f"job_reject_{case_name}",
                )
                job = await harness.memory.claim_job(kinds=(JobKind.EXTRACT_PENDING,))
                assert job is not None
                pending = await harness.memory.list_pending_runs(
                    fixture.owner_id,
                    limit=20,
                    token_budget=1_000_000,
                )
                add_item = make_item(
                    fixture,
                    pending[0],
                    candidate_index=0,
                    action=DecisionAction.ADD,
                    suffix=f"add_{case_name}",
                )
                update_item = make_item(
                    fixture,
                    pending[0],
                    candidate_index=1,
                    action=DecisionAction.UPDATE,
                    suffix=f"update_{case_name}",
                    target_memory_id=target_id,
                    target_version=target_version,
                )
                batch = build_batch(
                    fixture,
                    pending,
                    batch_id=f"batch_reject_{case_name}",
                    candidates=(add_item.candidate, update_item.candidate),
                    decisions=(add_item.decision, update_item.decision),
                )
                await harness.memory.load_batch_sources(batch)
                with pytest.raises(ValueError, match="memory_batch_target_changed"):
                    await harness.memory.apply_batch(
                        ApplyBatchCommand(
                            job_id=job.id,
                            batch=batch,
                            changes=(add_item.change, update_item.change),
                        )
                    )
                async with harness.database.factory() as session:
                    rows = (
                        await session.scalars(select(MemoryModel).order_by(MemoryModel.id))
                    ).all()
                    run = await session.get(RunModel, fixture.run_ids[0])
                expected_ids = [] if target_scope is None else [target_id]
                assert [row.id for row in rows] == expected_ids
                assert run is not None
                assert run.memory_extraction_status == "pending"

    asyncio.run(scenario())


def test_duplicate_update_target_and_stale_generation_are_rejected() -> None:
    """相同旧目标在同批更新两次会触发行级版本冲突并回滚；无目标冲突的 generation 变化可正常提交。"""

    async def scenario() -> None:
        async with lifecycle_harness(
            "owner_duplicate",
            pending_count=1,
            generation=5,
        ) as harness:
            fixture = harness.fixture
            project_scope = f"project:{fixture.project_id}"
            await insert_memory(
                harness.database.factory,
                memory_id="memory_duplicate_target",
                namespace_id=fixture.owner_id,
                scope_key=project_scope,
                content="唯一旧目标",
                now=fixture.now,
            )
            await add_extract_job(
                harness.database.factory,
                owner_id=fixture.owner_id,
                job_id="job_duplicate",
            )
            job = await harness.memory.claim_job(kinds=(JobKind.EXTRACT_PENDING,))
            assert job is not None
            pending = await harness.memory.list_pending_runs(
                fixture.owner_id,
                limit=20,
                token_budget=1_000_000,
            )
            first = make_item(
                fixture,
                pending[0],
                candidate_index=0,
                action=DecisionAction.UPDATE,
                suffix="duplicate_one",
                target_memory_id="memory_duplicate_target",
                target_version=1,
            )
            second = make_item(
                fixture,
                pending[0],
                candidate_index=1,
                action=DecisionAction.UPDATE,
                suffix="duplicate_two",
                target_memory_id="memory_duplicate_target",
                target_version=1,
            )
            batch = build_batch(
                fixture,
                pending,
                batch_id="batch_duplicate",
                candidates=(first.candidate, second.candidate),
                decisions=(first.decision, second.decision),
            )
            await harness.memory.load_batch_sources(batch)
            # 同批重复 UPDATE 同一目标会在第二次 _lock_target_memory 触发乐观锁冲突并回滚。
            with pytest.raises(ValueError, match="memory_batch_target_changed"):
                await harness.memory.apply_batch(
                    ApplyBatchCommand(
                        job_id=job.id,
                        batch=batch,
                        changes=(first.change, second.change),
                    )
                )
            async with harness.database.factory() as session:
                count = await session.scalar(select(func.count()).select_from(MemoryModel))
                target = await session.get(MemoryModel, "memory_duplicate_target")
            assert count == 1
            assert target is not None
            assert target.status == "active"
            assert target.version == 1

        async with lifecycle_harness(
            "owner_stale",
            pending_count=1,
            generation=7,
        ) as harness:
            fixture = harness.fixture
            await add_extract_job(
                harness.database.factory,
                owner_id=fixture.owner_id,
                job_id="job_stale",
            )
            job = await harness.memory.claim_job(kinds=(JobKind.EXTRACT_PENDING,))
            assert job is not None
            pending = await harness.memory.list_pending_runs(
                fixture.owner_id,
                limit=20,
                token_budget=1_000_000,
            )
            item = make_item(
                fixture,
                pending[0],
                candidate_index=0,
                action=DecisionAction.ADD,
                suffix="stale",
            )
            batch = build_batch(
                fixture,
                pending,
                batch_id="batch_stale",
                candidates=(item.candidate,),
                decisions=(item.decision,),
            )
            await harness.memory.load_batch_sources(batch)
            async with harness.database.factory() as session, session.begin():
                setting = await session.get(MemorySettingModel, fixture.owner_id)
                assert setting is not None
                setting.generation += 1
            result = await harness.memory.apply_batch(
                ApplyBatchCommand(
                    job_id=job.id,
                    batch=batch,
                    changes=(item.change,),
                )
            )
            # 无目标版本冲突时，仅 generation 推进不再导致异步提取整批作废。
            assert result.status == "applied"
            assert result.created == 1
            async with harness.database.factory() as session:
                count = await session.scalar(select(func.count()).select_from(MemoryModel))
                run = await session.get(RunModel, fixture.run_ids[0])
            assert count == 1
            assert run is not None
            assert run.memory_extraction_status == "completed"
            async with harness.database.factory() as session:
                stale_job = await session.get(MemoryJobModel, job.id)
            assert stale_job is not None
            assert stale_job.status == "done"

    asyncio.run(scenario())


def test_real_worker_requeues_stale_generation_and_reprocesses_pending_run() -> None:
    """Worker 在来源加载前遇到无关 generation 变化时不再作废重跑，一次 run_once 直接完成。"""

    async def scenario() -> None:
        async with lifecycle_harness(
            "owner_worker_stale",
            pending_count=1,
            generation=3,
        ) as harness:
            fixture = harness.fixture
            await add_extract_job(
                harness.database.factory,
                owner_id=fixture.owner_id,
                job_id="job_worker_stale",
            )
            repository = _GenerationRaceRepository(harness.database.factory)
            worker = MemoryWorker(
                repository,
                _WorkerProfiles(fixture.profile_id),  # type: ignore[arg-type]
                _EmptyExtraction(),  # type: ignore[arg-type]
                config=MemoryWorkerConfig(
                    batch_run_limit=20,
                    batch_token_limit=12_000,
                    idle_sleep_seconds=0.05,
                ),
            )
            # 隔离每日维护任务，确保本用例只观察目标提取任务。
            worker._maintenance_day = datetime.now(UTC).date().isoformat()

            assert await worker.run_once() is True
            async with harness.database.factory() as session:
                setting = await session.get(MemorySettingModel, fixture.owner_id)
                run = await session.get(RunModel, fixture.run_ids[0])
                job = await session.get(MemoryJobModel, "job_worker_stale")
            assert setting is not None
            # _GenerationRaceRepository 推进 generation 到 4，apply_batch 不再递增 generation。
            assert setting.generation == 4
            assert run is not None
            assert run.memory_extraction_status == "completed"
            assert job is not None
            assert job.status == "done"

    asyncio.run(scenario())


def test_permanent_discard_still_skips_pending_run() -> None:
    """自动提取禁用等永久失效原因仍将推荐 Run 标记为 skipped。"""

    async def scenario() -> None:
        async with lifecycle_harness(
            "owner_permanent_discard",
            pending_count=1,
            generation=1,
        ) as harness:
            fixture = harness.fixture
            await add_extract_job(
                harness.database.factory,
                owner_id=fixture.owner_id,
                job_id="job_permanent_discard",
            )
            job = await harness.memory.claim_job(kinds=(JobKind.EXTRACT_PENDING,))
            assert job is not None
            pending = await harness.memory.list_pending_runs(
                fixture.owner_id,
                limit=20,
                token_budget=1_000_000,
            )
            batch = build_batch(
                fixture,
                pending,
                batch_id="batch_permanent_discard",
            )
            async with harness.database.factory() as session, session.begin():
                setting = await session.get(MemorySettingModel, fixture.owner_id)
                assert setting is not None
                setting.auto_extract_enabled = False

            result = await harness.memory.apply_batch(
                ApplyBatchCommand(
                    job_id=job.id,
                    batch=batch,
                    changes=(),
                    discard_reason="memory_auto_extract_disabled",
                )
            )
            assert result.status == "discarded"
            async with harness.database.factory() as session:
                run = await session.get(RunModel, fixture.run_ids[0])
                stored_job = await session.get(MemoryJobModel, job.id)
            assert run is not None
            assert run.memory_extraction_status == "skipped"
            assert run.memory_extraction_safe_error_code == "memory_auto_extract_disabled"
            assert stored_job is not None
            assert stored_job.status == "done"

    asyncio.run(scenario())


def test_real_service_rechecks_revoked_auth_in_write_transaction() -> None:
    """向量准备期间撤销登录会话后，真实服务与仓储不得提交记忆。"""

    async def scenario() -> None:
        async with lifecycle_harness(
            "owner_auth_race",
            pending_count=1,
            generation=1,
        ) as harness:
            fixture = harness.fixture
            evidence = "记住，我喜欢苹果"
            async with harness.database.factory() as session, session.begin():
                await session.execute(
                    update(ConversationMessageModel)
                    .where(ConversationMessageModel.id == fixture.message_ids[0])
                    .values(parts=[{"type": "text", "text": evidence}])
                )

            embedding = _BlockingEmbedding()
            service = MemoryService(
                harness.memory,
                embedding,
                _Reranker(),
                _Counter(),
                lifecycle=harness.memory,
                control_source=MemoryControlSourceRepository(harness.database.factory),
            )
            project_scope = f"project:{fixture.project_id}"
            control = asyncio.create_task(
                service.remember(
                    AccessContext(
                        fixture.owner_id,
                        ("personal", project_scope),
                        ("personal", project_scope),
                    ),
                    run_id=fixture.run_ids[0],
                    session_id=fixture.session_id,
                    tool_call_id="tool_auth_race",
                    content="我喜欢苹果",
                    scope="project",
                    kind=MemoryKind.PREFERENCE,
                    load_policy=LoadPolicy.ON_DEMAND,
                    evidence=evidence,
                )
            )
            await asyncio.wait_for(embedding.started.wait(), timeout=5)
            async with harness.database.factory() as session, session.begin():
                auth = await session.get(AuthSessionModel, fixture.auth_session_id)
                assert auth is not None
                auth.revoked_time = datetime.now(UTC)
            embedding.release.set()

            with pytest.raises(ValueError, match="memory_auth_invalid"):
                await control
            async with harness.database.factory() as session:
                count = await session.scalar(select(func.count()).select_from(MemoryModel))
            assert count == 0

    asyncio.run(scenario())


def test_control_receipt_is_stable_and_deleted_run_cannot_execute() -> None:
    """同步控制按参数摘要重放，Session 删除后新调用不能写入。"""

    async def scenario() -> None:
        async with lifecycle_harness(
            "owner_control",
            pending_count=1,
            generation=2,
        ) as harness:
            fixture = harness.fixture
            project_scope = f"project:{fixture.project_id}"
            pending = await harness.memory.list_pending_runs(
                fixture.owner_id,
                limit=20,
                token_budget=1_000_000,
            )
            source = pending[0].messages[0]
            receipt = ControlPayload(
                owner_id=fixture.owner_id,
                run_id=fixture.run_ids[0],
                tool_call_id="call_remember",
                operation="remember",
                arguments_digest="a" * 64,
                auth_session_id=fixture.auth_session_id,
                namespace_id=fixture.owner_id,
                scope_key=project_scope,
            )
            command = ControlCommand(
                receipt=receipt,
                decision_generation=2,
                access_scope_key=project_scope,
                operation="remember",
                content="用户喜欢乌龙茶",
                kind=MemoryKind.PREFERENCE,
                load_policy=LoadPolicy.ON_DEMAND,
                evidence_sources=(source,),
                evidenced_at=source.source_at,
                expires_at=None,
                target_memory_id=None,
                expected_version=None,
                clear_scope=False,
                create_memory_id="memory_control",
                embedding=_EMBEDDING,
                embedding_profile_id=_EMBEDDING_PROFILE,
            )
            applied = await harness.memory.apply_control(command)
            replayed = await harness.memory.apply_control(command)
            assert replayed == applied
            assert applied.operation == "remember"
            assert applied.memory_id == "memory_control"
            still_pending = await harness.memory.list_pending_runs(
                fixture.owner_id,
                limit=20,
                token_budget=1_000_000,
            )
            assert fixture.run_ids[0] in {item.run_id for item in still_pending}
            with pytest.raises(ValueError, match="memory_control_idempotency_conflict"):
                await harness.memory.control_receipt(
                    run_id=fixture.run_ids[0],
                    tool_call_id="call_remember",
                    arguments_digest="b" * 64,
                )
            with pytest.raises(ValueError, match="memory_control_idempotency_conflict"):
                await harness.memory.apply_control(
                    ControlCommand(
                        receipt=receipt.model_copy(update={"arguments_digest": "b" * 64}),
                        decision_generation=applied.generation,
                        access_scope_key=project_scope,
                        operation="remember",
                        content="用户喜欢乌龙茶",
                        kind=MemoryKind.PREFERENCE,
                        load_policy=LoadPolicy.ON_DEMAND,
                        evidence_sources=(source,),
                        evidenced_at=source.source_at,
                        expires_at=None,
                        target_memory_id=None,
                        expected_version=None,
                        clear_scope=False,
                        create_memory_id="memory_control",
                        embedding=_EMBEDDING,
                        embedding_profile_id=_EMBEDDING_PROFILE,
                    )
                )

            async with harness.database.factory() as session, session.begin():
                chat_session = await session.get(SessionModel, fixture.session_id)
                assert chat_session is not None
                chat_session.deleted_time = datetime.now(UTC)
            after_delete = ControlCommand(
                receipt=receipt.model_copy(
                    update={
                        "tool_call_id": "call_after_delete",
                        "arguments_digest": "c" * 64,
                    }
                ),
                decision_generation=applied.generation,
                access_scope_key=project_scope,
                operation="remember",
                content="Session 删除后不得写入",
                kind=MemoryKind.PREFERENCE,
                load_policy=LoadPolicy.ON_DEMAND,
                evidence_sources=(source,),
                evidenced_at=source.source_at,
                expires_at=None,
                target_memory_id=None,
                expected_version=None,
                clear_scope=False,
                create_memory_id="memory_after_delete",
                embedding=_EMBEDDING,
                embedding_profile_id=_EMBEDDING_PROFILE,
            )
            with pytest.raises(ValueError, match="memory_control_session_invalid"):
                await harness.memory.apply_control(after_delete)
            async with harness.database.factory() as session:
                memory_ids = set((await session.scalars(select(MemoryModel.id))).all())
                control_count = await session.scalar(
                    select(func.count())
                    .select_from(MemoryJobModel)
                    .where(MemoryJobModel.kind == "control")
                )
            assert memory_ids == {"memory_control"}
            assert control_count == 1

    asyncio.run(scenario())
