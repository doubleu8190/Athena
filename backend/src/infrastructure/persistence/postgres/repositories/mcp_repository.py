"""新 MCP Server PostgreSQL repository。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domain.tools import MCPServer

from ..models import MCPServerModel
from .converters import mcp_server_domain_to_model, mcp_server_model_to_domain


class PostgresMCPServerRepository:
    """通过注入会话工厂读写 MCP 服务端配置。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def upsert(self, server: MCPServer) -> MCPServer:
        """插入或复活 MCP 服务端配置。"""
        async with self._session_factory() as db:
            async with db.begin():
                current = await db.get(MCPServerModel, server.name)
                values = mcp_server_domain_to_model(server)
                if current is None:
                    db.add(values)
                else:
                    current.config_json = values.config_json
                    current.deleted_time = None
        return server

    async def get(self, name: str) -> MCPServer | None:
        """查询未删除 MCP 服务端。"""
        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(MCPServerModel).where(
                        MCPServerModel.name == name,
                        MCPServerModel.deleted_time.is_(None),
                    )
                )
            ).scalar_one_or_none()
        return None if row is None else mcp_server_model_to_domain(row)

    async def list_all(self) -> list[MCPServer]:
        """按创建时间列出未删除 MCP 服务端。"""
        async with self._session_factory() as db:
            result = await db.execute(
                select(MCPServerModel)
                .where(MCPServerModel.deleted_time.is_(None))
                .order_by(MCPServerModel.created_at.asc())
            )
            rows = result.scalars().all()
        return [mcp_server_model_to_domain(row) for row in rows]

    async def delete(self, name: str) -> bool:
        """软删除 MCP 服务端。"""
        async with self._session_factory() as db:
            async with db.begin():
                result = await db.execute(
                    update(MCPServerModel)
                    .where(
                        MCPServerModel.name == name,
                        MCPServerModel.deleted_time.is_(None),
                    )
                    .values(deleted_time=datetime.now(timezone.utc).isoformat())
                    .returning(MCPServerModel.name)
                )
                deleted_name = result.scalar_one_or_none()
                if deleted_name is not None:
                    return True
                # Some injected sessions (and database drivers without a
                # RETURNING result) expose only the affected-row count.
                return bool(getattr(result, "rowcount", 0))
