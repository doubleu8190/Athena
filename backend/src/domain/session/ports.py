"""会话目录与可见性仓储端口。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from domain.session.model.entity import Session, SessionPage, SessionScope


class SessionRepository(Protocol):
    """按所有者读写未删除会话的持久化端口。"""

    async def create(self, session: Session) -> Session:
        """保存新会话。

        绑定项目时，必须在写入事务内确认项目属于同一 owner 且未删除；不可见
        的项目按不存在处理。
        """

    async def get_visible(self, scope: SessionScope) -> Session | None:
        """返回作用域内未删除的会话；不存在、已删除或 owner 不匹配时返回 None。"""

    async def list_active(
        self,
        owner_id: str,
        *,
        project_id: str | None,
        unbound: bool,
        offset: int,
        limit: int,
    ) -> SessionPage:
        """分页读取未删除会话。"""

    async def update(
        self,
        scope: SessionScope,
        *,
        update_title: bool,
        title: str | None,
        update_project: bool,
        project_id: str | None,
        updated_at: datetime,
    ) -> Session | None:
        """在行锁内更新会话标题或项目绑定。

        修改或解绑项目时，已有消息的会话必须抛出 SessionProjectLockedError。
        新绑定的项目必须在同一事务内属于同一 owner 且未删除。
        """

    async def soft_delete(
        self,
        owner_id: str,
        session_id: str,
        deleted_at: datetime,
    ) -> bool:
        """软删除会话；已删除但仍属于该 owner 的会话仍返回 True。"""


__all__ = ["SessionRepository"]
