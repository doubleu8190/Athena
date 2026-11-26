"""消息持久化端口。"""

from __future__ import annotations

from typing import Protocol

from domain.message.model.entity import ConversationMessagePage


class MessageRepository(Protocol):
    """公开会话历史的只读查询端口（消息写入统一经由 Run 事务路径执行）。"""

    async def list_conversation_for_owner(
        self,
        session_id: str,
        owner_id: str,
        *,
        before_sequence: int | None,
        limit: int,
    ) -> ConversationMessagePage | None:
        """按所有者读取一页会话消息；会话不可见时返回 None。"""


__all__ = ["MessageRepository"]
