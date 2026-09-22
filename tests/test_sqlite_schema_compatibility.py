"""SQLite 表结构必须直接来自当前 ORM 模型。"""

from __future__ import annotations

import sqlite3

import pytest

from athena.infrastructure.postgre.database import Database
from athena.infrastructure.postgre.models import Base


@pytest.mark.asyncio
async def test_fresh_database_schema_matches_orm_models(tmp_path):
    """新建数据库的模型表列应与 ``Base.metadata`` 完全一致。"""
    db_path = tmp_path / "model-schema.db"
    database = Database(str(db_path))
    await database.connect()
    try:
        with sqlite3.connect(db_path) as connection:
            for table in Base.metadata.sorted_tables:
                columns = {
                    row[1]
                    for row in connection.execute(f"PRAGMA table_info({table.name})")
                }
                assert columns == set(table.columns.keys())
    finally:
        await database.close()
