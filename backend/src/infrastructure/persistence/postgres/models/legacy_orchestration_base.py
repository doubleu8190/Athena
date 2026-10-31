"""暂未改造的旧 Round ORM 映射隔离区。

这些映射只用于让尚待 S7 移除的旧模块可导入，不参与目标 Base.metadata，
也不会由应用启动创建或供新 Orchestrator 查询。
"""

from sqlalchemy.orm import DeclarativeBase


class LegacyOrchestrationBase(DeclarativeBase):
    """不参与目标数据库初始化的旧编排映射基类。"""


__all__ = ["LegacyOrchestrationBase"]
