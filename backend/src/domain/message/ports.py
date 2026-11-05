"""消息持久化端口。"""

from __future__ import annotations

from typing import Protocol

from domain.message.model.entity import (
    ConversationMessage,
    ConversationMessageDraft,
    ConversationMessagePage,
    ExecutionMessage,
    ExecutionMessageDraft,
)


class MessageRepository(Protocol):
    """会话消息与执行轨迹的持久化端口。"""

    async def append_conversation(
        self,
        draft: ConversationMessageDraft,
    ) -> ConversationMessage:
        """按草稿 owner 幂等追加会话消息并分配 session 内 sequence。"""

    async def append_execution(
        self,
        draft: ExecutionMessageDraft,
    ) -> ExecutionMessage:
        """按草稿 owner 追加执行轨迹消息并分配 session 内 sequence。"""

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
