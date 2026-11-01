"""Lifecycle-managed LangGraph PostgreSQL checkpointer."""

from __future__ import annotations

from asyncio import Lock

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


class PostgresCheckpointer:
    """Own a health-checked connection pool shared by Root graphs."""

    def __init__(self, conn_string: str) -> None:
        """保存数据库连接信息，并初始化资源生命周期锁。

        参数：
        - `conn_string`（str）：checkpointer 使用的 PostgreSQL 连接串；不能为空。

        返回：
        - None：对象初始化完成。

        异常：
        - ValueError：连接串为空。
        """
        if not conn_string:
            raise ValueError("conn_string is required")
        self.conn_string = conn_string
        self._pool: AsyncConnectionPool | None = None
        self._saver: AsyncPostgresSaver | None = None
        self._lifecycle_lock = Lock()

    @property
    def saver(self) -> AsyncPostgresSaver:
        """返回已启动的 checkpointer。

        参数：无。

        返回：
        - AsyncPostgresSaver：使用健康检查连接池的 LangGraph saver。

        异常：
        - RuntimeError：资源尚未启动。
        """
        if self._saver is None:
            raise RuntimeError("PostgresCheckpointer has not been started")
        return self._saver

    def get_saver(self) -> AsyncPostgresSaver:
        """按依赖端口返回已启动的 checkpointer。"""
        return self.saver

    async def start(self) -> None:
        """打开连接池、等待连接可用并初始化 LangGraph checkpoint 表。

        参数：无。

        返回：
        - None：连接池和 saver 已就绪。

        异常：
        - psycopg 异常：数据库不可用或连接池无法建立连接时传播。
        - 其他异常：checkpoint 初始化失败时传播。
        """
        async with self._lifecycle_lock:
            if self._pool is not None:
                return

            pool = AsyncConnectionPool(
                conninfo=self.conn_string,
                min_size=1,
                open=False,
                check=AsyncConnectionPool.check_connection,
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                },
            )
            try:
                await pool.open()
                await pool.wait()
                saver = AsyncPostgresSaver(pool)
                await saver.setup()
            except BaseException:
                await pool.close()
                raise

            self._pool = pool
            self._saver = saver

    async def stop(self) -> None:
        """清除 saver 并关闭连接池。

        参数：无。

        返回：
        - None：连接池已关闭；未启动时不执行操作。

        异常：
        - psycopg 异常：连接池关闭失败时传播。
        """
        async with self._lifecycle_lock:
            pool, self._pool = self._pool, None
            self._saver = None
            if pool is not None:
                await pool.close()


__all__ = ["PostgresCheckpointer"]
