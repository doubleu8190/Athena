"""会话标题生成任务的领域契约。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SessionTitleJob:
    """已领取的标题任务快照。"""

    session_id: str
    source_message_id: str
    expected_title_revision: int
    attempt: int
    attachment_names: tuple[str, ...] = ()


class SessionTitleJobRepository(Protocol):
    """标题任务的持久化与租约操作。"""

    async def claim_next(
        self,
        *,
        now: datetime,
        worker_id: str,
        lease_seconds: int,
    ) -> SessionTitleJob | None: ...

    async def complete(
        self, job: SessionTitleJob, *, title: str, now: datetime
    ) -> bool: ...

    async def fail(
        self,
        job: SessionTitleJob,
        *,
        error_code: str,
        retry_at: datetime | None,
        now: datetime,
    ) -> None: ...


__all__ = ["SessionTitleJob", "SessionTitleJobRepository"]
