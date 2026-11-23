"""记忆迁移与 PostgreSQL 混合检索集成验证。"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta

import pytest
from integration.postgres_harness import isolated_postgres_schema
from sqlalchemy import func, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.memory.model import (
    MemoryModel,
    MemoryScopeStateModel,
    MemorySourceModel,
)
from adapter.memory.repository import MemoryRepository
from app.memory.service import MemoryService
from domain.memory.model import AccessContext, RecallRequest

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="test DB not configured"),
]


class _FixedEmbedding:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 1024 for _ in texts]


class _FixedReranker:
    async def score(self, _query: str, contents: list[str]) -> list[float]:
        return [0.99 for _ in contents]


class _TextLengthCounter:
    def count(self, text: str) -> int:
        return len(text)


async def _insert_memory(
    factory: async_sessionmaker[AsyncSession],
    *,
    memory_id: str,
    content: str,
    source_ref: str,
    updated_at: datetime,
    status: str = "active",
    expires_at: datetime | None = None,
) -> None:
    """直接写入检索夹具，与公共新增接口的幂等场景保持隔离。"""
    content_hash = hashlib.sha256(" ".join(content.split()).encode()).hexdigest()
    async with factory() as session, session.begin():
        await session.execute(
            pg_insert(MemoryModel).values(
                id=memory_id,
                namespace_id="memory-test-user",
                scope_key="personal",
                content=content,
                search_document=func.to_tsvector("athena_memory_chinese", content),
                embedding=[0.1] * 1024,
                embedding_profile_id="bge-m3-1024-v1",
                request_id=f"fixture:{memory_id}",
                request_hash=content_hash,
                status=status,
                origin="explicit",
                version=1,
                kind="fact",
                load_policy="on_demand",
                content_hash=content_hash,
                explicit_long_term=False,
                expires_at=expires_at,
                last_evidenced_at=updated_at,
                created_at=updated_at,
                updated_at=updated_at,
            )
        )
        await session.execute(
            pg_insert(MemorySourceModel).values(
                id=f"source:{memory_id}",
                memory_id=memory_id,
                source_kind="message",
                source_ref=source_ref,
                source_at=updated_at,
            )
        )


def test_memory_migration_and_hybrid_candidate_queries() -> None:
    """在隔离 schema 中验证迁移、检索、去重和版本写入。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("test DB not configured")

    async def scenario() -> None:
        async with (
            isolated_postgres_schema(
                database_url,
                prefix="athena_v2_memory",
                revision=None,
                token_bytes=6,
            ) as db,
            isolated_postgres_schema(
                database_url,
                prefix="athena_v2_shared",
                revision=None,
                token_bytes=6,
            ) as shared_db,
        ):
            shared_schema = shared_db.schema
            admin = db.admin_engine
            async with admin.begin() as connection:
                await connection.exec_driver_sql(
                    f'CREATE TEXT SEARCH CONFIGURATION "{shared_schema}".'
                    '"athena_memory_chinese" (PARSER=zhparser)'
                )
            # 连续执行两次 head 升级以验证记忆迁移脚本幂等。
            await db.upgrade_to("head")
            await db.upgrade_to("head")

            factory = db.session_factory
            repository = MemoryRepository(factory)
            now = datetime.now(UTC)
            async with factory() as session, session.begin():
                session.add(
                    MemoryScopeStateModel(
                        namespace_id="memory-test-user",
                        scope_key="personal",
                        enabled=True,
                        last_maintained_day=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
            await _insert_memory(
                factory,
                memory_id="memory_main",
                content="用户偏好中文回复，项目数据库为 PostgreSQL 17",
                source_ref="source-main",
                updated_at=now,
            )
            await _insert_memory(
                factory,
                memory_id="memory_dedupe_old",
                content="用户偏好使用中文回答",
                source_ref="source-dedupe-old",
                updated_at=now,
            )
            await _insert_memory(
                factory,
                memory_id="memory_dedupe_new",
                content=" 用户偏好使用中文回答 ",
                source_ref="source-dedupe-new",
                updated_at=now,
            )
            await _insert_memory(
                factory,
                memory_id="memory_archived",
                content="用户喜欢喝乌龙茶",
                source_ref="source-archived",
                updated_at=now,
                status="archived",
            )
            await _insert_memory(
                factory,
                memory_id="memory_expired",
                content="已过期的 PostgreSQL 17 记忆",
                source_ref="source-expired",
                updated_at=now,
                expires_at=now - timedelta(minutes=5),
            )

            keyword_hits = await repository.search_keyword(
                "memory-test-user", ("personal",), "PostgreSQL 17", 10
            )
            assert keyword_hits and keyword_hits[0][0].id == "memory_main"
            assert all(memory.id != "memory_expired" for memory, _score in keyword_hits)
            vector_hits = await repository.search_vector(
                "memory-test-user",
                ("personal",),
                [0.1] * 1024,
                "bge-m3-1024-v1",
                10,
            )
            assert any(memory.id == "memory_main" for memory, _score in vector_hits)
            assert all(memory.id != "memory_expired" for memory, _score in vector_hits)

            async with factory() as session, session.begin():
                await session.execute(
                    update(MemoryModel)
                    .where(MemoryModel.id == "memory_dedupe_new")
                    .values(updated_at=func.now())
                )
            service = MemoryService(
                repository,
                _FixedEmbedding(),
                _FixedReranker(),
                _TextLengthCounter(),
                profile_id="bge-m3-1024-v1",
            )
            recalled = await service.recall(
                RecallRequest(
                    AccessContext("memory-test-user", ("personal",), ()),
                    ("personal",),
                    "用户偏好使用中文回答",
                )
            )
            duplicates = [
                hit for hit in recalled.hits if hit.memory.content.strip() == "用户偏好使用中文回答"
            ]
            assert len(duplicates) == 1
            assert duplicates[0].memory.id == "memory_dedupe_new"
            assert {source.reference for source in duplicates[0].memory.sources} == {
                "source-dedupe-old",
                "source-dedupe-new",
            }

            retrieved = (
                await repository.retrieve_similar_batch(
                    AccessContext("memory-test-user", ("personal",), ("personal",)),
                    (("personal", "乌龙茶", (0.1,) * 1024),),
                    "bge-m3-1024-v1",
                )
            )[0]
            assert any(memory.id == "memory_archived" for memory in retrieved)

            await repository.update_settings("memory-test-user", False, False)
            disabled_snapshot = await repository.get_many(
                ("memory_main",), "memory-test-user", ("personal",)
            )
            assert not disabled_snapshot.read_enabled
            assert disabled_snapshot.memories == {}
            await repository.update_settings("memory-test-user", True, True)

            await db.downgrade_to("20261106_0007")
            async with admin.connect() as connection:
                shared_exists = await connection.scalar(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM pg_ts_config c "
                        "JOIN pg_namespace n ON n.oid = c.cfgnamespace "
                        "WHERE n.nspname = :schema AND c.cfgname = :config"
                        ")"
                    ),
                    {
                        "schema": shared_schema,
                        "config": "athena_memory_chinese",
                    },
                )
                assert shared_exists

    asyncio.run(scenario())
