"""会话文件元数据与配额锁的 SQLAlchemy 适配器。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.file.converter import session_file_to_domain
from adapter.file.model import SessionFileModel
from adapter.session.model import SessionModel
from adapter.skill.model import SkillExecutionModel, SkillVersionModel
from domain.file import (
    MAX_SESSION_FILES,
    FileObjectReferenceRepository,
    PendingSessionFile,
    SessionFile,
    SessionFilePage,
    SessionFileRepository,
)
from domain.session import SessionNotFoundError


async def lock_session_file_quota(session: AsyncSession, session_id: str) -> None:
    """用会话派生的事务 advisory lock 串行化上传和产物发布。"""
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"athena:session-files:{session_id}", 0)
                )
            )
        )


async def count_published_session_files(session: AsyncSession, session_id: str) -> int:
    """统计已经发布的文件；未发布对象不占配额。"""
    return int(
        (
            await session.scalar(
                select(func.count(SessionFileModel.id)).where(
                    SessionFileModel.session_id == session_id
                )
            )
        )
        or 0
    )


def pending_file_to_model(
    pending: PendingSessionFile,
    *,
    session_id: str,
    owner_id: str,
    source: str,
    source_call_id: str | None,
) -> SessionFileModel:
    """在同一事务内把待发布文件映射为可提交行。"""
    return SessionFileModel(
        id=pending.id,
        session_id=session_id,
        owner_id=owner_id,
        source=source,
        filename=pending.filename,
        label=pending.label,
        mime_type=pending.mime_type,
        size_bytes=pending.size_bytes,
        sha256=pending.sha256,
        object_key=pending.object_key,
        source_call_id=source_call_id,
        output_index=pending.output_index,
        created_at=pending.created_at,
    )


class SqlAlchemySessionFileRepository(
    SessionFileRepository,
    FileObjectReferenceRepository,
):
    """在文件配额锁内发布上传并读取 owner/session 范围元数据。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """保存共享数据库会话工厂。"""
        self._session_factory = session_factory

    async def publish_upload(
        self,
        session_id: str,
        owner_id: str,
        file: PendingSessionFile,
        *,
        idempotency_key: str,
        request_fingerprint: str,
        published_at: datetime,
    ) -> SessionFile:
        """对象已落存储后，在配额锁内发布唯一上传元数据。"""
        del published_at
        async with self._session_factory() as session:
            async with session.begin():
                # 上传和 Run 完成都按 Session → 配额锁的顺序加锁。这里先显式
                # 取 FOR KEY SHARE，避免在持有 advisory lock 时由新增文件的
                # 外键检查反向等待 Session 行，与完成事务形成死锁。
                visible = await session.scalar(
                    select(SessionModel.id).where(
                        SessionModel.id == session_id,
                        SessionModel.owner_id == owner_id,
                        SessionModel.deleted_time.is_(None),
                    ).with_for_update(key_share=True)
                )
                if visible is None:
                    raise SessionNotFoundError("session_not_found")
                await lock_session_file_quota(session, session_id)
                existing = await self._upload_request(
                    session,
                    session_id,
                    owner_id,
                    idempotency_key,
                )
                if existing is not None:
                    if existing.request_fingerprint != request_fingerprint:
                        raise ValueError("idempotency_conflict")
                    return session_file_to_domain(existing)
                count = await count_published_session_files(session, session_id)
                if count >= MAX_SESSION_FILES:
                    raise ValueError("session_file_quota_exceeded")
                row = pending_file_to_model(
                    file,
                    session_id=session_id,
                    owner_id=owner_id,
                    source="upload",
                    source_call_id=None,
                )
                # 上传文件没有脚本产物顺序；数据库约束据此区分 upload 与 artifact。
                row.output_index = None
                row.idempotency_key = idempotency_key
                row.request_fingerprint = request_fingerprint
                session.add(row)
                await session.flush()
                result = session_file_to_domain(row)
        return result

    async def find_upload_request(
        self,
        session_id: str,
        owner_id: str,
        idempotency_key: str,
    ) -> SessionFile | None:
        """只返回同一仍可见会话中的已提交上传重放。"""
        async with self._session_factory() as session:
            visible = await session.scalar(
                select(SessionModel.id).where(
                    SessionModel.id == session_id,
                    SessionModel.owner_id == owner_id,
                    SessionModel.deleted_time.is_(None),
                )
            )
            if visible is None:
                raise SessionNotFoundError("session_not_found")
            row = await self._upload_request(
                session,
                session_id,
                owner_id,
                idempotency_key,
            )
        return None if row is None else session_file_to_domain(row)

    async def list_visible(
        self,
        session_id: str,
        owner_id: str,
        *,
        after_file_id: str | None,
        limit: int,
    ) -> SessionFilePage | None:
        """读取同一未删除会话中的稳定文件页。"""
        if not 1 <= limit <= 100:
            raise ValueError("session_file_page_invalid")
        async with self._session_factory() as session:
            visible = await session.scalar(
                select(SessionModel.id).where(
                    SessionModel.id == session_id,
                    SessionModel.owner_id == owner_id,
                    SessionModel.deleted_time.is_(None),
                )
            )
            if visible is None:
                return None
            query = select(SessionFileModel).where(
                SessionFileModel.session_id == session_id,
                SessionFileModel.owner_id == owner_id,
            )
            if after_file_id is not None:
                cursor = await session.scalar(
                    select(SessionFileModel).where(
                        SessionFileModel.id == after_file_id,
                        SessionFileModel.session_id == session_id,
                        SessionFileModel.owner_id == owner_id,
                    )
                )
                if cursor is None:
                    raise ValueError("session_file_cursor_invalid")
                query = query.where(
                    (SessionFileModel.created_at > cursor.created_at)
                    | (
                        (SessionFileModel.created_at == cursor.created_at)
                        & (SessionFileModel.id > cursor.id)
                    )
                )
            rows = list(
                (
                    await session.scalars(
                        query.order_by(SessionFileModel.created_at, SessionFileModel.id).limit(
                            limit + 1
                        )
                    )
                ).all()
            )
        items = tuple(session_file_to_domain(row) for row in rows[:limit])
        return SessionFilePage(
            items=items,
            next_after_file_id=items[-1].id if len(rows) > limit and items else None,
        )

    async def get_visible(
        self,
        session_id: str,
        owner_id: str,
        file_id: str,
    ) -> SessionFile | None:
        """读取同一未删除会话内的文件。"""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SessionFileModel)
                .join(SessionModel, SessionModel.id == SessionFileModel.session_id)
                .where(
                    SessionFileModel.id == file_id,
                    SessionFileModel.session_id == session_id,
                    SessionFileModel.owner_id == owner_id,
                    SessionModel.owner_id == owner_id,
                    SessionModel.deleted_time.is_(None),
                )
            )
        return None if row is None else session_file_to_domain(row)

    async def get_visible_many(
        self,
        session_id: str,
        owner_id: str,
        file_ids: tuple[str, ...],
    ) -> tuple[SessionFile, ...]:
        """一次读取文件并保持请求顺序，调用方据此拒绝任意越权 ID。"""
        if not file_ids:
            return ()
        if len(file_ids) != len(set(file_ids)):
            raise ValueError("session_file_ids_duplicate")
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(SessionFileModel)
                    .join(SessionModel, SessionModel.id == SessionFileModel.session_id)
                    .where(
                        SessionFileModel.id.in_(file_ids),
                        SessionFileModel.session_id == session_id,
                        SessionFileModel.owner_id == owner_id,
                        SessionModel.owner_id == owner_id,
                        SessionModel.deleted_time.is_(None),
                    )
                )
            ).all()
        by_id = {row.id: session_file_to_domain(row) for row in rows}
        if len(by_id) != len(file_ids):
            raise ValueError("session_file_not_found")
        return tuple(by_id[file_id] for file_id in file_ids)

    async def is_object_key_referenced(self, object_key: str) -> bool:
        """在离线停写维护中复核三张业务表的对象引用。"""
        async with self._session_factory() as session:
            return await _is_object_key_referenced_in_session(session, object_key)

    async def _upload_request(
        self,
        session: AsyncSession,
        session_id: str,
        owner_id: str,
        idempotency_key: str,
    ) -> SessionFileModel | None:
        """读取上传来源的幂等账本行，不把产物计入请求键范围。"""
        return await session.scalar(
            select(SessionFileModel).where(
                SessionFileModel.session_id == session_id,
                SessionFileModel.owner_id == owner_id,
                SessionFileModel.source == "upload",
                SessionFileModel.idempotency_key == idempotency_key,
            )
        )


async def _is_object_key_referenced_in_session(
    session: AsyncSession,
    object_key: str,
) -> bool:
    """只要任一业务表仍引用对象，离线维护都必须保留它。"""
    if await session.scalar(
        select(SessionFileModel.id)
        .where(SessionFileModel.object_key == object_key)
        .limit(1)
    ):
        return True
    if await session.scalar(
        select(SkillVersionModel.id)
        .where(SkillVersionModel.object_key == object_key)
        .limit(1)
    ):
        return True
    return bool(
        await session.scalar(
            select(SkillExecutionModel.id)
            .where(SkillExecutionModel.log_object_key == object_key)
            .limit(1)
        )
    )


__all__ = [
    "SqlAlchemySessionFileRepository",
    "count_published_session_files",
    "lock_session_file_quota",
    "pending_file_to_model",
]
