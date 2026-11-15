"""系统提示词版本与当前指针的显式持久化端口。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from domain.auth import AuthContext
from domain.system_prompt.model import (
    SystemPromptKind,
    SystemPromptVersion,
    SystemPromptVersionPage,
)


class SystemPromptRepository(Protocol):
    """管理员版本管理及运行期只读解析端口。"""

    async def get_current(self, kind: SystemPromptKind) -> SystemPromptVersion | None:
        """读取指定类别的当前生效版本。"""

    async def list_versions(
        self,
        kind: SystemPromptKind,
        *,
        page: int,
        page_size: int,
    ) -> SystemPromptVersionPage:
        """按创建时间和版本标识稳定倒序读取版本分页。"""
        ...

    async def get_version(
        self, kind: SystemPromptKind, version_id: str
    ) -> SystemPromptVersion | None:
        """读取同类别版本，跨类别标识视为不存在。"""

    async def save(
        self,
        kind: SystemPromptKind,
        *,
        content: str,
        expected_current_version_id: str,
        actor: AuthContext,
        created_at: datetime,
    ) -> SystemPromptVersion:
        """比较当前指针后原子创建并切换新版本。"""
        ...

    async def restore(
        self,
        kind: SystemPromptKind,
        *,
        source_version_id: str,
        expected_current_version_id: str,
        actor: AuthContext,
        created_at: datetime,
    ) -> SystemPromptVersion:
        """从同类别历史正文创建新版本并原子切换当前指针。"""
        ...

    async def get_by_id(self, version_id: str) -> SystemPromptVersion | None:
        """按不可变版本标识解析正文，供 Run 执行使用。"""

    async def get_pair_by_ids(
        self, main_version_id: str, compression_version_id: str
    ) -> tuple[SystemPromptVersion, SystemPromptVersion] | None:
        """一次读取 Run 绑定的两类正文并校验类别，任一侧缺失返回空。"""


__all__ = ["SystemPromptRepository"]
