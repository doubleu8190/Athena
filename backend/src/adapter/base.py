"""PostgreSQL SQLAlchemy 声明式模型共享基类。"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """全部 PostgreSQL ORM 适配器模型共用的声明式基类。"""


__all__ = ["Base"]
