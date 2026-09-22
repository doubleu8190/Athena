"""工具治理配置 SQLite 仓库。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update

from athena.infrastructure.postgre.engine import get_session
from athena.infrastructure.postgre.models import ToolModel
from athena.models import ToolConfig

from .model_converters import _row_to_tool_config
from .repository_utils import _json_dumps, _now_iso


class ToolRepository:
    """工具治理配置表 CRUD 操作。"""

    async def upsert(
        self,
        tool_name: str,
        execution_mode: str,
        server_name: str | None = None,
        remote_name: str | None = None,
        description: str = "",
        parameters: dict[str, Any] | None = None,
        risk_level: str = "medium",
        require_approval: bool = True,
        enabled: bool = True,
    ) -> None:
        """插入或更新工具配置，保留已有治理参数。"""
        now = _now_iso()
        params_json = _json_dumps(parameters or {})
        async with get_session() as session:
            async with session.begin():
                existing = await session.get(ToolModel, tool_name)
                if existing is not None:
                    existing.description = description
                    existing.parameters_schema_json = params_json
                    existing.execution_mode = execution_mode
                    existing.server_name = server_name
                    existing.remote_name = remote_name
                    existing.updated_at = now
                else:
                    session.add(
                        ToolModel(
                            tool_name=tool_name,
                            execution_mode=execution_mode,
                            server_name=server_name,
                            remote_name=remote_name,
                            description=description,
                            parameters_schema_json=params_json,
                            risk_level=risk_level,
                            require_approval=int(require_approval),
                            enabled=int(enabled),
                            created_at=now,
                            updated_at=now,
                        )
                    )

    async def get(self, tool_name: str) -> ToolConfig | None:
        """获取单个工具配置。"""
        async with get_session() as session:
            row = await session.get(ToolModel, tool_name)
            return None if row is None else _row_to_tool_config(row)

    async def list_all(self) -> list[ToolConfig]:
        """列出所有工具配置。"""
        async with get_session() as session:
            result = await session.execute(
                select(ToolModel).order_by(ToolModel.created_at.asc())
            )
            return [_row_to_tool_config(row) for row in result.scalars().all()]

    async def update(
        self,
        tool_name: str,
        risk_level: str | None = None,
        require_approval: bool | None = None,
        enabled: bool | None = None,
    ) -> None:
        """更新工具治理参数。"""
        values: dict[str, Any] = {"updated_at": _now_iso()}
        if risk_level is not None:
            values["risk_level"] = risk_level
        if require_approval is not None:
            values["require_approval"] = int(require_approval)
        if enabled is not None:
            values["enabled"] = int(enabled)
        if len(values) <= 1:
            return

        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolModel)
                    .where(ToolModel.tool_name == tool_name)
                    .values(**values)
                )
