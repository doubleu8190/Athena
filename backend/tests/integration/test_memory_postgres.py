"""记忆迁移和 PostgreSQL 检索仓储集成验证。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from secrets import token_hex

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from adapter.memory.model import MemoryModel
from adapter.memory.repository import MemoryRepository
from app.memory.service import MemoryService
from domain.memory.model import (
    AccessContext,
    AddMemory,
    MemorySource,
    RecallRequest,
)

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


def test_memory_migration_add_and_hybrid_candidate_queries() -> None:
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("test DB not configured")

    async def scenario() -> None:
        schema = f"athena_v2_memory_{token_hex(6)}"
        shared_schema = f"athena_v2_shared_{token_hex(6)}"
        admin = create_async_engine(database_url, poolclass=NullPool)
        engine = create_async_engine(
            database_url,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": f"{schema},public"}},
        )
        try:
            async with admin.begin() as connection:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                await connection.execute(text(f'CREATE SCHEMA "{shared_schema}"'))
                await connection.execute(
                    text(
                        f'CREATE TEXT SEARCH CONFIGURATION "{shared_schema}".'
                        '"athena_memory_chinese" (PARSER=zhparser)'
                    )
                )
            config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
            config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
            config.attributes["connect_args"] = {
                "server_settings": {"search_path": f"{schema},public"}
            }
            config.attributes["version_table_schema"] = schema
            await asyncio.to_thread(command.upgrade, config, "head")
            await asyncio.to_thread(command.upgrade, config, "head")
            repository = MemoryRepository(async_sessionmaker(engine, expire_on_commit=False))
            record = await repository.add(
                AddMemory(
                    access=AccessContext("memory-test-user", ("personal",), ("personal",)),
                    scope_key="personal",
                    content="用户偏好中文回复，项目数据库为 PostgreSQL 17",
                    request_id="integration-1",
                    sources=(MemorySource("note", "case-1"),),
                ),
                [0.1] * 1024,
                "bge-m3-1024-v1",
            )
            replay = await repository.add(
                AddMemory(
                    access=AccessContext("memory-test-user", ("personal",), ("personal",)),
                    scope_key="personal",
                    content="用户偏好中文回复，项目数据库为 PostgreSQL 17",
                    request_id="integration-1",
                    sources=(MemorySource("note", "case-1"),),
                ),
                [0.1] * 1024,
                "bge-m3-1024-v1",
            )
            assert replay.id == record.id
            with pytest.raises(ValueError, match="memory_idempotency_conflict"):
                await repository.add(
                    AddMemory(
                        access=AccessContext("memory-test-user", ("personal",), ("personal",)),
                        scope_key="personal",
                        content="请求内容已改变",
                        request_id="integration-1",
                    ),
                    [0.1] * 1024,
                    "bge-m3-1024-v1",
                )
            with pytest.raises(IntegrityError):
                await repository.add(
                    AddMemory(
                        access=AccessContext("memory-test-user", ("personal",), ("personal",)),
                        scope_key="personal",
                        content="来源重复应导致整笔事务回滚",
                        request_id="integration-rollback",
                        sources=(
                            MemorySource("note", "duplicate"),
                            MemorySource("note", "duplicate"),
                        ),
                    ),
                    [0.1] * 1024,
                    "bge-m3-1024-v1",
                )
            assert (
                await repository.get_by_request(
                    AddMemory(
                        access=AccessContext("memory-test-user", ("personal",), ("personal",)),
                        scope_key="personal",
                        content="来源重复应导致整笔事务回滚",
                        request_id="integration-rollback",
                        sources=(
                            MemorySource("note", "duplicate"),
                            MemorySource("note", "duplicate"),
                        ),
                    )
                )
                is None
            )
            assert record.content.startswith("用户偏好")
            hits = await repository.search_keyword(
                "memory-test-user", ("personal",), "PostgreSQL 17", 10
            )
            assert hits and hits[0][0].id == record.id
            natural_question_hits = await repository.search_keyword(
                "memory-test-user", ("personal",), "这个项目使用的数据库版本号是多少", 10
            )
            assert natural_question_hits and natural_question_hits[0][0].id == record.id
            assert (
                await repository.search_keyword("memory-test-user", ("personal",), "a:b", 10) == []
            )
            punctuation_hits = await repository.search_keyword(
                "memory-test-user", ("personal",), "PostgreSQL's version:17", 10
            )
            assert punctuation_hits and punctuation_hits[0][0].id == record.id
            vectors = await repository.search_vector(
                "memory-test-user", ("personal",), [0.1] * 1024, "bge-m3-1024-v1", 10
            )
            assert vectors and vectors[0][0].id == record.id
            assert not await repository.search_vector(
                "memory-test-user", ("personal",), [0.1] * 1024, "different-profile", 10
            )
            duplicate_records = []
            for request_id, content, source_ref in (
                ("dedupe-old", "用户偏好使用中文回答", "dedupe-source-a"),
                ("dedupe-new", " 用户偏好使用中文回答 ", "dedupe-source-b"),
            ):
                duplicate_records.append(
                    await repository.add(
                        AddMemory(
                            access=AccessContext("memory-test-user", ("personal",), ("personal",)),
                            scope_key="personal",
                            content=content,
                            request_id=request_id,
                            sources=(MemorySource("note", source_ref),),
                        ),
                        [0.1] * 1024,
                        "bge-m3-1024-v1",
                    )
                )
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                await session.execute(
                    update(MemoryModel)
                    .where(MemoryModel.id == duplicate_records[1].id)
                    .values(updated_at=func.now() + text("INTERVAL '1 second'"))
                )
                await session.commit()
            memory_service = MemoryService(
                repository,
                _FixedEmbedding(),
                _FixedReranker(),
                _TextLengthCounter(),
                profile_id="bge-m3-1024-v1",
            )
            deduplicated = await memory_service.recall(
                RecallRequest(
                    AccessContext("memory-test-user", ("personal",), ()),
                    ("personal",),
                    "用户偏好使用中文回答",
                )
            )
            matching = [
                hit
                for hit in deduplicated.hits
                if hit.memory.content.strip() == "用户偏好使用中文回答"
            ]
            assert len(matching) == 1
            assert matching[0].memory.id == duplicate_records[1].id
            assert {source.reference for source in matching[0].memory.sources} == {
                "dedupe-source-a",
                "dedupe-source-b",
            }
            await repository.update_settings("memory-test-user", False, False)
            snapshot = await repository.get_many((record.id,), "memory-test-user", ("personal",))
            assert not snapshot.read_enabled
            assert snapshot.memories == {}
            with pytest.raises(ValueError, match="memory_auto_extract_disabled"):
                await repository.add(
                    AddMemory(
                        access=AccessContext("memory-test-user", (), ("personal",)),
                        scope_key="personal",
                        content="关闭提取后不能新增候选",
                        request_id="integration-extracted-disabled",
                        origin="extracted",
                        status="pending",
                    ),
                    [0.1] * 1024,
                    "bge-m3-1024-v1",
                )
            await repository.update_settings("memory-test-user", True, True)
            access = AccessContext("memory-test-user", ("personal",), ("personal",))
            changed = await asyncio.gather(
                repository.edit(access, record.id, "版本 A", [0.2] * 1024, "bge-m3-1024-v1", 1),
                repository.edit(access, record.id, "版本 B", [0.3] * 1024, "bge-m3-1024-v1", 1),
            )
            assert sorted(changed) == [False, True]
            assert await repository.delete(access, record.id, 2)
            with pytest.raises(ValueError, match="memory_request_deleted"):
                await repository.get_by_request(
                    AddMemory(
                        access=access,
                        scope_key="personal",
                        content="用户偏好中文回复，项目数据库为 PostgreSQL 17",
                        request_id="integration-1",
                        sources=(MemorySource("note", "case-1"),),
                    )
                )
            await asyncio.to_thread(command.downgrade, config, "20261106_0007")
            async with admin.connect() as connection:
                shared_config_exists = await connection.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_ts_config c "
                        "JOIN pg_namespace n ON n.oid=c.cfgnamespace "
                        "WHERE n.nspname=:schema AND c.cfgname=:config)"
                    ),
                    {"schema": shared_schema, "config": "athena_memory_chinese"},
                )
                own_config_exists = await connection.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_ts_config c "
                        "JOIN pg_namespace n ON n.oid=c.cfgnamespace "
                        "WHERE n.nspname=:schema AND c.cfgname=:config)"
                    ),
                    {"schema": schema, "config": "athena_memory_chinese"},
                )
                assert shared_config_exists
                assert not own_config_exists
        finally:
            await engine.dispose()
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{shared_schema}" CASCADE'))
            await admin.dispose()

    asyncio.run(scenario())
