"""会话领域实体和状态规则。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SessionStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    RECOVERING = "recovering"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Session:
    """与传输和持久化技术无关的会话实体。"""

    id: str
    title: str
    status: SessionStatus
    run_id: str | None
    created_at: datetime
    updated_at: datetime
    compression_summary: str | None = None
    last_compressed_message_id: str | None = None
    last_summarized_message_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        title: str,
        now: datetime,
    ) -> "Session":
        """创建处于空闲状态的新会话。"""
        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("title must not be empty")
        return cls(
            id=session_id,
            title=normalized_title,
            status=SessionStatus.IDLE,
            run_id=None,
            created_at=now,
            updated_at=now,
        )

    def rename(self, title: str, *, now: datetime) -> "Session":
        """返回使用新标题和更新时间的会话副本。"""
        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("title must not be empty")
        return Session(
            id=self.id,
            title=normalized_title,
            status=self.status,
            run_id=self.run_id,
            created_at=self.created_at,
            updated_at=now,
            compression_summary=self.compression_summary,
            last_compressed_message_id=self.last_compressed_message_id,
            last_summarized_message_id=self.last_summarized_message_id,
        )
