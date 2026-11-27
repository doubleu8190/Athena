"""飞书通讯录拉取端口。"""

from __future__ import annotations

from typing import Protocol

from domain.org.model import (
    RemoteDepartment,
    RemoteEmployee,
)


class FeishuDirectoryClient(Protocol):
    """通讯录同步所需的飞书只读拉取端口。"""

    async def list_departments(self) -> list[RemoteDepartment]:
        """分页拉取全部可见部门列表。"""

    async def list_department_users(self, department_external_id: str) -> list[RemoteEmployee]:
        """分页拉取指定部门下直属员工列表。"""


__all__ = [
    "FeishuDirectoryClient",
]
