from __future__ import annotations

import pytest
from sqlalchemy import text

from athena.infrastructure.sqlite.engine import close_engine, get_session, init_engine
from athena.infrastructure.sqlite.memory_repository import SqliteMemoryRepository


@pytest.fixture
async def repository(tmp_path):
    await init_engine(str(tmp_path / "relations.db"))
    yield SqliteMemoryRepository()
    await close_engine()


@pytest.mark.parametrize("relation_type", ["contradicts", "supports"])
@pytest.mark.asyncio
async def test_semantic_relation_is_persisted_idempotently(repository, relation_type):
    await repository.add_relation("new-memory", "existing-memory", relation_type)
    await repository.add_relation("new-memory", "existing-memory", relation_type)

    async with get_session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT source_memory_id, target_memory_id, relation_type "
                    "FROM memory_relations"
                )
            )
        ).all()

    assert rows == [("new-memory", "existing-memory", relation_type)]
