"""迁移期 PostgreSQL ORM 基类。"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """新持久化模型的 SQLAlchemy 声明基类。"""

