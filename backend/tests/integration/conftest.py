"""集成测试目录共享 pytest 夹具与 PostgreSQL 隔离 Schema 辅助导出。"""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from functools import partial

import pytest
from integration.postgres_harness import (
    ALEMBIC_INI,
    BACKEND_DIR,
    IsolatedPostgresSchema,
    build_alembic_config,
    isolated_postgres_schema,
    sync_isolated_postgres_schema,
)


@pytest.fixture
def postgres_test_database_url() -> str:
    """读取测试 PostgreSQL 连接串；未配置时按集成测试约定跳过。"""
    database_url = os.getenv("ATHENA_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    return database_url


@pytest.fixture
def isolated_postgres_schema_factory(
    postgres_test_database_url: str,
) -> Callable[..., AbstractAsyncContextManager[IsolatedPostgresSchema]]:
    """提供绑定好测试库连接串的隔离 schema 异步上下文工厂。"""
    return partial(isolated_postgres_schema, postgres_test_database_url)


__all__ = [
    "ALEMBIC_INI",
    "BACKEND_DIR",
    "IsolatedPostgresSchema",
    "build_alembic_config",
    "isolated_postgres_schema",
    "isolated_postgres_schema_factory",
    "postgres_test_database_url",
    "sync_isolated_postgres_schema",
]
