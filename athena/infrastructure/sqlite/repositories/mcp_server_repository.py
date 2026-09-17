"""MCP 服务端配置 SQLite 仓库。"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from athena.infrastructure.sqlite.engine import get_core_session
from athena.infrastructure.sqlite.models import McpServerModel
from athena.models import McpServer, McpServerConfig

from .model_converters import _row_to_mcp_server
from .repository_utils import _json_dumps, _now_iso


class McpServerRepository:
    """MCP 服务器配置表 CRUD 操作（软删除）。"""

    async def upsert(self, name: str, config: McpServerConfig) -> None:
        """插入或覆盖 MCP 服务器配置，并复活软删除记录。"""
        now = _now_iso()
        config_json = _json_dumps(config.model_dump())
        async with get_core_session() as session:
            async with session.begin():
                stmt = sqlite_insert(McpServerModel).values(
                    name=name,
                    config_json=config_json,
                    created_at=now,
                )
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[McpServerModel.name],
                        set_={
                            "config_json": stmt.excluded.config_json,
                            "deleted_time": None,
                        },
                    )
                )

    async def get(self, name: str, include_deleted: bool = False) -> McpServer | None:
        """获取单个 MCP 服务器配置。"""
        async with get_core_session() as session:
            stmt = select(McpServerModel).where(McpServerModel.name == name)
            if not include_deleted:
                stmt = stmt.where(McpServerModel.deleted_time.is_(None))
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return None if row is None else _row_to_mcp_server(row)

    async def list_all(self, include_deleted: bool = False) -> list[McpServer]:
        """列出所有 MCP 服务器配置。"""
        async with get_core_session() as session:
            stmt = select(McpServerModel).order_by(McpServerModel.created_at.asc())
            if not include_deleted:
                stmt = stmt.where(McpServerModel.deleted_time.is_(None))
            result = await session.execute(stmt)
            return [_row_to_mcp_server(row) for row in result.scalars().all()]

    async def soft_delete(self, name: str) -> None:
        """软删除 MCP 服务器配置。"""
        async with get_core_session() as session:
            async with session.begin():
                await session.execute(
                    update(McpServerModel)
                    .where(
                        McpServerModel.name == name,
                        McpServerModel.deleted_time.is_(None),
                    )
                    .values(deleted_time=_now_iso())
                )
