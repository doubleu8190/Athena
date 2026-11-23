"""后端 PostgreSQL 集成测试共享的隔离 Schema 与 Alembic 迁移夹具。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from secrets import token_hex
from typing import Literal, cast

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
_SCHEMA_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_SUBPROCESS_BOOTSTRAP = (
    "from integration.postgres_harness import _run_alembic_from_env; _run_alembic_from_env()"
)


def _validate_schema_name(schema: str) -> None:
    """校验隔离 schema 名称，防止 SQL 标识符注入或超出 PostgreSQL 63 字节上限。"""
    if not _SCHEMA_NAME_PATTERN.fullmatch(schema):
        raise ValueError(f"invalid_postgres_schema_name:{schema!r}")


def _merge_isolated_server_settings(
    schema: str,
    *overrides: Mapping[str, str] | None,
) -> dict[str, str]:
    """合并 PostgreSQL server_settings，禁止覆盖隔离 schema 的 search_path。"""
    merged: dict[str, str] = {}
    for mapping in overrides:
        if not mapping:
            continue
        if "search_path" in mapping:
            raise ValueError("cannot_override_isolated_search_path")
        merged.update(mapping)
    # 固定在末尾写入 search_path，确保隔离 schema 优先级不可被调用方参数改写。
    merged["search_path"] = f"{schema},public"
    return merged


def build_alembic_config(
    database_url: str,
    schema: str,
    *,
    connect_args: Mapping[str, object] | None = None,
    server_settings: Mapping[str, str] | None = None,
    alembic_ini: Path = ALEMBIC_INI,
) -> Config:
    """构造指向指定隔离 schema 的 Alembic 配置，并转义 URL 中的百分号。"""
    _validate_schema_name(schema)
    merged_server_settings = _merge_isolated_server_settings(schema, server_settings)
    merged_connect_args: dict[str, object] = {
        **(connect_args or {}),
        "server_settings": merged_server_settings,
    }
    config = Config(str(alembic_ini))
    # ConfigParser 会将单个 % 视为插值占位符，必须转义为 %% 以兼容含转义字符的密码。
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    config.attributes["connect_args"] = merged_connect_args
    config.attributes["version_table_schema"] = schema
    return config


def _execute_alembic_sync(
    action: Literal["upgrade", "downgrade"],
    config: Config,
    target_revision: str,
) -> None:
    """同步执行单次 Alembic 升级或降级。"""
    if action == "upgrade":
        command.upgrade(config, target_revision)
        return
    command.downgrade(config, target_revision)


def _run_alembic_from_env() -> None:
    """供子进程隔离模式调用的 Alembic 入口，避免 fileConfig 污染 pytest 日志配置。"""
    action_raw = os.environ["ATHENA_HARNESS_ALEMBIC_ACTION"]
    if action_raw not in {"upgrade", "downgrade"}:
        raise ValueError(f"unsupported_alembic_action:{action_raw!r}")
    action = cast(Literal["upgrade", "downgrade"], action_raw)
    database_url = os.environ["ATHENA_HARNESS_DATABASE_URL"]
    schema = os.environ["ATHENA_HARNESS_SCHEMA"]
    target_revision = os.environ["ATHENA_HARNESS_REVISION"]
    alembic_ini = Path(os.environ["ATHENA_HARNESS_ALEMBIC_INI"])
    connect_args = cast(
        dict[str, object] | None,
        json.loads(os.environ["ATHENA_HARNESS_CONNECT_ARGS"]),
    )
    server_settings = cast(
        dict[str, str] | None,
        json.loads(os.environ["ATHENA_HARNESS_SERVER_SETTINGS"]),
    )
    config = build_alembic_config(
        database_url,
        schema,
        connect_args=connect_args,
        server_settings=server_settings,
        alembic_ini=alembic_ini,
    )
    _execute_alembic_sync(action, config, target_revision)


def _run_alembic_subprocess_sync(
    action: Literal["upgrade", "downgrade"],
    database_url: str,
    schema: str,
    target_revision: str,
    *,
    connect_args: Mapping[str, object] | None,
    server_settings: Mapping[str, str] | None,
    alembic_ini: Path,
) -> None:
    """在独立子进程中执行 Alembic 命令，防止 logging.config.fileConfig 影响测试进程。"""
    child_env = os.environ.copy()
    existing_pythonpath = child_env.get("PYTHONPATH", "")
    pythonpath_entries = [str(BACKEND_DIR / "tests"), str(BACKEND_DIR / "src")]
    if existing_pythonpath:
        pythonpath_entries.append(existing_pythonpath)
    child_env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    child_env["ATHENA_HARNESS_ALEMBIC_ACTION"] = action
    child_env["ATHENA_HARNESS_DATABASE_URL"] = database_url
    child_env["ATHENA_HARNESS_SCHEMA"] = schema
    child_env["ATHENA_HARNESS_REVISION"] = target_revision
    child_env["ATHENA_HARNESS_ALEMBIC_INI"] = str(alembic_ini)
    child_env["ATHENA_HARNESS_CONNECT_ARGS"] = json.dumps(
        dict(connect_args) if connect_args is not None else None
    )
    child_env["ATHENA_HARNESS_SERVER_SETTINGS"] = json.dumps(
        dict(server_settings) if server_settings is not None else None
    )
    result = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_BOOTSTRAP],
        cwd=BACKEND_DIR,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    # 报错信息仅输出动作、目标 revision 与退出码，避免泄露包含凭据的数据库连接串。
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic {action}({target_revision}) subprocess failed with code {result.returncode}"
        )


async def _run_alembic(
    action: Literal["upgrade", "downgrade"],
    database_url: str,
    schema: str,
    target_revision: str,
    *,
    config: Config,
    connect_args: Mapping[str, object] | None,
    server_settings: Mapping[str, str] | None,
    alembic_ini: Path,
    use_subprocess: bool,
) -> None:
    """在后台线程中异步调度同进程或子进程 Alembic 迁移。"""
    if use_subprocess:
        await asyncio.to_thread(
            _run_alembic_subprocess_sync,
            action,
            database_url,
            schema,
            target_revision,
            connect_args=connect_args,
            server_settings=server_settings,
            alembic_ini=alembic_ini,
        )
        return
    await asyncio.to_thread(_execute_alembic_sync, action, config, target_revision)


async def _create_schema(admin_engine: AsyncEngine, schema: str) -> None:
    """在独立事务中创建隔离 PostgreSQL schema。"""
    _validate_schema_name(schema)
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))


async def _drop_schema(admin_engine: AsyncEngine, schema: str) -> None:
    """在独立事务中级联删除隔离 PostgreSQL schema。"""
    _validate_schema_name(schema)
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@dataclass(frozen=True, slots=True)
class IsolatedPostgresSchema:
    """单个集成测试独占的 PostgreSQL schema、引擎、会话工厂与迁移控制器。"""

    schema: str
    engine: AsyncEngine
    admin_engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    alembic_config: Config
    _database_url: str
    _connect_args: Mapping[str, object] | None = None
    _alembic_server_settings: Mapping[str, str] | None = None
    _alembic_ini: Path = ALEMBIC_INI
    _use_subprocess: bool = False

    def head_revision(self) -> str | None:
        """读取当前迁移脚本目录对应的 head 版本号。"""
        return ScriptDirectory.from_config(self.alembic_config).get_current_head()

    async def upgrade_to(self, target_revision: str = "head") -> None:
        """将当前隔离 schema 升级至目标 revision。"""
        await _run_alembic(
            "upgrade",
            self._database_url,
            self.schema,
            target_revision,
            config=self.alembic_config,
            connect_args=self._connect_args,
            server_settings=self._alembic_server_settings,
            alembic_ini=self._alembic_ini,
            use_subprocess=self._use_subprocess,
        )

    async def downgrade_to(self, target_revision: str) -> None:
        """将当前隔离 schema 降级至目标 revision。"""
        await _run_alembic(
            "downgrade",
            self._database_url,
            self.schema,
            target_revision,
            config=self.alembic_config,
            connect_args=self._connect_args,
            server_settings=self._alembic_server_settings,
            alembic_ini=self._alembic_ini,
            use_subprocess=self._use_subprocess,
        )


@asynccontextmanager
async def isolated_postgres_schema(
    database_url: str,
    *,
    prefix: str,
    revision: str | None = "head",
    token_bytes: int = 8,
    connect_args: Mapping[str, object] | None = None,
    server_settings: Mapping[str, str] | None = None,
    engine_server_settings: Mapping[str, str] | None = None,
    pool_size: int | None = None,
    use_subprocess: bool = False,
    alembic_ini: Path = ALEMBIC_INI,
) -> AsyncIterator[IsolatedPostgresSchema]:
    """创建随机隔离 PostgreSQL schema，按需迁移并在退出时彻底清理。

    Args:
        database_url: PostgreSQL 异步连接串。
        prefix: 随机 schema 名称前缀。
        revision: 进入上下文时自动升级到的 Alembic revision；传 None 时仅创建空 schema。
        token_bytes: 随机后缀字节数（默认 8 字节，即 16 位十六进制字符）。
        connect_args: 传递给 asyncpg 的附加连接参数（如 timeout、command_timeout）。
        server_settings: 同时注入 Alembic 与业务引擎的 PostgreSQL server_settings。
        engine_server_settings: 仅注入业务引擎的 server_settings（如 application_name）。
        pool_size: 非空时启用受限连接池（max_overflow=0, pool_timeout=0.5）；默认 NullPool。
        use_subprocess: 为 True 时在独立子进程中执行 Alembic，隔离 logging 配置。
        alembic_ini: Alembic 配置文件路径，默认指向 backend/alembic.ini。
    """
    schema = f"{prefix}_{token_hex(token_bytes)}"
    _validate_schema_name(schema)

    # 先校验并合并 server_settings，再创建引擎，避免参数非法时遗留未释放引擎。
    merged_engine_server_settings = _merge_isolated_server_settings(
        schema,
        server_settings,
        engine_server_settings,
    )
    alembic_config = build_alembic_config(
        database_url,
        schema,
        connect_args=connect_args,
        server_settings=server_settings,
        alembic_ini=alembic_ini,
    )

    admin_kwargs: dict[str, object] = {"poolclass": NullPool}
    if connect_args:
        admin_kwargs["connect_args"] = dict(connect_args)
    admin_engine = create_async_engine(database_url, **admin_kwargs)

    engine_kwargs: dict[str, object] = {
        "connect_args": {
            **(connect_args or {}),
            "server_settings": merged_engine_server_settings,
        }
    }
    if pool_size is None:
        engine_kwargs["poolclass"] = NullPool
    else:
        # 受限连接池配置用于验证长耗时操作前及时归还连接，避免池耗尽回归。
        engine_kwargs.update(
            {
                "pool_size": pool_size,
                "max_overflow": 0,
                "pool_timeout": 0.5,
            }
        )
    engine = create_async_engine(database_url, **engine_kwargs)
    harness = IsolatedPostgresSchema(
        schema=schema,
        engine=engine,
        admin_engine=admin_engine,
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        alembic_config=alembic_config,
        _database_url=database_url,
        _connect_args=connect_args,
        _alembic_server_settings=server_settings,
        _alembic_ini=alembic_ini,
        _use_subprocess=use_subprocess,
    )

    schema_created = False
    try:
        await _create_schema(admin_engine, schema)
        schema_created = True
        if revision is not None:
            await harness.upgrade_to(revision)
        yield harness
    finally:
        # 即使释放业务连接池失败或被取消，仍必须级联删除隔离 schema 并关闭管理引擎。
        try:
            await engine.dispose()
        finally:
            try:
                if schema_created:
                    await _drop_schema(admin_engine, schema)
            finally:
                await admin_engine.dispose()


@contextmanager
def sync_isolated_postgres_schema(
    database_url: str,
    *,
    prefix: str,
    revision: str | None = "head",
    token_bytes: int = 8,
    connect_args: Mapping[str, object] | None = None,
    server_settings: Mapping[str, str] | None = None,
    use_subprocess: bool = False,
    alembic_ini: Path = ALEMBIC_INI,
) -> Iterator[str]:
    """同步上下文版本：用于模块级 pytest fixture 创建隔离 schema 并在退出时彻底删除。"""
    schema = f"{prefix}_{token_hex(token_bytes)}"
    _validate_schema_name(schema)
    _merge_isolated_server_settings(schema, server_settings)
    admin_kwargs: dict[str, object] = {"poolclass": NullPool}
    if connect_args:
        admin_kwargs["connect_args"] = dict(connect_args)

    async def _setup() -> None:
        admin_engine = create_async_engine(database_url, **admin_kwargs)
        schema_created = False
        try:
            await _create_schema(admin_engine, schema)
            schema_created = True
            if revision is not None:
                config = build_alembic_config(
                    database_url,
                    schema,
                    connect_args=connect_args,
                    server_settings=server_settings,
                    alembic_ini=alembic_ini,
                )
                await _run_alembic(
                    "upgrade",
                    database_url,
                    schema,
                    revision,
                    config=config,
                    connect_args=connect_args,
                    server_settings=server_settings,
                    alembic_ini=alembic_ini,
                    use_subprocess=use_subprocess,
                )
        except BaseException:
            if schema_created:
                await _drop_schema(admin_engine, schema)
            raise
        finally:
            await admin_engine.dispose()

    async def _teardown() -> None:
        admin_engine = create_async_engine(database_url, **admin_kwargs)
        try:
            await _drop_schema(admin_engine, schema)
        finally:
            await admin_engine.dispose()

    asyncio.run(_setup())
    try:
        yield schema
    finally:
        asyncio.run(_teardown())


__all__ = [
    "ALEMBIC_INI",
    "BACKEND_DIR",
    "IsolatedPostgresSchema",
    "build_alembic_config",
    "isolated_postgres_schema",
    "sync_isolated_postgres_schema",
]
