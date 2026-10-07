"""新工具治理配置 PostgreSQL repository。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.tools import ToolConfig

from ..models import ToolModel
from .converters import tool_domain_to_model, tool_model_to_domain


class PostgresToolConfigRepository:
    """通过注入会话工厂读写工具治理配置。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def upsert(self, config: ToolConfig) -> ToolConfig:
        """插入或覆盖工具配置。"""
        async with self._session_factory() as db:
            async with db.begin():
                current = await db.get(ToolModel, config.tool_name)
                values = tool_domain_to_model(config)
                if current is None:
                    db.add(values)
                else:
                    for field in (
                        "execution_mode",
                        "server_name",
                        "remote_name",
                        "description",
                        "parameters_schema_json",
                        "risk_level",
                        "require_approval",
                        "enabled",
                        "updated_at",
                    ):
                        setattr(current, field, getattr(values, field))
        return config

    async def get(self, tool_name: str) -> ToolConfig | None:
        """查询工具配置。"""
        async with self._session_factory() as db:
            row = await db.get(ToolModel, tool_name)
        return None if row is None else tool_model_to_domain(row)

    async def list_all(self) -> list[ToolConfig]:
        """按创建时间列出工具配置。"""
        async with self._session_factory() as db:
            result = await db.execute(select(ToolModel).order_by(ToolModel.created_at.asc()))
            rows = result.scalars().all()
        return [tool_model_to_domain(row) for row in rows]

    async def update(self, config: ToolConfig) -> ToolConfig:
        """更新工具治理配置并返回最新领域实体。"""
        return await self.upsert(config)
