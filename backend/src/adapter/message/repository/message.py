"""会话消息与执行轨迹的 async SQLAlchemy 仓储。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from adapter.message.converter.message import (
    conversation_to_domain,
    deserialize_parts,
    execution_to_domain,
    serialize_metadata,
    serialize_parts,
)
from adapter.message.model.conversation import ConversationMessageModel
from adapter.message.model.execution import ExecutionMessageModel
from adapter.run.model.run import RunModel
from adapter.session.model import SessionModel
from domain.message import (
    ConversationMessage,
    ConversationMessageDraft,
    ConversationMessagePage,
    ConversationRole,
    ExecutionMessage,
    ExecutionMessageDraft,
    ExecutionRole,
    InvalidMessageReferenceError,
    MessageConflictError,
    SessionNotFoundError,
    ToolCallPart,
    ToolResultPart,
)


def _conversation_matches(
    existing: ConversationMessage,
    draft: ConversationMessageDraft,
) -> bool:
    """判断重复写入是否表达同一业务消息。"""
    return (
        existing.role is draft.role
        and existing.status is draft.status
        and existing.parts == draft.parts
        and existing.reply_to_message_id == draft.reply_to_message_id
        and existing.run_id == draft.run_id
        and existing.client_message_id == draft.client_message_id
        and existing.metadata == draft.metadata
    )


def _execution_matches(
    existing: ExecutionMessage,
    draft: ExecutionMessageDraft,
) -> bool:
    """判断重复工具结果是否表达同一业务事实。"""
    return (
        existing.session_id == draft.session_id
        and existing.run_id == draft.run_id
        and existing.thread_id == draft.thread_id
        and existing.role is draft.role
        and existing.parts == draft.parts
        and existing.tool_call_id == draft.tool_call_id
        and existing.source_message_id == draft.source_message_id
        and existing.metadata == draft.metadata
    )


def _monotonic_updated_at(current: datetime, candidate: datetime) -> datetime:
    """取锁内当前时间与候选时间的较大值，兼容 SQLite 丢失时区。"""
    current_utc = current.replace(tzinfo=UTC) if current.tzinfo is None else current
    candidate_utc = candidate.replace(tzinfo=UTC) if candidate.tzinfo is None else candidate
    return max(current_utc, candidate_utc)


class SqlAlchemyMessageRepository:
    """使用 PostgreSQL 兼容 SQL 持久化两类消息流。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """保存会话工厂。"""
        self._session_factory = session_factory

    async def append_conversation(
        self,
        draft: ConversationMessageDraft,
    ) -> ConversationMessage:
        """在 Session 行锁内幂等写入会话消息。"""
        async with self._session_factory() as session:
            async with session.begin():
                locked_session = await self._lock_session(
                    session,
                    draft.session_id,
                    draft.owner_id,
                )
                await self._ensure_legacy_run_reference(session, draft.run_id)
                result = await self.append_conversation_in_session(
                    session, draft, locked_session=locked_session
                )
        return result

    async def append_conversation_in_session(
        self,
        session: AsyncSession,
        draft: ConversationMessageDraft,
        *,
        locked_session: SessionModel,
        managed_run_id: str | None = None,
    ) -> ConversationMessage:
        """在调用方事务和已锁 Session 内写消息，不提交事务。"""
        if draft.session_id != locked_session.id or draft.owner_id != locked_session.owner_id:
            raise SessionNotFoundError("session_not_found")
        if managed_run_id is not None and managed_run_id != draft.run_id:
            raise InvalidMessageReferenceError("managed_run_reference_invalid")
        existing = await self._find_conversation(session, draft)
        if existing is not None:
            return self._accept_existing_conversation(existing, draft)
        await self._validate_conversation_reference(session, draft)
        sequence = self._next_sequence(locked_session, draft.created_at)
        row = ConversationMessageModel(
            id=draft.id,
            session_id=draft.session_id,
            role=draft.role.value,
            status=draft.status.value,
            sequence=sequence,
            parts=serialize_parts(draft.parts),
            reply_to_message_id=draft.reply_to_message_id,
            run_id=draft.run_id,
            managed_run_id=managed_run_id,
            context_turn_id=draft.context_turn_id,
            client_message_id=draft.client_message_id,
            created_at=draft.created_at,
            completed_at=draft.completed_at,
            metadata_json=serialize_metadata(draft.metadata),
        )
        session.add(row)
        await session.flush()
        return conversation_to_domain(row)

    async def append_execution(
        self,
        draft: ExecutionMessageDraft,
    ) -> ExecutionMessage:
        """在 Session 行锁内校验引用关系并写入执行轨迹。"""
        async with self._session_factory() as session:
            async with session.begin():
                locked_session = await self._lock_session(
                    session,
                    draft.session_id,
                    draft.owner_id,
                )
                await self._ensure_legacy_run_reference(session, draft.run_id)
                result = await self.append_execution_in_session(
                    session, draft, locked_session=locked_session
                )
        return result

    @staticmethod
    async def _ensure_legacy_run_reference(session: AsyncSession, run_id: str | None) -> None:
        """阻止旧公开入口为受管 Run 写入缺少 fencing 的消息。"""
        if (
            run_id is not None
            and await session.scalar(select(RunModel.id).where(RunModel.id == run_id)) is not None
        ):
            raise InvalidMessageReferenceError("managed_run_write_requires_run_command")

    async def append_execution_in_session(
        self,
        session: AsyncSession,
        draft: ExecutionMessageDraft,
        *,
        locked_session: SessionModel,
        managed_run_id: str | None = None,
    ) -> ExecutionMessage:
        """在调用方事务和已锁 Session 内写执行消息，不提交事务。"""
        if draft.session_id != locked_session.id or draft.owner_id != locked_session.owner_id:
            raise SessionNotFoundError("session_not_found")
        if managed_run_id is not None and managed_run_id != draft.run_id:
            raise InvalidMessageReferenceError("managed_run_reference_invalid")
        existing = await self._find_execution_tool_result(session, draft)
        if existing is not None:
            return self._accept_existing_execution(existing, draft)
        await self._validate_source_message(session, draft)
        if draft.role is ExecutionRole.TOOL:
            await self._validate_tool_result_reference(session, draft)
        elif draft.role is ExecutionRole.ASSISTANT:
            await self._validate_tool_call_uniqueness(session, draft)
        sequence = self._next_sequence(locked_session, draft.created_at)
        row = ExecutionMessageModel(
            id=draft.id,
            session_id=draft.session_id,
            run_id=draft.run_id,
            managed_run_id=managed_run_id,
            thread_id=draft.thread_id,
            sequence=sequence,
            role=draft.role.value,
            parts=serialize_parts(draft.parts),
            tool_call_id=draft.tool_call_id,
            source_message_id=draft.source_message_id,
            created_at=draft.created_at,
            metadata_json=serialize_metadata(draft.metadata),
        )
        session.add(row)
        await session.flush()
        return execution_to_domain(row)

    async def list_conversation_for_owner(
        self,
        session_id: str,
        owner_id: str,
        *,
        before_sequence: int | None,
        limit: int,
    ) -> ConversationMessagePage | None:
        """在同一 SQL 快照内校验会话可见性并读取一页公开消息。"""
        # 隔离主聊天历史：先在子查询中过滤掉子 Run（depth >= 1）的澄清提问与定向回答，
        # 再与 SessionModel 做 outerjoin，确保会话存在但仅有子 Run 消息时仍返回空页而非 None。
        visible_messages = (
            select(ConversationMessageModel)
            .outerjoin(RunModel, RunModel.id == ConversationMessageModel.managed_run_id)
            .where(
                ConversationMessageModel.session_id == session_id,
                *(
                    (ConversationMessageModel.sequence < before_sequence,)
                    if before_sequence is not None
                    else ()
                ),
                or_(
                    ConversationMessageModel.managed_run_id.is_(None),
                    RunModel.depth == 0,
                ),
            )
            .order_by(ConversationMessageModel.sequence.desc())
            .limit(limit + 1)
            .subquery()
        )
        visible_msg_alias = aliased(ConversationMessageModel, visible_messages)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(SessionModel.id, visible_msg_alias)
                    .select_from(SessionModel)
                    .outerjoin(visible_msg_alias, visible_msg_alias.session_id == SessionModel.id)
                    .where(
                        SessionModel.id == session_id,
                        SessionModel.owner_id == owner_id,
                        SessionModel.deleted_time.is_(None),
                    )
                    .order_by(visible_msg_alias.sequence.desc())
                )
            ).all()
        if not rows:
            return None

        message_rows = [msg_row for _, msg_row in rows if msg_row is not None]
        has_more = len(message_rows) > limit
        page_rows = message_rows[:limit]
        next_before_sequence = page_rows[-1].sequence if has_more and page_rows else None
        page_rows.reverse()
        return ConversationMessagePage(
            items=tuple(conversation_to_domain(row) for row in page_rows),
            next_before_sequence=next_before_sequence,
        )

    async def _find_conversation(
        self,
        session: AsyncSession,
        draft: ConversationMessageDraft,
    ) -> ConversationMessage | None:
        """在写事务内按客户端幂等键读取已有消息。"""
        if draft.client_message_id is not None:
            row = await session.scalar(
                select(ConversationMessageModel).where(
                    ConversationMessageModel.session_id == draft.session_id,
                    ConversationMessageModel.client_message_id == draft.client_message_id,
                )
            )
            if row is not None:
                return conversation_to_domain(row)
        return None

    async def _find_execution_tool_result(
        self,
        session: AsyncSession,
        draft: ExecutionMessageDraft,
    ) -> ExecutionMessage | None:
        """仅对 tool 结果执行 thread 内幂等查找。"""
        if draft.role is not ExecutionRole.TOOL or draft.tool_call_id is None:
            return None
        row = await session.scalar(
            select(ExecutionMessageModel).where(
                ExecutionMessageModel.session_id == draft.session_id,
                ExecutionMessageModel.run_id == draft.run_id,
                ExecutionMessageModel.thread_id == draft.thread_id,
                ExecutionMessageModel.tool_call_id == draft.tool_call_id,
            )
        )
        return None if row is None else execution_to_domain(row)

    @staticmethod
    async def _lock_session(
        session: AsyncSession,
        session_id: str,
        owner_id: str,
    ) -> SessionModel:
        """锁定当前 owner 的 Session 行，序列化同一 Session 内的消息写入。"""
        locked = await session.scalar(
            select(SessionModel)
            .where(
                SessionModel.id == session_id,
                SessionModel.owner_id == owner_id,
                SessionModel.deleted_time.is_(None),
            )
            .with_for_update()
        )
        if locked is None:
            raise SessionNotFoundError("session_not_found")
        return locked

    @staticmethod
    def _next_sequence(session: SessionModel, updated_at: datetime) -> int:
        """在已锁定的 Session 上分配共享 sequence。"""
        session.last_message_sequence += 1
        session.updated_at = _monotonic_updated_at(session.updated_at, updated_at)
        return session.last_message_sequence

    @staticmethod
    async def _validate_conversation_reference(
        session: AsyncSession,
        draft: ConversationMessageDraft,
    ) -> None:
        """校验 assistant 回复目标为同一 Session 的 user 消息。"""
        if draft.role is not ConversationRole.ASSISTANT:
            if draft.reply_to_message_id is not None:
                raise InvalidMessageReferenceError("reply_to_not_allowed")
            return
        if draft.reply_to_message_id is None:
            return
        reply_to = await session.scalar(
            select(ConversationMessageModel).where(
                ConversationMessageModel.id == draft.reply_to_message_id,
                ConversationMessageModel.session_id == draft.session_id,
            )
        )
        if reply_to is None or reply_to.role != ConversationRole.USER.value:
            raise InvalidMessageReferenceError("reply_to_user_message_not_found")

    @staticmethod
    async def _validate_source_message(
        session: AsyncSession,
        draft: ExecutionMessageDraft,
    ) -> None:
        """校验执行消息的来源为同一 Session 的 canonical 会话消息。"""
        if draft.source_message_id is None:
            return
        source = await session.scalar(
            select(ConversationMessageModel).where(
                ConversationMessageModel.id == draft.source_message_id,
                ConversationMessageModel.session_id == draft.session_id,
            )
        )
        if source is None or source.role not in {
            ConversationRole.USER.value,
            ConversationRole.ASSISTANT.value,
        }:
            raise InvalidMessageReferenceError("source_conversation_message_not_found")

    @staticmethod
    async def _validate_tool_result_reference(
        session: AsyncSession,
        draft: ExecutionMessageDraft,
    ) -> None:
        """校验 tool 结果恰好关联同 run/thread 中已存在的 assistant 调用。"""
        result_parts = [part for part in draft.parts if isinstance(part, ToolResultPart)]
        if (
            len(result_parts) != 1
            or draft.tool_call_id is None
            or result_parts[0].tool_call_id != draft.tool_call_id
        ):
            raise InvalidMessageReferenceError("tool_result_reference_invalid")

        rows = (
            await session.scalars(
                select(ExecutionMessageModel).where(
                    ExecutionMessageModel.session_id == draft.session_id,
                    ExecutionMessageModel.run_id == draft.run_id,
                    ExecutionMessageModel.thread_id == draft.thread_id,
                    ExecutionMessageModel.role == ExecutionRole.ASSISTANT.value,
                )
            )
        ).all()
        for row in rows:
            for part in deserialize_parts(row.parts):
                if isinstance(part, ToolCallPart) and part.tool_call_id == draft.tool_call_id:
                    return
        raise InvalidMessageReferenceError("tool_call_not_found")

    @staticmethod
    async def _validate_tool_call_uniqueness(
        session: AsyncSession,
        draft: ExecutionMessageDraft,
    ) -> None:
        """阻止同 run/thread 跨 assistant 消息重复声明工具调用 ID。"""
        call_ids = [part.tool_call_id for part in draft.parts if isinstance(part, ToolCallPart)]
        current_ids = set(call_ids)
        if len(current_ids) != len(call_ids):
            raise InvalidMessageReferenceError("duplicate_tool_call_id")
        if not current_ids:
            return
        rows = (
            await session.scalars(
                select(ExecutionMessageModel).where(
                    ExecutionMessageModel.session_id == draft.session_id,
                    ExecutionMessageModel.run_id == draft.run_id,
                    ExecutionMessageModel.thread_id == draft.thread_id,
                    ExecutionMessageModel.role == ExecutionRole.ASSISTANT.value,
                )
            )
        ).all()
        for row in rows:
            for part in deserialize_parts(row.parts):
                if isinstance(part, ToolCallPart) and part.tool_call_id in current_ids:
                    raise InvalidMessageReferenceError("duplicate_tool_call_id")

    @staticmethod
    def _accept_existing_conversation(
        existing: ConversationMessage,
        draft: ConversationMessageDraft,
    ) -> ConversationMessage:
        """接受完全相同的重试，拒绝同键不同内容。"""
        if not _conversation_matches(existing, draft):
            raise MessageConflictError("message_conflict")
        return existing

    @staticmethod
    def _accept_existing_execution(
        existing: ExecutionMessage,
        draft: ExecutionMessageDraft,
    ) -> ExecutionMessage:
        """接受完全相同的工具结果重试，拒绝冲突结果。"""
        if not _execution_matches(existing, draft):
            raise MessageConflictError("message_conflict")
        return existing


__all__ = ["SqlAlchemyMessageRepository"]
