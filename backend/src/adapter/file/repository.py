"""会话文件元数据与配额校验的 SQLAlchemy 适配器。"""

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
    SessionFileExtractionStatus,
    SessionFilePage,
    SessionFileRepository,
)
from domain.session import SessionNotFoundError


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
    """在同一事务内把待发布文件映射为可提交行（写前校验提取字段组合一致性）。"""
    _validate_extraction_fields(
        extraction_status=pending.extraction_status,
        extracted_text_key=pending.extracted_text_key,
        extracted_text_bytes=pending.extracted_text_bytes,
        extraction_error_code=pending.extraction_error_code,
    )
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
        extraction_status=pending.extraction_status.value,
        extracted_text_key=pending.extracted_text_key,
        extracted_text_bytes=pending.extracted_text_bytes,
        extraction_error_code=pending.extraction_error_code,
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
        # 与 update_extraction 对称：在进入数据库事务和配额锁前先快速校验提取字段组合一致性。
        _validate_extraction_fields(
            extraction_status=file.extraction_status,
            extracted_text_key=file.extracted_text_key,
            extracted_text_bytes=file.extracted_text_bytes,
            extraction_error_code=file.extraction_error_code,
        )
        async with self._session_factory() as session:
            async with session.begin():
                # 与 finish_tool_call 统一锁定 SessionModel 行（FOR UPDATE）串行化配额与幂等检查；
                # 事务内无对象存储 I/O 且极短，锁序完全一致，无需额外 pg_advisory_xact_lock。
                visible = await session.scalar(
                    select(SessionModel.id)
                    .where(
                        SessionModel.id == session_id,
                        SessionModel.owner_id == owner_id,
                        SessionModel.deleted_time.is_(None),
                    )
                    .with_for_update()
                )
                if visible is None:
                    raise SessionNotFoundError("session_not_found")
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

    async def update_extraction(
        self,
        file_id: str,
        *,
        extraction_status: SessionFileExtractionStatus,
        extracted_text_key: str | None,
        extracted_text_bytes: int | None,
        extraction_error_code: str | None,
    ) -> SessionFile | None:
        """在行锁内原子回填惰性提取结果；并发已写入 ready 时绝不覆盖，直接返回最新已提交记录。"""
        _validate_extraction_fields(
            extraction_status=extraction_status,
            extracted_text_key=extracted_text_key,
            extracted_text_bytes=extracted_text_bytes,
            extraction_error_code=extraction_error_code,
        )
        async with self._session_factory() as session:
            async with session.begin():
                # 锁定目标文件行（PostgreSQL 下 FOR UPDATE），防止并发惰性补解析相互覆盖缓存键。
                row = await session.scalar(
                    select(SessionFileModel)
                    .where(SessionFileModel.id == file_id)
                    .with_for_update()
                )
                if row is None:
                    return None
                if row.extraction_status == SessionFileExtractionStatus.READY.value:
                    # 并发胜者已提交 ready 缓存，保持既有引用并返回，由调用方清理自身临时对象。
                    return session_file_to_domain(row)
                if (
                    row.extraction_status == SessionFileExtractionStatus.FAILED.value
                    and extraction_status is SessionFileExtractionStatus.FAILED
                ):
                    return session_file_to_domain(row)
                row.extraction_status = extraction_status.value
                row.extracted_text_key = extracted_text_key
                row.extracted_text_bytes = extracted_text_bytes
                row.extraction_error_code = extraction_error_code
                await session.flush()
                return session_file_to_domain(row)

    async def is_object_key_referenced(self, object_key: str) -> bool:
        """在离线停写维护中复核会话原件、提取缓存、Skill 版本与执行日志四处对象引用。"""
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


def _validate_extraction_fields(
    *,
    extraction_status: SessionFileExtractionStatus,
    extracted_text_key: str | None,
    extracted_text_bytes: int | None,
    extraction_error_code: str | None,
) -> None:
    """在写入前校验提取状态与关联字段组合一致性，与数据库 CheckConstraint 保持同构。"""
    if extraction_status is SessionFileExtractionStatus.NONE:
        if (
            extracted_text_key is not None
            or extracted_text_bytes is not None
            or extraction_error_code is not None
        ):
            raise ValueError("session_file_extraction_fields_invalid")
        return
    if extraction_status is SessionFileExtractionStatus.READY:
        if (
            not extracted_text_key
            or extracted_text_bytes is None
            or extracted_text_bytes < 0
            or extraction_error_code is not None
        ):
            raise ValueError("session_file_extraction_fields_invalid")
        return
    if (
        extracted_text_key is not None
        or extracted_text_bytes is not None
        or not extraction_error_code
    ):
        raise ValueError("session_file_extraction_fields_invalid")


async def _is_object_key_referenced_in_session(
    session: AsyncSession,
    object_key: str,
) -> bool:
    """只要任一业务表仍引用原件或提取文本缓存对象，离线维护都必须保留它。"""
    if await session.scalar(
        select(SessionFileModel.id)
        .where(SessionFileModel.object_key == object_key)
        .limit(1)
    ):
        return True
    # 独立命中 uq_session_files_extracted_text_key，避免离线维护误删活跃文档提取缓存。
    if await session.scalar(
        select(SessionFileModel.id)
        .where(SessionFileModel.extracted_text_key == object_key)
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
    "pending_file_to_model",
]
