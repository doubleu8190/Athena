from __future__ import annotations

import pytest
from sqlalchemy import text

from athena.infrastructure.postgre.engine import (
    close_postgres_engine,
    get_session,
    initialize_postgres_engine,
)
from athena.infrastructure.postgre.repositories.memory_repository import PostgresMemoryRepository


@pytest.fixture
async def repository(postgres_url):
    await initialize_postgres_engine(postgres_url)
    yield PostgresMemoryRepository()
    await close_postgres_engine()


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
