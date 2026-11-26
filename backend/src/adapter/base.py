"""SQLAlchemy 声明式模型共享基类与跨方言列类型辅助。"""

from __future__ import annotations

from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import DeclarativeBase


@compiles(TSVECTOR, "sqlite")
def _compile_tsvector_for_sqlite(_type: TSVECTOR, _compiler: object, **_kwargs: object) -> str:
    """让含 PostgreSQL 全文检索列（如 KnowledgeChunkModel）的元数据可供 SQLite 测试建表。"""
    return "TEXT"


def json_column_type() -> JSON:
    """构造在 PostgreSQL 使用 JSONB、在 SQLite 测试中回退普通 JSON 的列类型。"""
    return JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    """项目与消息等适配器模型共用的声明式基类。"""


__all__ = ["Base", "json_column_type"]
