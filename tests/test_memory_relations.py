from __future__ import annotations

import pytest
from sqlalchemy import text

from athena.infrastructure.postgre.engine import (
    close_sqlite_engines,
    get_core_session,
    initialize_sqlite_engines,
)
from athena.infrastructure.postgre.repositories.memory_repository import SQLiteMemoryRepository


@pytest.fixture
async def repository(tmp_path):
    await initialize_sqlite_engines(str(tmp_path / "relations.db"))
    yield SQLiteMemoryRepository()
    await close_sqlite_engines()


@pytest.mark.parametrize("relation_type", ["contradicts", "supports"])
@pytest.mark.asyncio
async def test_semantic_relation_is_persisted_idempotently(repository, relation_type):
    await repository.add_relation("new-memory", "existing-memory", relation_type)
    await repository.add_relation("new-memory", "existing-memory", relation_type)

    async with get_core_session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT source_memory_id, target_memory_id, relation_type "
                    "FROM memory_relations"
                )
            )
        ).all()

    assert rows == [("new-memory", "existing-memory", relation_type)]
