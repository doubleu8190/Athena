"""所有进程共享的数据库连接池构造。"""

from __future__ import annotations

from bootstrap.config import env_int

# 模块级导入具体实现，便于测试替换 Database 并让函数内引用同一全局名。
from infrastructure.database import (
    DEFAULT_DATABASE_MAX_OVERFLOW,
    DEFAULT_DATABASE_POOL_SIZE,
    Database,
)


def build_database(url: str) -> Database:
    """按统一环境变量创建有界连接池。

    API、Run Worker 与 Knowledge Worker 都读取同一组连接池配置，避免不同
    进程对数据库容量做出彼此矛盾的假设。
    """
    return Database(
        url,
        pool_size=env_int(
            "ATHENA_DATABASE_POOL_SIZE",
            default=DEFAULT_DATABASE_POOL_SIZE,
            minimum=1,
        ),
        max_overflow=env_int(
            "ATHENA_DATABASE_MAX_OVERFLOW",
            default=DEFAULT_DATABASE_MAX_OVERFLOW,
            minimum=0,
        ),
    )


__all__ = ["build_database"]
