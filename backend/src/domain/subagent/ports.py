"""Subagent 配置持久化与外部目录查询端口。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from domain.access import UserAccessSnapshot, VisibilityGrantInput
from domain.auth import AuthContext
from domain.subagent.model import (
    AvailableToolGroup,
    SubagentPage,
    SubagentProfile,
    SubagentUpdatePatch,
)


class SubagentRepository(Protocol):
    """Subagent 配置持久化仓储端口。"""

    async def create(
        self,
        actor: AuthContext,
        profile: SubagentProfile,
        *,
        visibility_grants: Sequence[VisibilityGrantInput] = (),
    ) -> SubagentProfile:
        """在事务内复核管理员身份后创建 Subagent 配置及可见性授权。"""
        ...

    async def get(self, subagent_id: str) -> SubagentProfile | None:
        """按 ID 读取未软删除的 Subagent 配置。"""
        ...

    async def get_by_slug(self, slug: str) -> SubagentProfile | None:
        """按 slug 读取未软删除的 Subagent 配置。"""
        ...

    async def list_page(
        self,
        *,
        offset: int,
        limit: int,
        enabled: bool | None = None,
    ) -> SubagentPage:
        """分页读取未软删除的 Subagent 列表，支持按 enabled 过滤。"""
        ...

    async def list_readable_enabled(
        self,
        snapshot: UserAccessSnapshot | None,
    ) -> tuple[SubagentProfile, ...]:
        """按用户权限快照下推读取未软删除且已启用的可读 Subagent 列表。"""
        ...

    async def update(
        self,
        actor: AuthContext,
        subagent_id: str,
        *,
        expected_version: int,
        patch: SubagentUpdatePatch,
    ) -> SubagentProfile:
        """在事务内复核管理员身份并按乐观锁更新 Subagent 配置。"""
        ...

    async def set_enabled(
        self,
        actor: AuthContext,
        subagent_id: str,
        enabled: bool,
        *,
        updated_at: datetime,
    ) -> SubagentProfile:
        """在事务内复核管理员身份并切换启停状态。"""
        ...

    async def soft_delete(
        self,
        actor: AuthContext,
        subagent_id: str,
        *,
        deleted_at: datetime,
    ) -> None:
        """在事务内复核管理员身份并执行幂等软删除。"""
        ...


class SubagentModelChecker(Protocol):
    """检查模型配置是否存在且处于启用状态。"""

    async def is_model_available(self, model_profile_id: str) -> bool:
        """返回指定模型配置 ID 是否存在且已启用。"""
        ...


class SubagentToolCatalog(Protocol):
    """聚合 Subagent 可用工具目录（内置工具 + 已注册 MCP 工具）。"""

    async def list_available_tool_groups(self) -> tuple[AvailableToolGroup, ...]:
        """返回按来源分组且排除嵌套调度工具的可选工具目录。"""
        ...

    async def list_available_tool_ids(self) -> frozenset[str]:
        """返回所有允许被 Subagent 选用的工具 ID 集合。"""
        ...


__all__ = [
    "SubagentModelChecker",
    "SubagentRepository",
    "SubagentToolCatalog",
]
