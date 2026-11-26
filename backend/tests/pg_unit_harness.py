"""PostgreSQL 快速单元测试夹具。

核心策略：
1. 懒加载初始化：不在模块导入或全局 autouse fixture 中连库，保证纯内存单测可零依赖离线秒跑；
2. 进程级一次建表：首次调用时确保 pgvector 扩展存在、创建随机隔离 Schema（``unit_<12hex>``），
   并在该 Schema 内执行一次 ``Base.metadata.create_all``；
3. 事件循环安全：每次进入测试均在当前事件循环创建独立 ``NullPool`` 异步引擎，兼容同步测试中的
   多次 ``asyncio.run(...)`` 及 ``TestClient`` 跨线程事件循环；
4. 毫秒级数据重置：每用例入口执行单条 ``TRUNCATE TABLE ... RESTART IDENTITY CASCADE``
   清空全部 39 张表；
5. 进程退出零残留：由 ``backend/tests/conftest.py`` 的 ``pytest_sessionfinish``（并辅以
   ``atexit`` 兜底）幂等执行 ``DROP SCHEMA IF EXISTS "<schema>" CASCADE``。
"""

from __future__ import annotations

import asyncio
import atexit
import os
import re
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from secrets import token_hex

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from adapter.models import Base

_SCHEMA_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

# 进程级缓存：同一个测试会话共享唯一隔离 Schema，避免每个用例重复执行建表/迁移。
_active_database_url: str | None = None
_active_schema: str | None = None
_truncate_sql: str | None = None
_atexit_registered: bool = False


def _validate_schema_name(schema: str) -> None:
    """校验隔离 Schema 标识符，防止非法字符或超出 PostgreSQL 63 字节限制。"""
    if not _SCHEMA_NAME_PATTERN.fullmatch(schema):
        raise ValueError(f"invalid_postgres_unit_schema_name:{schema!r}")


def require_pg_test_database_url() -> str:
    """读取并校验 PostgreSQL 单元测试数据库连接串；未配置时给出明确操作指引。"""
    database_url = os.getenv("ATHENA_TEST_DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError(
            "未配置 ATHENA_TEST_DATABASE_URL，无法运行依赖 PostgreSQL 的单元测试。\n"
            "请先启动本地测试数据库并加载环境变量：\n"
            "  docker compose -f compose.test.yml up -d postgres && "
            "source scripts/load-test-env.sh\n"
            "（不依赖数据库的纯内存单元测试可直接离线运行。）"
        )
    return database_url


def pg_unit_connect_args(schema: str) -> dict[str, object]:
    """构造绑定到指定隔离 Schema 的 asyncpg 连接参数（保持 public 可解析 pgvector 类型）。"""
    _validate_schema_name(schema)
    return {"server_settings": {"search_path": f"{schema},public"}}


def _build_truncate_sql(schema: str) -> str:
    """生成覆盖全部 ORM 表的单条 TRUNCATE 语句，确保毫秒级清空且级联重置序列。"""
    _validate_schema_name(schema)
    table_names = sorted(Base.metadata.tables.keys())
    if not table_names:
        raise RuntimeError("Base.metadata.tables is empty; ORM models were not registered")
    quoted_tables = ", ".join(f'"{schema}"."{table_name}"' for table_name in table_names)
    return f"TRUNCATE TABLE {quoted_tables} RESTART IDENTITY CASCADE"


async def _drop_schema_async(database_url: str, schema: str) -> None:
    """在独立事务中级联删除指定临时 Schema。"""
    _validate_schema_name(schema)
    admin_engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        await admin_engine.dispose()


async def _create_schema_with_metadata(database_url: str, schema: str) -> None:
    """确保 pgvector 扩展已安装，创建隔离 Schema 并执行一次 ORM 全量建表。"""
    _validate_schema_name(schema)
    admin_engine = create_async_engine(database_url, poolclass=NullPool)
    schema_created = False
    try:
        # CI 容器中的全新 athena_test 库可能尚未安装 vector 扩展，显式幂等安装到 public。
        async with admin_engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        schema_created = True

        init_engine = create_async_engine(
            database_url,
            poolclass=NullPool,
            connect_args=pg_unit_connect_args(schema),
        )
        try:
            async with init_engine.begin() as conn:
                # 必须传 checkfirst=False：当 search_path 含 public（解析 pgvector 类型）且
                # public 存在同名历史表时，checkfirst=True 会误判表已存在而跳过在隔离 Schema 建表。
                await conn.run_sync(
                    lambda sync_conn: Base.metadata.create_all(sync_conn, checkfirst=False)
                )
        finally:
            await init_engine.dispose()
    except BaseException:
        # 建表失败时立即回收半成品 Schema，避免测试库遗留脏 Schema。
        if schema_created:
            async with admin_engine.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        raise
    finally:
        await admin_engine.dispose()


async def ensure_pg_unit_schema() -> tuple[str, str]:
    """懒初始化当前测试进程复用的隔离 PostgreSQL Schema，并返回 (database_url, schema)。"""
    global _active_database_url, _active_schema, _truncate_sql, _atexit_registered

    database_url = require_pg_test_database_url()
    if _active_schema is not None and _active_database_url == database_url:
        return database_url, _active_schema

    # 若测试进程内切换了目标数据库 URL，先安全清理旧 Schema 再重建。
    if _active_schema is not None and _active_database_url is not None:
        old_url, old_schema = _active_database_url, _active_schema
        _active_database_url = None
        _active_schema = None
        _truncate_sql = None
        await _drop_schema_async(old_url, old_schema)

    schema = f"unit_{token_hex(6)}"
    await _create_schema_with_metadata(database_url, schema)
    _active_database_url = database_url
    _active_schema = schema
    _truncate_sql = _build_truncate_sql(schema)

    if not _atexit_registered:
        atexit.register(cleanup_pg_unit_schema)
        _atexit_registered = True

    return database_url, schema


async def reset_pg_unit_data(engine: AsyncEngine) -> None:
    """使用单条 TRUNCATE 语句清空当前隔离 Schema 内全部表数据并重置标识序列。"""
    if _truncate_sql is None:
        raise RuntimeError("pg_unit_schema_not_initialized")
    async with engine.begin() as conn:
        await conn.execute(text(_truncate_sql))


async def create_pg_unit_db(
    *,
    reset: bool = True,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """在当前事件循环内创建指向隔离 Schema 的异步引擎与会话工厂，默认先清空表数据。

    调用方须在用例结束时执行 ``await engine.dispose()``，或直接使用 ``async with pg_unit_db()``。
    """
    database_url, schema = await ensure_pg_unit_schema()
    # 使用 NullPool 确保不跨事件循环缓存 asyncpg 连接，并防止空闲连接阻塞 TRUNCATE/DROP SCHEMA。
    engine = create_async_engine(
        database_url,
        poolclass=NullPool,
        connect_args=pg_unit_connect_args(schema),
    )
    try:
        if reset:
            await reset_pg_unit_data(engine)
    except BaseException:
        await engine.dispose()
        raise
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, session_factory


@asynccontextmanager
async def pg_unit_db(
    *,
    reset: bool = True,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """异步上下文管理器：获取清空后的隔离 PostgreSQL 引擎与会话工厂，退出时自动释放。"""
    engine, session_factory = await create_pg_unit_db(reset=reset)
    try:
        yield engine, session_factory
    finally:
        await engine.dispose()


async def prepare_pg_unit_cli_target(*, reset: bool = True) -> tuple[str, str, dict[str, object]]:
    """为 CLI 入口测试准备隔离 Schema，返回 (database_url, schema, connect_args)。"""
    database_url, schema = await ensure_pg_unit_schema()
    connect_args = pg_unit_connect_args(schema)
    if reset:
        engine = create_async_engine(
            database_url,
            poolclass=NullPool,
            connect_args=connect_args,
        )
        try:
            await reset_pg_unit_data(engine)
        finally:
            await engine.dispose()
    return database_url, schema, connect_args


@asynccontextmanager
async def temp_pg_schema(
    *,
    prefix: str = "unit_ddl",
) -> AsyncIterator[tuple[AsyncEngine, str]]:
    """创建一次性空临时 Schema 并在退出时立即级联删除，专用于验证 ORM 元数据 DDL 建表。"""
    database_url = require_pg_test_database_url()
    schema = f"{prefix}_{token_hex(6)}"
    _validate_schema_name(schema)

    admin_engine = create_async_engine(database_url, poolclass=NullPool)
    schema_created = False
    engine = create_async_engine(
        database_url,
        poolclass=NullPool,
        connect_args=pg_unit_connect_args(schema),
    )
    try:
        async with admin_engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        schema_created = True
        yield engine, schema
    finally:
        try:
            await engine.dispose()
        finally:
            try:
                if schema_created:
                    async with admin_engine.begin() as conn:
                        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            finally:
                await admin_engine.dispose()


def cleanup_pg_unit_schema() -> None:
    """幂等清理当前测试进程创建的隔离 Schema；未初始化时零开销直接返回。"""
    global _active_database_url, _active_schema, _truncate_sql

    if _active_schema is None or _active_database_url is None:
        return

    database_url = _active_database_url
    schema = _active_schema
    _active_database_url = None
    _active_schema = None
    _truncate_sql = None

    # 正常在 pytest_sessionfinish 或 atexit 中处于无运行事件循环状态；
    # 若意外处于活动事件循环线程中，退回到独立线程执行清理，避免 asyncio.run 报错。
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_drop_schema_async(database_url, schema))
        return

    error_box: list[BaseException] = []

    def _run_in_thread() -> None:
        try:
            asyncio.run(_drop_schema_async(database_url, schema))
        except BaseException as exc:  # pragma: no cover - 仅兜底异常传播
            error_box.append(exc)

    thread = threading.Thread(target=_run_in_thread, daemon=True)
    thread.start()
    thread.join(timeout=30.0)
    if error_box:
        raise error_box[0]


__all__ = [
    "cleanup_pg_unit_schema",
    "create_pg_unit_db",
    "ensure_pg_unit_schema",
    "pg_unit_connect_args",
    "pg_unit_db",
    "prepare_pg_unit_cli_target",
    "require_pg_test_database_url",
    "reset_pg_unit_data",
    "temp_pg_schema",
]
