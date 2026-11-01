"""验证 checkpointer 在数据库关闭其空闲连接后能够自动恢复。"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from infrastructure.persistence.postgres.checkpointer import PostgresCheckpointer


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_checkpointer_replaces_terminated_idle_connection() -> None:
    """只终止隔离测试池的空闲连接，并通过 saver 验证重连后的读写。

    参数：无。

    返回：
    - None：断连后 checkpoint 仍可读取和写入。

    异常：
    - pytest.skip：未配置 PostgreSQL 测试数据库时跳过。
    - 数据库异常：隔离 schema、checkpoint 或连接池操作失败时传播。
    """
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"checkpointer_recovery_{uuid4().hex}"
    base_url = make_url(DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    admin_engine = create_async_engine(base_url)
    checkpoint_options: dict[str, str | int] = {
        "host": base_url.host or "",
        "port": base_url.port or 5432,
        "dbname": base_url.database or "",
        "user": base_url.username or "",
        "password": base_url.password or "",
        "options": f"-c search_path={schema}",
    }
    checkpoint_options.update(
        {
            name: value
            for name, value in base_url.query.items()
            if isinstance(value, (str, int)) and name != "options"
        }
    )
    checkpoint_conninfo = make_conninfo(**checkpoint_options)
    checkpointer = PostgresCheckpointer(checkpoint_conninfo)
    schema_created = False

    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True

        await checkpointer.start()
        saver = checkpointer.get_saver()
        pool = checkpointer._pool
        assert pool is not None

        config = {
            "configurable": {
                "thread_id": f"thread-{uuid4().hex}",
                "checkpoint_ns": "",
            }
        }
        checkpoint = empty_checkpoint()
        first_config = await saver.aput(
            config,
            checkpoint,
            {"source": "input", "step": 0, "parents": {}},
            {},
        )
        first_tuple = await saver.aget_tuple(first_config)
        assert first_tuple is not None
        assert first_tuple.checkpoint["id"] == checkpoint["id"]

        # 从测试专属 pool 读取其 backend PID，释放后只终止这条空闲连接。
        async with pool.connection() as pool_connection:
            async with pool_connection.cursor() as cursor:
                await cursor.execute("SELECT pg_backend_pid() AS pid")
                old_pid = (await cursor.fetchone())["pid"]
        async with await AsyncConnection.connect(
            checkpoint_conninfo, autocommit=True
        ) as admin_connection:
            async with admin_connection.cursor() as cursor:
                await cursor.execute("SELECT pg_terminate_backend(%s)", (old_pid,))
                assert (await cursor.fetchone())[0] is True

        recovered_tuple = await saver.aget_tuple(first_config)
        assert recovered_tuple is not None
        assert recovered_tuple.checkpoint["id"] == checkpoint["id"]

        next_checkpoint = empty_checkpoint()
        next_config = await saver.aput(
            first_config,
            next_checkpoint,
            {"source": "update", "step": 1, "parents": {}},
            {},
        )
        final_tuple = await saver.aget_tuple(next_config)
        assert final_tuple is not None
        assert final_tuple.checkpoint["id"] == next_checkpoint["id"]

        async with pool.connection() as pool_connection:
            async with pool_connection.cursor() as cursor:
                await cursor.execute("SELECT pg_backend_pid() AS pid")
                new_pid = (await cursor.fetchone())["pid"]
        assert new_pid != old_pid
    finally:
        await checkpointer.stop()
        if schema_created:
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()
