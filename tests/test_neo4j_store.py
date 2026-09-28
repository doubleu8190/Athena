"""Tests for the optional Neo4j connection and schema adapter."""

from __future__ import annotations

import pytest

from athena.config.settings import Settings
from athena.infrastructure.neo4j.graph_store import (
    Neo4jGraphConfig,
    Neo4jGraphStore,
)
from athena.infrastructure.neo4j.schema import SCHEMA_STATEMENTS


class _Result:
    def __init__(self, value: int = 1) -> None:
        self.value = value

    async def consume(self) -> None:
        return None

    async def single(self):
        return {"health": self.value}


class _Session:
    def __init__(self, statements: list[str]) -> None:
        self.statements = statements

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def run(self, statement: str, **_kwargs):
        self.statements.append(statement)
        return _Result()


class _Driver:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.closed = False

    def session(self, **_kwargs):
        return _Session(self.statements)

    async def close(self) -> None:
        self.closed = True


def test_settings_expose_opt_in_graph_configuration():
    settings = Settings(_env_file=None)

    assert settings.neo4j_enabled is False
    assert settings.graph_rag_enabled is False
    assert settings.graph_max_hops == 2
    assert Neo4jGraphConfig.from_settings(settings).uri == settings.neo4j_uri


@pytest.mark.asyncio
async def test_initialize_applies_idempotent_schema_and_closes_driver():
    driver = _Driver()
    store = Neo4jGraphStore(
        Neo4jGraphConfig(
            uri="bolt://test",
            username="neo4j",
            password="secret",
        )
    )
    store._driver = driver

    await store.initialize()

    assert len(driver.statements) == len(SCHEMA_STATEMENTS)
    assert all("IF NOT EXISTS" in statement for statement in driver.statements)
    assert await store.health_check() is True
    await store.close()
    assert driver.closed is True
    assert store.is_connected is False


@pytest.mark.asyncio
async def test_search_paths_rejects_unbounded_hop_count_before_driver_access():
    store = Neo4jGraphStore(
        Neo4jGraphConfig(uri="bolt://test", username="neo4j", password="secret")
    )

    with pytest.raises(ValueError, match="max_hops must be 1 or 2"):
        await store.search_paths(
            query="A",
            knowledge_base_ids=["kb-1"],
            max_hops=3,
            entity_limit=8,
            path_limit=12,
            min_confidence=0.65,
        )
