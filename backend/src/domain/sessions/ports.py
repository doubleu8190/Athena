"""会话持久化端口。"""

from __future__ import annotations

from typing import Protocol

from .entities import Session
from .messages import Message


class SessionRepository(Protocol):
    """会话持久化能力，由基础设施实现。"""

    async def create(self, session: Session) -> Session:
        """保存新会话并返回持久化结果。"""
        ...

    async def create_idempotent(
        self, session: Session, *, creation_request_id: str
    ) -> Session:
        """幂等创建会话并返回同一请求已经创建的会话。"""
        ...

    async def get(self, session_id: str) -> Session | None:
        """按 ID 查询会话，不存在时返回 None。"""
        ...

    async def list_sessions(self, *, project_id: str | None = None) -> list[Session]:
        """返回可见会话列表。"""
        ...

    async def save(self, session: Session) -> Session:
        """保存已修改会话并返回持久化结果。"""
        ...

    async def rename(self, session: Session) -> Session:
        """只更新标题字段，避免旧会话快照覆盖其他并发更新。"""
        ...

    async def delete(self, session_id: str) -> bool:
        """删除会话并返回是否找到目标。"""
        ...


class MessageRepository(Protocol):
    """会话消息持久化能力，由基础设施实现。"""

    async def save(self, message: Message) -> str:
        """保存消息并返回消息 ID。"""
        ...

    async def save_user_message_with_title(
        self, message: Message, *, fallback_title: str
    ) -> bool:
        """原子保存用户消息、活动时间及首条消息标题任务。"""
        ...

    async def get(self, message_id: str) -> Message | None:
        """按 ID 查询消息，不存在或已删除时返回 None。"""
        ...

    async def list_by_session(
        self,
        session_id: str,
        *,
        limit: int | None = None,
    ) -> list[Message]:
        """按时间顺序返回会话消息。"""
        ...

    async def list_recent_conversation(
        self, session_id: str, *, limit: int
    ) -> list[Message]:
        """返回最近的用户/助手对话消息，并按时间升序排列。"""
        ...

    async def list_after(self, session_id: str, after_id: str) -> list[Message]:
        """返回指定消息之后的消息。"""
        ...
