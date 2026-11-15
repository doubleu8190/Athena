"""同步记忆控制所需的固定 Run 用户证据读取。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.message.converter.message import deserialize_parts
from adapter.message.model import ConversationMessageModel
from adapter.run.model import RunModel
from adapter.session.model import SessionModel
from common.date import as_utc
from domain.memory.lifecycle import ControlSourceSnapshot, MemorySourceMessage
from domain.message import ConversationRole, TextPart


class MemoryControlSourceRepository:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
    """从数据库可信范围读取当前 Run 的用户直接表达，混入 MemoryRepository。"""

    async def read_control_source(
        self,
        *,
        owner_id: str,
        session_id: str,
        run_id: str,
    ) -> ControlSourceSnapshot | None:
        """复核 Run、Session 和固定项目后返回无模型用户消息。"""
        async with self._factory() as session:
            run = await session.scalar(
                select(RunModel).where(
                    RunModel.id == run_id,
                    RunModel.session_id == session_id,
                    RunModel.owner_id == owner_id,
                )
            )
            if run is None:
                return None
            if run.auth_session_id is None:
                return None
            chat_session = await session.get(SessionModel, session_id)
            if (
                chat_session is None
                or chat_session.deleted_time is not None
                or chat_session.owner_id != owner_id
                or chat_session.project_id != run.memory_project_id
            ):
                return None
            ids = tuple(run.memory_input_message_ids or (run.trigger_message_id,))
            rows = (
                await session.scalars(
                    select(ConversationMessageModel)
                    .where(
                        ConversationMessageModel.id.in_(ids),
                        ConversationMessageModel.session_id == session_id,
                        ConversationMessageModel.role == ConversationRole.USER.value,
                    )
                    .order_by(
                        ConversationMessageModel.created_at,
                        ConversationMessageModel.id,
                    )
                )
            ).all()
            messages: list[MemorySourceMessage] = []
            for row in rows:
                try:
                    parts = deserialize_parts(row.parts)
                except ValueError:
                    return None
                text = "".join(part.text for part in parts if isinstance(part, TextPart))
                if not text.strip():
                    continue
                messages.append(
                    MemorySourceMessage(
                        message_id=row.id,
                        text=text,
                        source_at=as_utc(row.completed_at or row.created_at),
                    )
                )
            if not messages:
                return None
        return ControlSourceSnapshot(
            owner_id=owner_id,
            auth_session_id=run.auth_session_id,
            session_id=session_id,
            project_id=run.memory_project_id,
            run_id=run_id,
            messages=tuple(messages),
        )


__all__ = ["MemoryControlSourceRepository"]
