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
        self,
        session: Session,
        *,
        request_id: str,
        parameters: dict[str, object],
    ) -> tuple[Session, bool]:
        """Create or replay a session under an idempotency receipt."""
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

    async def list_recent_conversation_before_sequence(
        self, session_id: str, *, before_sequence: int, limit: int
    ) -> list[Message]:
        """Return conversation messages belonging to earlier Run sequence numbers."""
        ...

    async def list_after(self, session_id: str, after_id: str) -> list[Message]:
        """返回指定消息之后的消息。"""
        ...
