"""业务连接池配置测试。"""

from __future__ import annotations

import asyncio

import pytest

from infrastructure.database import Database

DATABASE_URL = "postgresql+asyncpg://user:password@127.0.0.1:1/athena"


def test_database_exposes_single_business_engine_and_session_factory() -> None:
    database = Database(
        DATABASE_URL,
        pool_size=2,
        max_overflow=3,
    )
    try:
        assert database.session_factory.kw["bind"] is database.engine
    finally:
        asyncio.run(database.dispose())


@pytest.mark.parametrize(
    "options",
    [
        {"pool_size": 0},
        {"max_overflow": -1},
    ],
)
def test_database_rejects_invalid_bounded_pool_configuration(
    options: dict[str, int],
) -> None:
    with pytest.raises(ValueError):
        Database(DATABASE_URL, **options)
