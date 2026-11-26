"""所有进程共享的数据库连接池构造。"""

from __future__ import annotations

from bootstrap.config import DatabaseSettings

# 模块级导入具体实现，便于测试替换 Database 并让函数内引用同一全局名。
from infrastructure.database import Database


def build_database(url: str) -> Database:
    """按统一强类型配置创建有界数据库连接池。

    API、Run Worker 与 Knowledge Worker 共用同一份 DatabaseSettings 校验规则，
    避免不同进程对连接池容量做出彼此矛盾的假设。
    """
    settings = DatabaseSettings.from_environment()
    return Database(
        url,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
    )


__all__ = ["build_database"]
