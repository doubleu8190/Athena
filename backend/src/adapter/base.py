"""SQLAlchemy 声明式模型共享基类。"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """项目与消息等适配器模型共用的声明式基类。"""


__all__ = ["Base"]
