"""知识库目录与导入记录的 SQLAlchemy 仓储。"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import ColumnElement, delete, exists, func, or_, select, text, union, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased
from sqlalchemy.sql import Select

from adapter.access.repository import (
    build_readable_resource_clause,
    delete_resource_grants_in_session,
    load_grants_map_in_session,
    load_user_access_snapshot_in_session,
    sync_resource_grants_in_session,
)
from adapter.auth.model import UserModel
from adapter.knowledge.model import (
    KnowledgeBaseModel,
    KnowledgeChunkModel,
    KnowledgeDocumentModel,
    KnowledgeJobModel,
    KnowledgeRequestModel,
    KnowledgeSourceModel,
    KnowledgeUploadReservationModel,
    KnowledgeUploadSlotModel,
    KnowledgeVersionModel,
    ProjectKnowledgeBaseModel,
)
from adapter.project.model import ProjectModel
from common.ids import new_id
from domain.access import (
    ResourceType,
    UserAccessSnapshot,
    VisibilityGrant,
    VisibilityGrantInput,
    VisibilityScope,
)
from domain.knowledge.model import (
    MAX_PROJECT_KNOWLEDGE_BASES,
    ChunkLocator,
    Document,
    DocumentState,
    DocumentVersion,
    KnowledgeBase,
    ProjectKnowledgeLimitReachedError,
    SearchHit,
    SourceReference,
)
from domain.knowledge.ports import ClaimedKnowledgeJob, FileUploadReservation


def _personal_privacy_guard(actor_id: str) -> ColumnElement[bool]:
    """个人私有知识库始终强制 owner_id 纵深防御；公司/部门库由上层权限引擎鉴权。"""
    return or_(
        KnowledgeBaseModel.visibility_scope != VisibilityScope.PERSONAL.value,
        KnowledgeBaseModel.owner_id == actor_id,
    )


class KnowledgeRepository:
    """执行知识库事务与 owner 范围查询。"""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def _check_owner_quota(
        self,
        session: AsyncSession,
        owner_id: str,
        *,
        additional_bytes: int,
        additional_documents: int,
    ) -> None:
        """锁定用户行并在提交前复核文档数和原件字节配额。"""
        await self._lock_owner(session, owner_id)
        documents = await session.scalar(
            select(func.count())
            .select_from(KnowledgeDocumentModel)
            .where(
                KnowledgeDocumentModel.owner_id == owner_id,
                KnowledgeDocumentModel.deleted_time.is_(None),
            )
        )
        byte_count = await session.scalar(
            select(func.coalesce(func.sum(KnowledgeSourceModel.size_bytes), 0)).where(
                KnowledgeSourceModel.owner_id == owner_id,
            )
        )
        reserved_bytes = await session.scalar(
            select(func.coalesce(func.sum(KnowledgeUploadReservationModel.size_bytes), 0)).where(
                KnowledgeUploadReservationModel.owner_id == owner_id
            )
        )
        reserved_documents = await session.scalar(
            select(func.count())
            .select_from(KnowledgeUploadReservationModel)
            .where(
                KnowledgeUploadReservationModel.owner_id == owner_id,
                KnowledgeUploadReservationModel.operation == "create",
            )
        )
        if int(documents or 0) + int(reserved_documents or 0) + additional_documents > 1000:
            raise ValueError("knowledge_document_quota_exceeded")
        if int(byte_count or 0) + int(reserved_bytes or 0) + additional_bytes > 1024 * 1024 * 1024:
            raise ValueError("knowledge_storage_quota_exceeded")

    async def _lock_owner(self, session: AsyncSession, owner_id: str) -> None:
        owner = await session.scalar(
            select(UserModel.id).where(UserModel.id == owner_id).with_for_update()
        )
        if owner is None:
            raise LookupError("owner_not_found")

    async def is_storage_key_referenced(self, storage_key: str) -> bool:
        async with self._sessions() as session:
            source_reference = exists().where(KnowledgeSourceModel.storage_key == storage_key)
            reservation_reference = exists().where(
                KnowledgeUploadReservationModel.storage_key == storage_key
            )
            return bool(await session.scalar(select(source_reference | reservation_reference)))

    async def find_referenced_storage_keys(self, storage_keys: Sequence[str]) -> set[str]:
        """单条 SQL 批量核验一组对象 key 的引用状态，避免孤儿扫描 N+1 查库。"""
        unique_keys = tuple(dict.fromkeys(storage_keys))
        if not unique_keys:
            return set()
        # 使用 UNION 在同一语句快照内同时查来源表与上传预留表，保持与单条检查一致的引用语义。
        query = union(
            select(KnowledgeSourceModel.storage_key).where(
                KnowledgeSourceModel.storage_key.in_(unique_keys)
            ),
            select(KnowledgeUploadReservationModel.storage_key).where(
                KnowledgeUploadReservationModel.storage_key.in_(unique_keys)
            ),
        )
        async with self._sessions() as session:
            rows = await session.scalars(query)
            return set(rows)

    async def reserve_file_upload(
        self,
        *,
        owner_id: str,
        operation: str,
        request_key: str,
        request_hash: str,
        base_id: str,
        document_id: str,
        size_bytes: int,
        reservation_token: str,
        storage_key: str,
        source_id: str,
        version_id: str,
        job_id: str,
        at: datetime,
        expires_after_seconds: int = 86400,
    ) -> FileUploadReservation:
        """Atomically reuse idempotency or reserve owner storage before writing a file."""
        if operation not in {"create", "replace"} or size_bytes < 1:
            raise ValueError("knowledge_upload_reservation_invalid")
        if expires_after_seconds < 60:
            raise ValueError("knowledge_upload_reservation_ttl_invalid")
        async with self._sessions() as session, session.begin():
            await self._lock_owner(session, owner_id)
            existing = await session.scalar(
                select(KnowledgeRequestModel).where(
                    KnowledgeRequestModel.owner_id == owner_id,
                    KnowledgeRequestModel.operation == operation,
                    KnowledgeRequestModel.request_key == request_key,
                )
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise ValueError("idempotency_conflict")
                # 协作场景下文档创建者可能与替换操作者不同，幂等重放只按 document_id 查找。
                existing_document = await session.scalar(
                    select(KnowledgeDocumentModel).where(
                        KnowledgeDocumentModel.id == existing.document_id,
                    )
                )
                if existing_document is None:
                    raise LookupError("document_not_found")
                return FileUploadReservation(
                    None,
                    None,
                    None,
                    existing.document_id,
                    existing.version_id,
                    None,
                    _document(existing_document),
                )
            reservation = await session.scalar(
                select(KnowledgeUploadReservationModel)
                .where(
                    KnowledgeUploadReservationModel.owner_id == owner_id,
                    KnowledgeUploadReservationModel.operation == operation,
                    KnowledgeUploadReservationModel.request_key == request_key,
                )
                .with_for_update()
            )
            if reservation is not None:
                db_now = cast(datetime, await session.scalar(select(func.clock_timestamp())))
                if reservation.request_hash != request_hash:
                    raise ValueError("idempotency_conflict")
                if reservation.state != "reserved":
                    raise ValueError("upload_cleanup_in_progress")
                if reservation.expires_at <= db_now:
                    raise ValueError("knowledge_upload_reservation_stale")
                reservation.expires_at = func.clock_timestamp() + text(
                    f"INTERVAL '{expires_after_seconds} seconds'"
                )
                return FileUploadReservation(
                    reservation.reservation_token,
                    reservation.storage_key,
                    reservation.source_id,
                    reservation.document_id,
                    reservation.version_id,
                    reservation.job_id,
                )
            base = await session.scalar(
                select(KnowledgeBaseModel)
                .where(
                    KnowledgeBaseModel.id == base_id,
                    _personal_privacy_guard(owner_id),
                    KnowledgeBaseModel.deleted_time.is_(None),
                )
                .with_for_update()
            )
            if base is None:
                raise LookupError("knowledge_base_not_found")
            if operation == "replace":
                # 部门/公司库允许不同管理员替换既有文档，不按 KnowledgeDocumentModel.owner_id 过滤。
                document = await session.scalar(
                    select(KnowledgeDocumentModel)
                    .where(
                        KnowledgeDocumentModel.id == document_id,
                        KnowledgeDocumentModel.knowledge_base_id == base_id,
                        KnowledgeDocumentModel.deleted_time.is_(None),
                    )
                    .with_for_update()
                )
                if document is None:
                    raise LookupError("document_not_found")
                if document.pending_version_id is not None:
                    raise ValueError("document_update_in_progress")
            await self._check_owner_quota(
                session,
                owner_id,
                additional_bytes=size_bytes,
                additional_documents=1 if operation == "create" else 0,
            )
            session.add(
                KnowledgeUploadReservationModel(
                    owner_id=owner_id,
                    operation=operation,
                    request_key=request_key,
                    reservation_token=reservation_token,
                    request_hash=request_hash,
                    base_id=base_id,
                    document_id=document_id,
                    version_id=version_id,
                    source_id=source_id,
                    job_id=job_id,
                    storage_key=storage_key,
                    size_bytes=size_bytes,
                    state="reserved",
                    fencing_token=0,
                    created_at=at,
                    expires_at=func.clock_timestamp()
                    + text(f"INTERVAL '{expires_after_seconds} seconds'"),
                )
            )
            await session.flush()
            return FileUploadReservation(
                reservation_token,
                storage_key,
                source_id,
                document_id,
                version_id,
                job_id,
            )

    async def acquire_upload_slot(self, token: str, *, lease_seconds: int) -> bool:
        if lease_seconds < 601:
            raise ValueError("upload_slot_lease_invalid")
        async with self._sessions() as session, session.begin():
            slot = await session.scalar(
                select(KnowledgeUploadSlotModel)
                .where(
                    KnowledgeUploadSlotModel.token.is_(None)
                    | (KnowledgeUploadSlotModel.lease_until < func.clock_timestamp())
                )
                .order_by(KnowledgeUploadSlotModel.slot_id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if slot is None:
                return False
            slot.token = token
            slot.lease_until = func.clock_timestamp() + text(f"INTERVAL '{lease_seconds} seconds'")
            return True

    async def release_upload_slot(self, token: str) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(KnowledgeUploadSlotModel)
                .where(KnowledgeUploadSlotModel.token == token)
                .values(token=None, lease_until=None)
            )

    async def list_expired_upload_reservations(
        self, *, limit: int = 20
    ) -> tuple[FileUploadReservation, ...]:
        """读取仍处于 reserved 状态且已过期的上传预留（排除已被来源记录引用的对象）。"""
        if limit < 1:
            raise ValueError("upload_reservation_cleanup_parameters_invalid")
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(KnowledgeUploadReservationModel)
                    .where(
                        KnowledgeUploadReservationModel.state == "reserved",
                        KnowledgeUploadReservationModel.expires_at <= func.clock_timestamp(),
                        ~select(KnowledgeSourceModel.id)
                        .where(
                            KnowledgeSourceModel.storage_key
                            == KnowledgeUploadReservationModel.storage_key
                        )
                        .exists(),
                    )
                    .order_by(KnowledgeUploadReservationModel.expires_at)
                    .limit(limit)
                )
            ).all()
        return tuple(
            FileUploadReservation(
                item.reservation_token,
                item.storage_key,
                item.source_id,
                item.document_id,
                item.version_id,
                item.job_id,
            )
            for item in rows
        )

    async def delete_expired_upload_reservation(self, reservation_token: str) -> bool:
        """在单事务内原子核验过期、无来源引用与无其他有效预留引用后删除预留行。"""
        async with self._sessions() as session, session.begin():
            owner_id = await session.scalar(
                select(KnowledgeUploadReservationModel.owner_id).where(
                    KnowledgeUploadReservationModel.reservation_token == reservation_token
                )
            )
            if owner_id is None:
                return False
            await self._lock_owner(session, owner_id)
            other_reservation = aliased(KnowledgeUploadReservationModel)
            item = await session.scalar(
                select(KnowledgeUploadReservationModel)
                .where(
                    KnowledgeUploadReservationModel.owner_id == owner_id,
                    KnowledgeUploadReservationModel.reservation_token == reservation_token,
                    KnowledgeUploadReservationModel.state == "reserved",
                    KnowledgeUploadReservationModel.expires_at <= func.clock_timestamp(),
                    ~select(KnowledgeSourceModel.id)
                    .where(
                        KnowledgeSourceModel.storage_key
                        == KnowledgeUploadReservationModel.storage_key
                    )
                    .exists(),
                    ~select(other_reservation.reservation_token)
                    .where(
                        other_reservation.storage_key
                        == KnowledgeUploadReservationModel.storage_key,
                        other_reservation.reservation_token != reservation_token,
                        other_reservation.expires_at > func.clock_timestamp(),
                    )
                    .exists(),
                )
                .with_for_update()
            )
            if item is None:
                return False
            await session.delete(item)
            return True

    async def claim_job(
        self, *, lease_seconds: int, owner_concurrency: int = 2
    ) -> ClaimedKnowledgeJob | None:
        """锁定用户配额后用 SKIP LOCKED 认领任务并递增 fencing token。"""
        if lease_seconds < 1 or owner_concurrency < 1:
            raise ValueError("lease_seconds_invalid")
        async with self._sessions() as session, session.begin():
            candidates = await session.scalars(
                select(KnowledgeJobModel.owner_id)
                .where(
                    KnowledgeJobModel.kind.in_(("ingestion", "cleanup")),
                    KnowledgeJobModel.state.in_(("queued", "retry", "processing")),
                    KnowledgeJobModel.next_attempt_at <= func.clock_timestamp(),
                    (
                        KnowledgeJobModel.lease_until.is_(None)
                        | (KnowledgeJobModel.lease_until < func.clock_timestamp())
                    ),
                )
                .order_by(KnowledgeJobModel.next_attempt_at, KnowledgeJobModel.created_at)
                .limit(100)
            )
            job = None
            for owner_id in dict.fromkeys(candidates):
                owner = await session.scalar(
                    select(UserModel.id).where(UserModel.id == owner_id).with_for_update()
                )
                if owner is None:
                    continue
                active_count = await session.scalar(
                    select(func.count())
                    .select_from(KnowledgeJobModel)
                    .where(
                        KnowledgeJobModel.owner_id == owner_id,
                        KnowledgeJobModel.state == "processing",
                        KnowledgeJobModel.lease_until > func.clock_timestamp(),
                    )
                )
                if int(active_count or 0) >= owner_concurrency:
                    continue
                job = await session.scalar(
                    select(KnowledgeJobModel)
                    .where(
                        KnowledgeJobModel.owner_id == owner_id,
                        KnowledgeJobModel.kind.in_(("ingestion", "cleanup")),
                        KnowledgeJobModel.state.in_(("queued", "retry", "processing")),
                        KnowledgeJobModel.next_attempt_at <= func.clock_timestamp(),
                        KnowledgeJobModel.lease_until.is_(None)
                        | (KnowledgeJobModel.lease_until < func.clock_timestamp()),
                    )
                    .order_by(KnowledgeJobModel.next_attempt_at, KnowledgeJobModel.created_at)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if job is not None:
                    break
            if job is None:
                return None
            if job.kind == "cleanup":
                cleanup_document_id = (
                    job.target_id.removeprefix("doc:") if job.target_id.startswith("doc:") else None
                )
                source_query = (
                    select(KnowledgeSourceModel.storage_key)
                    .join(
                        KnowledgeVersionModel,
                        KnowledgeVersionModel.source_id == KnowledgeSourceModel.id,
                    )
                    .join(
                        KnowledgeDocumentModel,
                        KnowledgeDocumentModel.id == KnowledgeVersionModel.document_id,
                    )
                    .where(
                        (
                            KnowledgeDocumentModel.id == cleanup_document_id
                            if cleanup_document_id is not None
                            else KnowledgeDocumentModel.knowledge_base_id == job.target_id
                        ),
                        ~exists(
                            select(KnowledgeVersionModel.id)
                            .join(
                                KnowledgeDocumentModel,
                                KnowledgeDocumentModel.id == KnowledgeVersionModel.document_id,
                            )
                            .where(
                                KnowledgeVersionModel.source_id == KnowledgeSourceModel.id,
                                (
                                    KnowledgeDocumentModel.id != cleanup_document_id
                                    if cleanup_document_id is not None
                                    else (KnowledgeDocumentModel.knowledge_base_id != job.target_id)
                                ),
                            )
                        ),
                    )
                )
                keys = await session.scalars(source_query)
                job.state = "processing"
                job.attempt += 1
                job.fencing_token += 1
                job.lease_until = func.clock_timestamp() + text(
                    f"INTERVAL '{lease_seconds} seconds'"
                )
                job.updated_at = func.clock_timestamp()
                return ClaimedKnowledgeJob(
                    job.id,
                    job.owner_id,
                    "",
                    "",
                    "",
                    None,
                    None,
                    None,
                    job.fencing_token,
                    "cleanup",
                    tuple(dict.fromkeys(keys)),
                    job.target_id,
                    None,
                )
            version = await session.scalar(
                select(KnowledgeVersionModel).where(KnowledgeVersionModel.id == job.target_id)
            )
            if version is None:
                job.state = "failed"
                job.error_code = "version_missing"
                return None
            # 协作管理员替换/重试他人上传的文档时，job.owner_id 可能不同于文档/源创建者。
            document = await session.scalar(
                select(KnowledgeDocumentModel).where(
                    KnowledgeDocumentModel.id == version.document_id,
                )
            )
            if (
                document is None
                or document.deleted_time is not None
                or document.pending_version_id != version.id
            ):
                job.state = "failed"
                job.error_code = "version_not_pending"
                return None
            source = (
                await session.scalar(
                    select(KnowledgeSourceModel).where(
                        KnowledgeSourceModel.id == version.source_id,
                    )
                )
                if version.source_id
                else None
            )
            job.state = "processing"
            job.attempt += 1
            job.fencing_token += 1
            job.lease_until = func.clock_timestamp() + text(f"INTERVAL '{lease_seconds} seconds'")
            job.updated_at = func.clock_timestamp()
            version.state = "processing"
            return ClaimedKnowledgeJob(
                job.id,
                job.owner_id,
                version.id,
                document.id,
                document.title,
                version.source_text,
                source.storage_key if source else None,
                source.filename if source else None,
                job.fencing_token,
                target_id=version.id,
                profile_id=version.profile_id,
            )

    async def heartbeat_job(self, job_id: str, fencing_token: int, *, lease_seconds: int) -> bool:
        """仅当前 fencing token 可续租。"""
        if lease_seconds < 1:
            raise ValueError("lease_seconds_invalid")
        async with self._sessions() as session, session.begin():
            job = await session.scalar(
                select(KnowledgeJobModel).where(KnowledgeJobModel.id == job_id).with_for_update()
            )
            now = cast(datetime, await session.scalar(select(func.clock_timestamp())))
            if (
                job is None
                or job.state != "processing"
                or job.fencing_token != fencing_token
                or job.lease_until is None
                or job.lease_until <= now
            ):
                return False
            job.lease_until = func.clock_timestamp() + text(f"INTERVAL '{lease_seconds} seconds'")
            job.updated_at = func.clock_timestamp()
            return True

    async def publish_job(
        self,
        job: ClaimedKnowledgeJob,
        *,
        chunks: list[tuple[str, dict[str, object], int, str, list[float]]],
        warnings: list[str],
    ) -> bool:
        """短事务重建片段并发布活动版本，拒绝过期 worker 写入。"""
        async with self._sessions() as session, session.begin():
            # 后台发布任务不限制 owner_id 与文档/知识库创建者一致，支持跨管理员替换版本。
            base_id = await session.scalar(
                select(KnowledgeDocumentModel.knowledge_base_id).where(
                    KnowledgeDocumentModel.id == job.document_id,
                )
            )
            if base_id is None:
                return False
            base = await session.scalar(
                select(KnowledgeBaseModel)
                .where(
                    KnowledgeBaseModel.id == base_id,
                )
                .with_for_update()
            )
            document = await session.scalar(
                select(KnowledgeDocumentModel)
                .where(
                    KnowledgeDocumentModel.id == job.document_id,
                )
                .with_for_update()
            )
            task = await session.scalar(
                select(KnowledgeJobModel)
                .where(
                    KnowledgeJobModel.id == job.job_id,
                )
                .with_for_update()
            )
            version = await session.scalar(
                select(KnowledgeVersionModel)
                .where(
                    KnowledgeVersionModel.id == job.version_id,
                    KnowledgeVersionModel.document_id == job.document_id,
                )
                .with_for_update()
            )
            database_now = cast(datetime, await session.scalar(select(func.clock_timestamp())))
            if (
                document is None
                or base is None
                or base.deleted_time is not None
                or task is None
                or version is None
                or version.profile_id != job.profile_id
                or document.deleted_time is not None
                or task.state != "processing"
                or task.fencing_token != job.fencing_token
                or task.lease_until is None
                or task.lease_until <= database_now
                or document.pending_version_id != job.version_id
            ):
                return False
            await session.execute(
                delete(KnowledgeChunkModel).where(
                    KnowledgeChunkModel.version_id == job.version_id,
                )
            )
            for ordinal, (chunk_text, locator, token_count, search_text, vector) in enumerate(
                chunks
            ):
                session.add(
                    KnowledgeChunkModel(
                        id=new_id("kchunk"),
                        document_id=job.document_id,
                        version_id=job.version_id,
                        ordinal=ordinal,
                        content=chunk_text,
                        locator=locator,
                        token_count=token_count,
                        search_document=func.to_tsvector("simple", search_text),
                        embedding=vector,
                    )
                )
            version.state = "ready"
            version.warnings = warnings
            version.chunk_count = len(chunks)
            version.completed_at = func.clock_timestamp()
            version.error_code = None
            document.active_version_id = job.version_id
            document.pending_version_id = None
            document.updated_at = func.clock_timestamp()
            task.state = "done"
            task.lease_until = None
            task.error_code = None
            task.updated_at = func.clock_timestamp()
            return True

    async def fail_job(self, job: ClaimedKnowledgeJob, *, error_code: str, retryable: bool) -> None:
        """按 fencing token 记录失败并安排有限退避重试。"""
        async with self._sessions() as session, session.begin():
            base_id = (
                job.target_id
                if job.kind == "cleanup" and not job.target_id.startswith("doc:")
                else await session.scalar(
                    select(KnowledgeDocumentModel.knowledge_base_id).where(
                        KnowledgeDocumentModel.id
                        == (
                            job.target_id.removeprefix("doc:")
                            if job.kind == "cleanup"
                            else job.document_id
                        ),
                    )
                )
            )
            if base_id is not None:
                await session.scalar(
                    select(KnowledgeBaseModel)
                    .where(
                        KnowledgeBaseModel.id == base_id,
                    )
                    .with_for_update()
                )
            document = await session.scalar(
                select(KnowledgeDocumentModel)
                .where(
                    KnowledgeDocumentModel.id == job.document_id,
                )
                .with_for_update()
            )
            task = await session.scalar(
                select(KnowledgeJobModel)
                .where(
                    KnowledgeJobModel.id == job.job_id,
                )
                .with_for_update()
            )
            version = await session.scalar(
                select(KnowledgeVersionModel)
                .where(KnowledgeVersionModel.id == job.version_id)
                .with_for_update()
            )
            database_now = cast(datetime, await session.scalar(select(func.clock_timestamp())))
            if (
                task is None
                or task.state != "processing"
                or task.fencing_token != job.fencing_token
                or task.lease_until is None
                or task.lease_until <= database_now
            ):
                return
            retry = retryable and (task.kind == "cleanup" or task.attempt < 3)
            task.state = "retry" if retry else "failed"
            task.error_code = error_code[:64]
            task.lease_until = None
            jittered_delay = _retry_delay_seconds(task.attempt) if retry else 0.0
            task.next_attempt_at = func.clock_timestamp() + text(
                f"INTERVAL '{jittered_delay:.3f} seconds'"
            )
            task.updated_at = func.clock_timestamp()
            if version is not None and not retry:
                version.state = "failed"
                version.error_code = error_code[:64]
                version.completed_at = func.clock_timestamp()
                if document is not None and document.pending_version_id == job.version_id:
                    document.pending_version_id = None

    async def finish_cleanup_job(self, job: ClaimedKnowledgeJob) -> bool:
        """fencing 校验后清除已软删除文档或知识库的数据库记录。"""
        async with self._sessions() as session, session.begin():
            if job.target_id.startswith("doc:"):
                document_id = job.target_id.removeprefix("doc:")
                base_id = await session.scalar(
                    select(KnowledgeDocumentModel.knowledge_base_id).where(
                        KnowledgeDocumentModel.id == document_id,
                    )
                )
                if base_id is None:
                    return False
                await session.scalar(
                    select(KnowledgeBaseModel)
                    .where(
                        KnowledgeBaseModel.id == base_id,
                    )
                    .with_for_update()
                )
                document = await session.scalar(
                    select(KnowledgeDocumentModel)
                    .where(
                        KnowledgeDocumentModel.id == document_id,
                    )
                    .with_for_update()
                )
                task = await session.scalar(
                    select(KnowledgeJobModel)
                    .where(KnowledgeJobModel.id == job.job_id)
                    .with_for_update()
                )
                now = cast(datetime, await session.scalar(select(func.clock_timestamp())))
                if (
                    document is None
                    or document.deleted_time is None
                    or task is None
                    or task.state != "processing"
                    or task.fencing_token != job.fencing_token
                    or task.lease_until is None
                    or task.lease_until <= now
                ):
                    return False
                version_rows = (
                    await session.execute(
                        select(KnowledgeVersionModel.id, KnowledgeVersionModel.source_id).where(
                            KnowledgeVersionModel.document_id == document_id,
                        )
                    )
                ).all()
                version_ids = [row[0] for row in version_rows]
                candidate_source_ids = [row[1] for row in version_rows if row[1] is not None]
                if version_ids:
                    await session.execute(
                        delete(KnowledgeJobModel).where(
                            KnowledgeJobModel.id != task.id,
                            KnowledgeJobModel.target_id.in_(version_ids),
                        )
                    )
                await session.execute(
                    delete(KnowledgeVersionModel).where(
                        KnowledgeVersionModel.document_id == document_id,
                    )
                )
                await session.execute(
                    delete(KnowledgeRequestModel).where(
                        KnowledgeRequestModel.document_id == document_id,
                    )
                )
                # 仅回收当前文档曾经引用且已无任何其他版本引用的原件记录（跨管理员删除同样生效）。
                if candidate_source_ids:
                    unused_sources = list(
                        await session.scalars(
                            select(KnowledgeSourceModel.id).where(
                                KnowledgeSourceModel.id.in_(candidate_source_ids),
                                ~exists(
                                    select(KnowledgeVersionModel.id).where(
                                        KnowledgeVersionModel.source_id == KnowledgeSourceModel.id,
                                    )
                                ),
                            )
                        )
                    )
                    if unused_sources:
                        await session.execute(
                            delete(KnowledgeSourceModel).where(
                                KnowledgeSourceModel.id.in_(unused_sources),
                            )
                        )
                task.state = "done"
                task.lease_until = None
                task.updated_at = func.clock_timestamp()
                return True
            base = await session.scalar(
                select(KnowledgeBaseModel)
                .where(
                    KnowledgeBaseModel.id == job.target_id,
                )
                .with_for_update()
            )
            task = await session.scalar(
                select(KnowledgeJobModel)
                .where(
                    KnowledgeJobModel.id == job.job_id,
                )
                .with_for_update()
            )
            now = cast(datetime, await session.scalar(select(func.clock_timestamp())))
            if (
                base is None
                or base.deleted_time is None
                or task is None
                or task.state != "processing"
                or task.fencing_token != job.fencing_token
                or task.lease_until is None
                or task.lease_until <= now
            ):
                return False
            document_ids = list(
                await session.scalars(
                    select(KnowledgeDocumentModel.id).where(
                        KnowledgeDocumentModel.knowledge_base_id == base.id,
                    )
                )
            )
            version_rows = (
                (
                    await session.execute(
                        select(KnowledgeVersionModel.id, KnowledgeVersionModel.source_id).where(
                            KnowledgeVersionModel.document_id.in_(document_ids),
                        )
                    )
                ).all()
                if document_ids
                else []
            )
            version_ids = [row[0] for row in version_rows]
            candidate_source_ids = [row[1] for row in version_rows if row[1] is not None]
            if version_ids:
                await session.execute(
                    delete(KnowledgeJobModel).where(
                        KnowledgeJobModel.id != task.id,
                        KnowledgeJobModel.target_id.in_(version_ids),
                    )
                )
            if document_ids:
                await session.execute(
                    delete(KnowledgeJobModel).where(
                        KnowledgeJobModel.id != task.id,
                        KnowledgeJobModel.kind == "cleanup",
                        KnowledgeJobModel.target_id.in_([f"doc:{item}" for item in document_ids]),
                    )
                )
                await session.execute(
                    delete(KnowledgeVersionModel).where(
                        KnowledgeVersionModel.document_id.in_(document_ids),
                    )
                )
                await session.execute(
                    delete(KnowledgeRequestModel).where(
                        KnowledgeRequestModel.document_id.in_(document_ids),
                    )
                )
            await session.execute(
                delete(ProjectKnowledgeBaseModel).where(
                    ProjectKnowledgeBaseModel.knowledge_base_id == base.id,
                )
            )
            await delete_resource_grants_in_session(session, ResourceType.KNOWLEDGE_BASE, base.id)
            if document_ids:
                await session.execute(
                    delete(KnowledgeDocumentModel).where(
                        KnowledgeDocumentModel.id.in_(document_ids),
                    )
                )
            if candidate_source_ids:
                source_ids = list(
                    await session.scalars(
                        select(KnowledgeSourceModel.id).where(
                            KnowledgeSourceModel.id.in_(candidate_source_ids),
                            ~exists(
                                select(KnowledgeVersionModel.id).where(
                                    KnowledgeVersionModel.source_id == KnowledgeSourceModel.id,
                                )
                            ),
                        )
                    )
                )
                if source_ids:
                    await session.execute(
                        delete(KnowledgeSourceModel).where(
                            KnowledgeSourceModel.id.in_(source_ids),
                        )
                    )
            await session.delete(base)
            task.state = "done"
            task.lease_until = None
            task.updated_at = func.clock_timestamp()
            return True

    async def list_project_bases(self, owner_id: str, project_id: str) -> list[KnowledgeBase]:
        """读取用户项目关联且当前未删除的知识库（含可见性授权信息）。"""
        async with self._sessions() as session:
            rows = list(
                await session.scalars(
                    select(KnowledgeBaseModel)
                    .join(
                        ProjectKnowledgeBaseModel,
                        ProjectKnowledgeBaseModel.knowledge_base_id == KnowledgeBaseModel.id,
                    )
                    .where(
                        ProjectKnowledgeBaseModel.owner_id == owner_id,
                        ProjectKnowledgeBaseModel.project_id == project_id,
                        KnowledgeBaseModel.deleted_time.is_(None),
                    )
                    .order_by(KnowledgeBaseModel.name, KnowledgeBaseModel.id)
                )
            )
            grants_map = await load_grants_map_in_session(
                session, ResourceType.KNOWLEDGE_BASE, [row.id for row in rows]
            )
            return [_base(row, grants=grants_map.get(row.id, ())) for row in rows]

    async def bind_project_base(
        self, owner_id: str, project_id: str, base_id: str, at: datetime
    ) -> bool:
        """按项目→知识库锁顺序幂等关联用户项目与有权限的知识库。"""
        async with self._sessions() as session, session.begin():
            project = await session.scalar(
                select(ProjectModel)
                .where(
                    ProjectModel.id == project_id,
                    ProjectModel.owner_id == owner_id,
                    ProjectModel.deleted_time.is_(None),
                )
                .with_for_update()
            )
            if project is None:
                return False
            # 复合外键已拆除：支持绑定 company/department 知识库及本人 personal 知识库。
            base = await session.scalar(
                select(KnowledgeBaseModel)
                .where(
                    KnowledgeBaseModel.id == base_id,
                    _personal_privacy_guard(owner_id),
                    KnowledgeBaseModel.deleted_time.is_(None),
                )
                .with_for_update()
            )
            if base is None:
                raise LookupError("knowledge_base_not_found")
            existing = await session.get(ProjectKnowledgeBaseModel, (project_id, base_id))
            if existing is None:
                active_count = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(ProjectKnowledgeBaseModel)
                        .join(
                            KnowledgeBaseModel,
                            KnowledgeBaseModel.id == ProjectKnowledgeBaseModel.knowledge_base_id,
                        )
                        .where(
                            ProjectKnowledgeBaseModel.project_id == project_id,
                            ProjectKnowledgeBaseModel.owner_id == owner_id,
                            KnowledgeBaseModel.deleted_time.is_(None),
                        )
                    )
                    or 0
                )
                if active_count >= MAX_PROJECT_KNOWLEDGE_BASES:
                    raise ProjectKnowledgeLimitReachedError("project_knowledge_limit_reached")
                session.add(
                    ProjectKnowledgeBaseModel(
                        project_id=project_id,
                        knowledge_base_id=base_id,
                        owner_id=owner_id,
                        created_at=at,
                    )
                )
            return True

    async def unbind_project_base(self, owner_id: str, project_id: str, base_id: str) -> bool:
        """按项目→知识库锁顺序幂等解除关联（即使调岗后仍允许解绑旧部门库）。"""
        async with self._sessions() as session, session.begin():
            project = await session.scalar(
                select(ProjectModel)
                .where(
                    ProjectModel.id == project_id,
                    ProjectModel.owner_id == owner_id,
                )
                .with_for_update()
            )
            if project is None:
                return False
            await session.scalar(
                select(KnowledgeBaseModel)
                .where(
                    KnowledgeBaseModel.id == base_id,
                )
                .with_for_update()
            )
            link = await session.get(ProjectKnowledgeBaseModel, (project_id, base_id))
            if link is not None and link.owner_id == owner_id:
                await session.delete(link)
            return True

    async def create_base(
        self,
        item: KnowledgeBase,
        *,
        visibility_grants: Sequence[VisibilityGrantInput] = (),
    ) -> KnowledgeBase:
        """在单事务内原子创建知识库并写入关联的可见性授权。"""
        model = KnowledgeBaseModel(
            id=item.id,
            owner_id=item.owner_id,
            name=item.name,
            description=item.description,
            visibility_scope=item.visibility_scope.value,
            owner_department_id=item.owner_department_id,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )
        async with self._sessions() as session, session.begin():
            session.add(model)
            await session.flush()
            grants = await sync_resource_grants_in_session(
                session,
                ResourceType.KNOWLEDGE_BASE,
                item.id,
                visibility_grants,
                now=item.created_at,
            )
            return _base(model, grants=grants)

    async def get_base(self, owner_id: str, base_id: str) -> KnowledgeBase | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(KnowledgeBaseModel).where(
                    KnowledgeBaseModel.id == base_id,
                    _personal_privacy_guard(owner_id),
                    KnowledgeBaseModel.deleted_time.is_(None),
                )
            )
            if model is None:
                return None
            grants_map = await load_grants_map_in_session(
                session, ResourceType.KNOWLEDGE_BASE, (model.id,)
            )
            return _base(model, grants=grants_map.get(model.id, ()))

    async def get_base_by_id(self, base_id: str) -> KnowledgeBase | None:
        """按主键读取未软删除的知识库（供权限引擎统一鉴权使用）。"""
        async with self._sessions() as session:
            model = await session.scalar(
                select(KnowledgeBaseModel).where(
                    KnowledgeBaseModel.id == base_id,
                    KnowledgeBaseModel.deleted_time.is_(None),
                )
            )
            if model is None:
                return None
            grants_map = await load_grants_map_in_session(
                session, ResourceType.KNOWLEDGE_BASE, (model.id,)
            )
            return _base(model, grants=grants_map.get(model.id, ()))

    async def list_bases(self, owner_id: str, *, offset: int, limit: int) -> list[KnowledgeBase]:
        async with self._sessions() as session:
            rows = list(
                await session.scalars(
                    select(KnowledgeBaseModel)
                    .where(
                        KnowledgeBaseModel.owner_id == owner_id,
                        KnowledgeBaseModel.deleted_time.is_(None),
                    )
                    .order_by(KnowledgeBaseModel.updated_at.desc(), KnowledgeBaseModel.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            grants_map = await load_grants_map_in_session(
                session, ResourceType.KNOWLEDGE_BASE, [row.id for row in rows]
            )
            return [_base(item, grants=grants_map.get(item.id, ())) for item in rows]

    async def list_readable_bases(
        self,
        user_id: str,
        *,
        offset: int,
        limit: int,
        visibility_scope: VisibilityScope | None = None,
        owner_department_id: str | None = None,
        snapshot: UserAccessSnapshot | None = None,
    ) -> list[KnowledgeBase]:
        """单 Session 加载/复用用户权限快照并以 SQL EXISTS 下推过滤、排序与分页知识库。"""
        normalized_user_id = user_id.strip()
        if snapshot is None and not normalized_user_id:
            return []
        if snapshot is not None and not snapshot.is_active:
            return []
        async with self._sessions() as session:
            resolved = (
                snapshot
                if snapshot is not None
                else await load_user_access_snapshot_in_session(session, normalized_user_id)
            )
            if resolved is None or not resolved.is_active:
                return []
            conditions: list[ColumnElement[bool]] = [
                KnowledgeBaseModel.deleted_time.is_(None),
                build_readable_resource_clause(
                    resolved,
                    ResourceType.KNOWLEDGE_BASE,
                    id_col=KnowledgeBaseModel.id,
                    scope_col=KnowledgeBaseModel.visibility_scope,
                    owner_id_col=KnowledgeBaseModel.owner_id,
                    owner_department_id_col=KnowledgeBaseModel.owner_department_id,
                ),
            ]
            if visibility_scope is not None:
                conditions.append(KnowledgeBaseModel.visibility_scope == visibility_scope.value)
            if owner_department_id is not None:
                conditions.append(KnowledgeBaseModel.owner_department_id == owner_department_id)
            rows = list(
                await session.scalars(
                    select(KnowledgeBaseModel)
                    .where(*conditions)
                    .order_by(KnowledgeBaseModel.updated_at.desc(), KnowledgeBaseModel.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            if not rows:
                return []
            # 仅对当前分页命中的知识库加载可见性授权，避免全表加载 grants。
            grants_map = await load_grants_map_in_session(
                session, ResourceType.KNOWLEDGE_BASE, [row.id for row in rows]
            )
            return [_base(item, grants=grants_map.get(item.id, ())) for item in rows]

    async def list_bases_by_ids(
        self,
        base_ids: Sequence[str],
        *,
        offset: int,
        limit: int,
        visibility_scope: VisibilityScope | None = None,
        owner_department_id: str | None = None,
    ) -> list[KnowledgeBase]:
        """按已通过权限引擎过滤的可读 ID 集合与可选筛选条件稳定分页。"""
        unique_ids = tuple(dict.fromkeys(base_ids))
        if not unique_ids:
            return []
        conditions: list[ColumnElement[bool]] = [
            KnowledgeBaseModel.id.in_(unique_ids),
            KnowledgeBaseModel.deleted_time.is_(None),
        ]
        if visibility_scope is not None:
            conditions.append(KnowledgeBaseModel.visibility_scope == visibility_scope.value)
        if owner_department_id is not None:
            conditions.append(KnowledgeBaseModel.owner_department_id == owner_department_id)
        async with self._sessions() as session:
            rows = list(
                await session.scalars(
                    select(KnowledgeBaseModel)
                    .where(*conditions)
                    .order_by(KnowledgeBaseModel.updated_at.desc(), KnowledgeBaseModel.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            grants_map = await load_grants_map_in_session(
                session, ResourceType.KNOWLEDGE_BASE, [row.id for row in rows]
            )
            return [_base(item, grants=grants_map.get(item.id, ())) for item in rows]

    async def rename_base(
        self, owner_id: str, base_id: str, name: str, description: str | None, at: datetime
    ) -> KnowledgeBase | None:
        async with self._sessions() as session, session.begin():
            model = await session.scalar(
                select(KnowledgeBaseModel)
                .where(
                    KnowledgeBaseModel.id == base_id,
                    _personal_privacy_guard(owner_id),
                    KnowledgeBaseModel.deleted_time.is_(None),
                )
                .with_for_update()
            )
            if model is None:
                return None
            model.name, model.description, model.updated_at = name, description, at
            await session.flush()
            grants_map = await load_grants_map_in_session(
                session, ResourceType.KNOWLEDGE_BASE, (model.id,)
            )
            return _base(model, grants=grants_map.get(model.id, ()))

    async def update_base(
        self,
        base_id: str,
        *,
        name: str,
        description: str | None,
        visibility_scope: VisibilityScope,
        owner_department_id: str | None,
        visibility_grants: Sequence[VisibilityGrantInput] | None,
        at: datetime,
    ) -> KnowledgeBase | None:
        """在单事务内原子更新知识库元数据、三层可见性及授权清单。"""
        async with self._sessions() as session, session.begin():
            model = await session.scalar(
                select(KnowledgeBaseModel)
                .where(
                    KnowledgeBaseModel.id == base_id,
                    KnowledgeBaseModel.deleted_time.is_(None),
                )
                .with_for_update()
            )
            if model is None:
                return None
            model.name = name
            model.description = description
            model.visibility_scope = visibility_scope.value
            model.owner_department_id = owner_department_id
            model.updated_at = at
            await session.flush()
            if visibility_grants is not None:
                grants = await sync_resource_grants_in_session(
                    session,
                    ResourceType.KNOWLEDGE_BASE,
                    model.id,
                    visibility_grants,
                    now=at,
                )
            else:
                grants_map = await load_grants_map_in_session(
                    session, ResourceType.KNOWLEDGE_BASE, (model.id,)
                )
                grants = grants_map.get(model.id, ())
            return _base(model, grants=grants)

    async def soft_delete_base(self, owner_id: str, base_id: str, at: datetime) -> bool:
        """在单事务内软删除知识库、清理全部项目绑定及可见性授权并入队清理任务。"""
        async with self._sessions() as session, session.begin():
            item = await session.scalar(
                select(KnowledgeBaseModel)
                .where(
                    KnowledgeBaseModel.id == base_id,
                    _personal_privacy_guard(owner_id),
                )
                .with_for_update()
            )
            if item is None:
                return False
            item.deleted_time = item.deleted_time or at
            item.updated_at = at
            # 删除所有用户项目对该已删知识库的绑定，不再按 owner_id 限定。
            await session.execute(
                delete(ProjectKnowledgeBaseModel).where(
                    ProjectKnowledgeBaseModel.knowledge_base_id == base_id,
                )
            )
            # 同步清理该知识库的所有可见性授权，杜绝残留孤儿记录。
            await delete_resource_grants_in_session(session, ResourceType.KNOWLEDGE_BASE, base_id)
            cleanup = await session.scalar(
                select(KnowledgeJobModel)
                .where(
                    KnowledgeJobModel.kind == "cleanup",
                    KnowledgeJobModel.target_id == base_id,
                    KnowledgeJobModel.state.in_(("queued", "retry", "processing")),
                )
                .with_for_update()
            )
            if cleanup is None:
                session.add(
                    KnowledgeJobModel(
                        id=new_id("kjob"),
                        owner_id=owner_id,
                        kind="cleanup",
                        target_id=base_id,
                        state="queued",
                        attempt=0,
                        fencing_token=0,
                        next_attempt_at=at,
                        created_at=at,
                        updated_at=at,
                    )
                )
            return True

    async def get_document(self, owner_id: str, base_id: str, document_id: str) -> Document | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(KnowledgeDocumentModel)
                .join(
                    KnowledgeBaseModel,
                    KnowledgeBaseModel.id == KnowledgeDocumentModel.knowledge_base_id,
                )
                .where(
                    KnowledgeDocumentModel.id == document_id,
                    KnowledgeDocumentModel.knowledge_base_id == base_id,
                    KnowledgeDocumentModel.deleted_time.is_(None),
                    KnowledgeBaseModel.deleted_time.is_(None),
                    _personal_privacy_guard(owner_id),
                )
            )
            if model is None:
                return None
            # 在同一读取会话中装配最新版本摘要，不按版本创建者过滤以支持跨管理员协作。
            latest_by_doc = await _load_latest_versions(session, (model.id,))
            return _document(model, latest_version=latest_by_doc.get(model.id))

    async def create_text_document(
        self,
        *,
        owner_id: str,
        base_id: str,
        title: str,
        text: str,
        request_key: str,
        request_hash: str,
        content_hash: str,
        document_id: str,
        version_id: str,
        job_id: str,
        profile_id: str,
        at: datetime,
    ) -> tuple[Document, str]:
        """在一个事务中保存手动文本、版本、处理任务和幂等记录。"""
        async with self._sessions() as session, session.begin():
            await self._lock_owner(session, owner_id)
            existing = await session.scalar(
                select(KnowledgeRequestModel).where(
                    KnowledgeRequestModel.owner_id == owner_id,
                    KnowledgeRequestModel.operation == "create",
                    KnowledgeRequestModel.request_key == request_key,
                )
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise ValueError("idempotency_conflict")
                found = await session.scalar(
                    select(KnowledgeDocumentModel).where(
                        KnowledgeDocumentModel.id == existing.document_id,
                    )
                )
                if found is None:
                    raise LookupError("document_not_found")
                return _document(found), existing.version_id
            await self._check_owner_quota(
                session, owner_id, additional_bytes=0, additional_documents=1
            )
            base = await session.scalar(
                select(KnowledgeBaseModel)
                .where(
                    KnowledgeBaseModel.id == base_id,
                    _personal_privacy_guard(owner_id),
                    KnowledgeBaseModel.deleted_time.is_(None),
                )
                .with_for_update()
            )
            if base is None:
                raise LookupError("knowledge_base_not_found")
            document = KnowledgeDocumentModel(
                id=document_id,
                owner_id=owner_id,
                knowledge_base_id=base_id,
                title=title,
                active_version_id=None,
                pending_version_id=version_id,
                created_at=at,
                updated_at=at,
            )
            version = KnowledgeVersionModel(
                id=version_id,
                document_id=document_id,
                owner_id=owner_id,
                source_id=None,
                source_text=text,
                content_hash=content_hash,
                profile_id=profile_id,
                processing_config={},
                state="queued",
                warnings=[],
                chunk_count=0,
                created_at=at,
            )
            job = KnowledgeJobModel(
                id=job_id,
                owner_id=owner_id,
                kind="ingestion",
                target_id=version_id,
                state="queued",
                attempt=0,
                fencing_token=0,
                next_attempt_at=at,
                created_at=at,
                updated_at=at,
            )
            request = KnowledgeRequestModel(
                owner_id=owner_id,
                operation="create",
                request_key=request_key,
                request_hash=request_hash,
                document_id=document_id,
                version_id=version_id,
                created_at=at,
            )
            session.add_all([document, version, job, request])
            await session.flush()
            return _document(document), version_id

    async def create_file_document(
        self,
        *,
        owner_id: str,
        base_id: str,
        title: str,
        filename: str,
        media_type: str,
        storage_key: str,
        size_bytes: int,
        sha256: str,
        request_key: str,
        request_hash: str,
        document_id: str,
        version_id: str,
        source_id: str,
        reservation_token: str,
        job_id: str,
        profile_id: str,
        at: datetime,
    ) -> tuple[Document, str]:
        """在单一事务中登记上传原件、待处理版本和幂等结果。"""
        async with self._sessions() as session, session.begin():
            await self._lock_owner(session, owner_id)
            existing = await session.scalar(
                select(KnowledgeRequestModel).where(
                    KnowledgeRequestModel.owner_id == owner_id,
                    KnowledgeRequestModel.operation == "create",
                    KnowledgeRequestModel.request_key == request_key,
                )
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise ValueError("idempotency_conflict")
                document = await session.scalar(
                    select(KnowledgeDocumentModel).where(
                        KnowledgeDocumentModel.id == existing.document_id,
                    )
                )
                if document is None:
                    raise LookupError("document_not_found")
                return _document(document), existing.version_id
            reservation = await session.scalar(
                select(KnowledgeUploadReservationModel)
                .where(
                    KnowledgeUploadReservationModel.owner_id == owner_id,
                    KnowledgeUploadReservationModel.operation == "create",
                    KnowledgeUploadReservationModel.request_key == request_key,
                    KnowledgeUploadReservationModel.reservation_token == reservation_token,
                )
                .with_for_update()
            )
            db_now = cast(datetime, await session.scalar(select(func.clock_timestamp())))
            if reservation is None or (
                reservation.request_hash != request_hash
                or reservation.base_id != base_id
                or reservation.document_id != document_id
                or reservation.version_id != version_id
                or reservation.source_id != source_id
                or reservation.job_id != job_id
                or reservation.storage_key != storage_key
                or reservation.size_bytes != size_bytes
                or reservation.state != "reserved"
                or reservation.expires_at <= db_now
            ):
                raise ValueError("knowledge_upload_reservation_stale")
            await session.delete(reservation)
            await session.flush()
            await self._check_owner_quota(
                session,
                owner_id,
                additional_bytes=size_bytes,
                additional_documents=1,
            )
            base = await session.scalar(
                select(KnowledgeBaseModel)
                .where(
                    KnowledgeBaseModel.id == base_id,
                    _personal_privacy_guard(owner_id),
                    KnowledgeBaseModel.deleted_time.is_(None),
                )
                .with_for_update()
            )
            if base is None:
                raise LookupError("knowledge_base_not_found")
            source = await session.scalar(
                select(KnowledgeSourceModel).where(
                    KnowledgeSourceModel.owner_id == owner_id,
                    KnowledgeSourceModel.storage_key == storage_key,
                )
            )
            if source is None:
                source = KnowledgeSourceModel(
                    id=source_id,
                    owner_id=owner_id,
                    storage_key=storage_key,
                    filename=filename,
                    media_type=media_type,
                    size_bytes=size_bytes,
                    sha256=sha256,
                    created_at=at,
                )
            document = KnowledgeDocumentModel(
                id=document_id,
                owner_id=owner_id,
                knowledge_base_id=base_id,
                title=title,
                active_version_id=None,
                pending_version_id=version_id,
                created_at=at,
                updated_at=at,
            )
            version = KnowledgeVersionModel(
                id=version_id,
                document_id=document_id,
                owner_id=owner_id,
                source_id=source.id,
                source_text=None,
                content_hash=sha256,
                profile_id=profile_id,
                processing_config={},
                state="queued",
                warnings=[],
                chunk_count=0,
                created_at=at,
            )
            job = KnowledgeJobModel(
                id=job_id,
                owner_id=owner_id,
                kind="ingestion",
                target_id=version_id,
                state="queued",
                attempt=0,
                fencing_token=0,
                next_attempt_at=at,
                created_at=at,
                updated_at=at,
            )
            request = KnowledgeRequestModel(
                owner_id=owner_id,
                operation="create",
                request_key=request_key,
                request_hash=request_hash,
                document_id=document_id,
                version_id=version_id,
                created_at=at,
            )
            session.add_all([document, version, job, request])
            if source.id == source_id:
                session.add(source)
            await session.flush()
            return _document(document), version_id

    async def replace_text_document(
        self,
        *,
        owner_id: str,
        base_id: str,
        document_id: str,
        text: str,
        request_key: str,
        request_hash: str,
        content_hash: str,
        version_id: str,
        job_id: str,
        profile_id: str,
        at: datetime,
    ) -> tuple[Document, str]:
        """锁定文档并创建不会覆盖当前活动版本的待处理替换版本。"""
        async with self._sessions() as session, session.begin():
            await self._lock_owner(session, owner_id)
            existing = await session.scalar(
                select(KnowledgeRequestModel).where(
                    KnowledgeRequestModel.owner_id == owner_id,
                    KnowledgeRequestModel.operation == "replace",
                    KnowledgeRequestModel.request_key == request_key,
                )
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise ValueError("idempotency_conflict")
                prior = await session.scalar(
                    select(KnowledgeDocumentModel).where(
                        KnowledgeDocumentModel.id == existing.document_id,
                    )
                )
                if prior is None:
                    raise LookupError("document_not_found")
                return _document(prior), existing.version_id
            await self._check_owner_quota(
                session, owner_id, additional_bytes=0, additional_documents=0
            )
            base = await session.scalar(
                select(KnowledgeBaseModel)
                .where(
                    KnowledgeBaseModel.id == base_id,
                    _personal_privacy_guard(owner_id),
                    KnowledgeBaseModel.deleted_time.is_(None),
                )
                .with_for_update()
            )
            if base is None:
                raise LookupError("knowledge_base_not_found")
            document = await session.scalar(
                select(KnowledgeDocumentModel)
                .where(
                    KnowledgeDocumentModel.id == document_id,
                    KnowledgeDocumentModel.knowledge_base_id == base_id,
                    KnowledgeDocumentModel.deleted_time.is_(None),
                )
                .with_for_update()
            )
            if document is None:
                raise LookupError("document_not_found")
            if document.pending_version_id is not None:
                raise ValueError("document_update_in_progress")
            version_created_at = await _next_version_time(session, document_id, at)
            version = KnowledgeVersionModel(
                id=version_id,
                document_id=document_id,
                owner_id=owner_id,
                source_id=None,
                source_text=text,
                content_hash=content_hash,
                profile_id=profile_id,
                processing_config={},
                state="queued",
                warnings=[],
                chunk_count=0,
                created_at=version_created_at,
            )
            job = KnowledgeJobModel(
                id=job_id,
                owner_id=owner_id,
                kind="ingestion",
                target_id=version_id,
                state="queued",
                attempt=0,
                fencing_token=0,
                next_attempt_at=at,
                created_at=at,
                updated_at=at,
            )
            request = KnowledgeRequestModel(
                owner_id=owner_id,
                operation="replace",
                request_key=request_key,
                request_hash=request_hash,
                document_id=document_id,
                version_id=version_id,
                created_at=at,
            )
            session.add_all([version, job, request])
            document.pending_version_id = version_id
            document.updated_at = at
            await session.flush()
            return _document(document), version_id

    async def replace_file_document(
        self,
        *,
        owner_id: str,
        base_id: str,
        document_id: str,
        title: str,
        filename: str,
        media_type: str,
        storage_key: str,
        size_bytes: int,
        sha256: str,
        request_key: str,
        request_hash: str,
        version_id: str,
        source_id: str,
        reservation_token: str,
        job_id: str,
        profile_id: str,
        at: datetime,
    ) -> tuple[Document, str]:
        """以幂等事务保存新原件和版本，保留当前活动版本直到处理成功。"""
        async with self._sessions() as session, session.begin():
            await self._lock_owner(session, owner_id)
            existing = await session.scalar(
                select(KnowledgeRequestModel).where(
                    KnowledgeRequestModel.owner_id == owner_id,
                    KnowledgeRequestModel.operation == "replace",
                    KnowledgeRequestModel.request_key == request_key,
                )
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise ValueError("idempotency_conflict")
                document = await session.scalar(
                    select(KnowledgeDocumentModel).where(
                        KnowledgeDocumentModel.id == existing.document_id,
                    )
                )
                if document is None:
                    raise LookupError("document_not_found")
                return _document(document), existing.version_id
            reservation = await session.scalar(
                select(KnowledgeUploadReservationModel)
                .where(
                    KnowledgeUploadReservationModel.owner_id == owner_id,
                    KnowledgeUploadReservationModel.operation == "replace",
                    KnowledgeUploadReservationModel.request_key == request_key,
                    KnowledgeUploadReservationModel.reservation_token == reservation_token,
                )
                .with_for_update()
            )
            db_now = cast(datetime, await session.scalar(select(func.clock_timestamp())))
            if reservation is None or (
                reservation.request_hash != request_hash
                or reservation.base_id != base_id
                or reservation.document_id != document_id
                or reservation.version_id != version_id
                or reservation.source_id != source_id
                or reservation.job_id != job_id
                or reservation.storage_key != storage_key
                or reservation.size_bytes != size_bytes
                or reservation.state != "reserved"
                or reservation.expires_at <= db_now
            ):
                raise ValueError("knowledge_upload_reservation_stale")
            await session.delete(reservation)
            await session.flush()
            await self._check_owner_quota(
                session, owner_id, additional_bytes=size_bytes, additional_documents=0
            )
            base = await session.scalar(
                select(KnowledgeBaseModel)
                .where(
                    KnowledgeBaseModel.id == base_id,
                    _personal_privacy_guard(owner_id),
                    KnowledgeBaseModel.deleted_time.is_(None),
                )
                .with_for_update()
            )
            if base is None:
                raise LookupError("knowledge_base_not_found")
            document = await session.scalar(
                select(KnowledgeDocumentModel)
                .where(
                    KnowledgeDocumentModel.id == document_id,
                    KnowledgeDocumentModel.knowledge_base_id == base_id,
                    KnowledgeDocumentModel.deleted_time.is_(None),
                )
                .with_for_update()
            )
            if document is None:
                raise LookupError("document_not_found")
            if document.pending_version_id is not None:
                raise ValueError("document_update_in_progress")
            version_created_at = await _next_version_time(session, document_id, at)
            source = await session.scalar(
                select(KnowledgeSourceModel).where(
                    KnowledgeSourceModel.owner_id == owner_id,
                    KnowledgeSourceModel.storage_key == storage_key,
                )
            )
            if source is None:
                source = KnowledgeSourceModel(
                    id=source_id,
                    owner_id=owner_id,
                    storage_key=storage_key,
                    filename=filename,
                    media_type=media_type,
                    size_bytes=size_bytes,
                    sha256=sha256,
                    created_at=at,
                )
                session.add(source)
            version = KnowledgeVersionModel(
                id=version_id,
                document_id=document_id,
                owner_id=owner_id,
                source_id=source.id,
                source_text=None,
                content_hash=sha256,
                profile_id=profile_id,
                processing_config={},
                state="queued",
                warnings=[],
                chunk_count=0,
                created_at=version_created_at,
            )
            job = KnowledgeJobModel(
                id=job_id,
                owner_id=owner_id,
                kind="ingestion",
                target_id=version_id,
                state="queued",
                attempt=0,
                fencing_token=0,
                next_attempt_at=at,
                created_at=at,
                updated_at=at,
            )
            request = KnowledgeRequestModel(
                owner_id=owner_id,
                operation="replace",
                request_key=request_key,
                request_hash=request_hash,
                document_id=document_id,
                version_id=version_id,
                created_at=at,
            )
            session.add_all([version, job, request])
            document.pending_version_id = version_id
            document.updated_at = at
            await session.flush()
            return _document(document), version_id

    async def list_documents(
        self, owner_id: str, base_id: str, *, offset: int, limit: int
    ) -> list[Document]:
        async with self._sessions() as session:
            rows = list(
                await session.scalars(
                    select(KnowledgeDocumentModel)
                    .join(
                        KnowledgeBaseModel,
                        KnowledgeBaseModel.id == KnowledgeDocumentModel.knowledge_base_id,
                    )
                    .where(
                        KnowledgeDocumentModel.knowledge_base_id == base_id,
                        KnowledgeDocumentModel.deleted_time.is_(None),
                        KnowledgeBaseModel.deleted_time.is_(None),
                        _personal_privacy_guard(owner_id),
                    )
                    .order_by(KnowledgeDocumentModel.updated_at.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            # 文档页为空时直接返回，不额外发起版本表查询。
            if not rows:
                return []
            # 使用单次批量窗口函数查询为本页全部文档装配最新版本摘要，消除前端 N+1 轮询。
            latest_by_doc = await _load_latest_versions(session, [row.id for row in rows])
            return [_document(row, latest_version=latest_by_doc.get(row.id)) for row in rows]

    async def rename_document(
        self, owner_id: str, base_id: str, document_id: str, title: str, at: datetime
    ) -> Document | None:
        async with self._sessions() as session, session.begin():
            base = await session.scalar(
                select(KnowledgeBaseModel)
                .where(
                    KnowledgeBaseModel.id == base_id,
                    _personal_privacy_guard(owner_id),
                    KnowledgeBaseModel.deleted_time.is_(None),
                )
                .with_for_update()
            )
            if base is None:
                return None
            document = await session.scalar(
                select(KnowledgeDocumentModel)
                .where(
                    KnowledgeDocumentModel.id == document_id,
                    KnowledgeDocumentModel.knowledge_base_id == base_id,
                    KnowledgeDocumentModel.deleted_time.is_(None),
                )
                .with_for_update()
            )
            if document is None:
                return None
            document.title, document.updated_at = title, at
            # 在同一事务会话中装配最新版本摘要，保持重命名响应与 list/get 文档契约完全一致。
            latest_by_doc = await _load_latest_versions(session, (document.id,))
            return _document(document, latest_version=latest_by_doc.get(document.id))

    async def delete_document(
        self, owner_id: str, base_id: str, document_id: str
    ) -> list[str] | None:
        """软删除文档并清除派生版本，返回不再被引用的原件 key。"""
        async with self._sessions() as session, session.begin():
            base = await session.scalar(
                select(KnowledgeBaseModel)
                .where(
                    KnowledgeBaseModel.id == base_id,
                    _personal_privacy_guard(owner_id),
                )
                .with_for_update()
            )
            if base is None:
                return None
            document = await session.scalar(
                select(KnowledgeDocumentModel)
                .where(
                    KnowledgeDocumentModel.id == document_id,
                    KnowledgeDocumentModel.knowledge_base_id == base_id,
                )
                .with_for_update()
            )
            if document is None:
                return None
            if document.deleted_time is not None:
                return []
            document.deleted_time = func.clock_timestamp()
            document.updated_at = func.clock_timestamp()
            document.active_version_id = None
            document.pending_version_id = None
            session.add(
                KnowledgeJobModel(
                    id=new_id("kjob"),
                    owner_id=owner_id,
                    kind="cleanup",
                    target_id=f"doc:{document_id}",
                    state="queued",
                    attempt=0,
                    fencing_token=0,
                    next_attempt_at=func.clock_timestamp(),
                    created_at=func.clock_timestamp(),
                    updated_at=func.clock_timestamp(),
                )
            )
            return []

    async def list_versions(
        self, owner_id: str, base_id: str, document_id: str
    ) -> list[DocumentVersion] | None:
        if await self.get_document(owner_id, base_id, document_id) is None:
            return None
        async with self._sessions() as session:
            # 文档已通过归属知识库校验，版本列表按 document_id 完整展示（不限上传者）。
            versions = await session.scalars(
                select(KnowledgeVersionModel)
                .where(
                    KnowledgeVersionModel.document_id == document_id,
                )
                .order_by(KnowledgeVersionModel.created_at.desc(), KnowledgeVersionModel.id.desc())
            )
            return [_version(version) for version in versions]

    async def retry_version(
        self,
        owner_id: str,
        base_id: str,
        document_id: str,
        version_id: str,
        job_id: str,
        at: datetime,
    ) -> DocumentVersion | None:
        """仅允许最新失败版本重试，阻止较旧内容覆盖新版本。"""
        async with self._sessions() as session, session.begin():
            base = await session.scalar(
                select(KnowledgeBaseModel)
                .where(
                    KnowledgeBaseModel.id == base_id,
                    _personal_privacy_guard(owner_id),
                    KnowledgeBaseModel.deleted_time.is_(None),
                )
                .with_for_update()
            )
            if base is None:
                return None
            document = await session.scalar(
                select(KnowledgeDocumentModel)
                .where(
                    KnowledgeDocumentModel.id == document_id,
                    KnowledgeDocumentModel.knowledge_base_id == base_id,
                    KnowledgeDocumentModel.deleted_time.is_(None),
                )
                .with_for_update()
            )
            if document is None:
                return None
            if document.pending_version_id is not None:
                raise ValueError("document_update_in_progress")
            version = await session.scalar(
                select(KnowledgeVersionModel)
                .where(
                    KnowledgeVersionModel.id == version_id,
                    KnowledgeVersionModel.document_id == document_id,
                )
                .with_for_update()
            )
            if version is None:
                return None
            latest_id = await session.scalar(
                select(KnowledgeVersionModel.id)
                .where(
                    KnowledgeVersionModel.document_id == document_id,
                )
                .order_by(KnowledgeVersionModel.created_at.desc(), KnowledgeVersionModel.id.desc())
                .limit(1)
            )
            if version.state != "failed":
                raise ValueError("version_not_failed")
            if latest_id != version_id:
                raise ValueError("version_superseded")
            existing_job = await session.scalar(
                select(KnowledgeJobModel)
                .where(
                    KnowledgeJobModel.target_id == version_id,
                    KnowledgeJobModel.kind == "ingestion",
                )
                .with_for_update()
            )
            if existing_job is None:
                existing_job = KnowledgeJobModel(
                    id=job_id,
                    owner_id=owner_id,
                    kind="ingestion",
                    target_id=version_id,
                    state="queued",
                    attempt=0,
                    fencing_token=0,
                    next_attempt_at=at,
                    created_at=at,
                    updated_at=at,
                )
                session.add(existing_job)
            else:
                existing_job.state = "queued"
                existing_job.attempt = 0
                existing_job.lease_until = None
                existing_job.next_attempt_at = at
                existing_job.error_code = None
                existing_job.updated_at = at
            version.state = "queued"
            version.error_code = None
            version.completed_at = None
            document.pending_version_id = version_id
            document.updated_at = at
            return _version(version)

    async def get_source(
        self, owner_id: str, base_id: str, document_id: str, version_id: str
    ) -> SourceReference | None:
        async with self._sessions() as session:
            # 原件下载以所属知识库权限为准，支持普通成员读取公司/部门库文档原文。
            row = await session.execute(
                select(KnowledgeVersionModel, KnowledgeSourceModel)
                .outerjoin(
                    KnowledgeSourceModel,
                    KnowledgeSourceModel.id == KnowledgeVersionModel.source_id,
                )
                .join(
                    KnowledgeDocumentModel,
                    KnowledgeDocumentModel.id == KnowledgeVersionModel.document_id,
                )
                .join(
                    KnowledgeBaseModel,
                    KnowledgeBaseModel.id == KnowledgeDocumentModel.knowledge_base_id,
                )
                .where(
                    KnowledgeVersionModel.id == version_id,
                    KnowledgeVersionModel.document_id == document_id,
                    KnowledgeDocumentModel.knowledge_base_id == base_id,
                    KnowledgeDocumentModel.deleted_time.is_(None),
                    KnowledgeBaseModel.deleted_time.is_(None),
                    _personal_privacy_guard(owner_id),
                )
            )
            result = row.one_or_none()
            if result is None:
                return None
            version, source = result
            if source is None:
                return SourceReference(
                    f"{document_id}.txt",
                    "text/plain",
                    None,
                    version.source_text,
                )
            return SourceReference(source.filename, source.media_type, source.storage_key, None)

    async def get_chunk(self, owner_id: str, base_id: str, chunk_id: str) -> SearchHit | None:
        async with self._sessions() as session:
            row = await session.execute(
                select(KnowledgeChunkModel, KnowledgeDocumentModel.title)
                .join(
                    KnowledgeVersionModel,
                    KnowledgeVersionModel.id == KnowledgeChunkModel.version_id,
                )
                .join(
                    KnowledgeDocumentModel,
                    KnowledgeDocumentModel.id == KnowledgeVersionModel.document_id,
                )
                .join(
                    KnowledgeBaseModel,
                    KnowledgeBaseModel.id == KnowledgeDocumentModel.knowledge_base_id,
                )
                .where(
                    KnowledgeChunkModel.id == chunk_id,
                    KnowledgeDocumentModel.knowledge_base_id == base_id,
                    KnowledgeDocumentModel.deleted_time.is_(None),
                    KnowledgeBaseModel.deleted_time.is_(None),
                    _personal_privacy_guard(owner_id),
                )
            )
            item = row.one_or_none()
            if item is None:
                return None
            chunk, title = item
            return SearchHit(
                chunk.id,
                chunk.document_id,
                chunk.version_id,
                title,
                chunk.content,
                (_locator(chunk.locator),),
                (),
                0.0,
                knowledge_base_id=base_id,
            )

    async def validate_search_scope(self, owner_id: str, base_ids: list[str]) -> None:
        async with self._sessions() as session:
            visible = await session.scalars(
                select(KnowledgeBaseModel.id).where(
                    KnowledgeBaseModel.id.in_(base_ids),
                    _personal_privacy_guard(owner_id),
                    KnowledgeBaseModel.deleted_time.is_(None),
                )
            )
            if set(visible) != set(base_ids):
                raise LookupError("knowledge_base_not_found")

    async def search_keyword(
        self, owner_id: str, base_ids: list[str], tokens: tuple[str, ...], limit: int
    ) -> tuple[SearchHit, ...]:
        if not tokens:
            return ()
        tsquery = func.to_tsquery("simple", " | ".join(_tsquery_token(token) for token in tokens))
        async with self._sessions() as session:
            rows = await session.execute(
                _visible_chunks_query(owner_id, base_ids)
                .where(KnowledgeChunkModel.search_document.op("@@")(tsquery))
                .order_by(func.ts_rank_cd(KnowledgeChunkModel.search_document, tsquery).desc())
                .limit(min(limit, 40))
            )
            return tuple(
                _search_hit(chunk, title, base_id, "keyword", position)
                for position, (chunk, title, base_id) in enumerate(rows, start=1)
            )

    async def search_semantic(
        self,
        owner_id: str,
        base_ids: list[str],
        vector: list[float],
        limit: int,
        profile_id: str | None = None,
        *,
        min_score: float = 0.0,
    ) -> tuple[SearchHit, ...]:
        distance = KnowledgeChunkModel.embedding.cosine_distance(vector)
        query = _visible_chunks_query(owner_id, base_ids).where(
            KnowledgeChunkModel.embedding.is_not(None)
        )
        if profile_id is not None:
            query = query.where(KnowledgeVersionModel.profile_id == profile_id)
        if min_score > 0.0:
            # 余弦相似度 = 1 - cosine_distance，过滤相关性低于阈值的噪声片段。
            query = query.where(distance <= (1.0 - min_score))
        query = query.order_by(distance).limit(min(limit, 40))
        async with self._sessions() as session:
            rows = await session.execute(query)
            return tuple(
                _search_hit(chunk, title, base_id, "semantic", position)
                for position, (chunk, title, base_id) in enumerate(rows, start=1)
            )

    async def has_incompatible_profile(
        self, owner_id: str, base_ids: list[str], profile_id: str
    ) -> bool:
        """Report active embedded chunks that cannot share this query vector."""
        query = (
            select(KnowledgeChunkModel.id)
            .select_from(KnowledgeChunkModel)
            .join(KnowledgeVersionModel, KnowledgeVersionModel.id == KnowledgeChunkModel.version_id)
            .join(
                KnowledgeDocumentModel,
                KnowledgeDocumentModel.id == KnowledgeChunkModel.document_id,
            )
            .join(
                KnowledgeBaseModel,
                KnowledgeBaseModel.id == KnowledgeDocumentModel.knowledge_base_id,
            )
            .where(
                KnowledgeDocumentModel.knowledge_base_id.in_(base_ids),
                KnowledgeDocumentModel.deleted_time.is_(None),
                KnowledgeBaseModel.deleted_time.is_(None),
                _personal_privacy_guard(owner_id),
                KnowledgeDocumentModel.active_version_id == KnowledgeVersionModel.id,
                KnowledgeVersionModel.state == "ready",
                KnowledgeVersionModel.profile_id != profile_id,
                KnowledgeChunkModel.embedding.is_not(None),
            )
            .limit(1)
        )
        async with self._sessions() as session:
            return await session.scalar(query) is not None

    async def filter_active_hits(
        self, owner_id: str, hits: tuple[SearchHit, ...]
    ) -> tuple[SearchHit, ...]:
        """Return only hits still visible as the document's active version."""
        if not hits:
            return ()
        ids = [hit.chunk_id for hit in hits]
        async with self._sessions() as session:
            rows = await session.scalars(
                select(KnowledgeChunkModel.id)
                .join(
                    KnowledgeVersionModel,
                    KnowledgeVersionModel.id == KnowledgeChunkModel.version_id,
                )
                .join(
                    KnowledgeDocumentModel,
                    KnowledgeDocumentModel.id == KnowledgeChunkModel.document_id,
                )
                .join(
                    KnowledgeBaseModel,
                    KnowledgeBaseModel.id == KnowledgeDocumentModel.knowledge_base_id,
                )
                .where(
                    KnowledgeChunkModel.id.in_(ids),
                    KnowledgeDocumentModel.deleted_time.is_(None),
                    KnowledgeBaseModel.deleted_time.is_(None),
                    _personal_privacy_guard(owner_id),
                    KnowledgeDocumentModel.active_version_id == KnowledgeChunkModel.version_id,
                    KnowledgeVersionModel.state == "ready",
                )
            )
            active = set(rows)
        return tuple(hit for hit in hits if hit.chunk_id in active)


def _base(
    model: KnowledgeBaseModel,
    *,
    grants: tuple[VisibilityGrant, ...] = (),
) -> KnowledgeBase:
    return KnowledgeBase(
        model.id,
        model.owner_id,
        model.name,
        model.description,
        model.created_at,
        model.updated_at,
        model.deleted_time,
        visibility_scope=VisibilityScope(model.visibility_scope),
        owner_department_id=model.owner_department_id,
        visibility_grants=grants,
    )


def _document(
    model: KnowledgeDocumentModel,
    *,
    latest_version: DocumentVersion | None = None,
) -> Document:
    return Document(
        model.id,
        model.owner_id,
        model.knowledge_base_id,
        model.title,
        model.active_version_id,
        model.pending_version_id,
        model.created_at,
        model.updated_at,
        model.deleted_time,
        latest_version=latest_version,
    )


async def _load_latest_versions(
    session: AsyncSession, document_ids: Sequence[str]
) -> dict[str, DocumentVersion]:
    """用单次批量 SQL 为给定文档集合取回各自最新版本，同时兼容 PostgreSQL 与 SQLite。"""
    unique_ids = tuple(dict.fromkeys(document_ids))
    if not unique_ids:
        return {}
    # 按 document_id 分区并按 created_at DESC, id DESC 排序取首条，
    # 与 list_versions 顺序 100% 一致，不按版本上传者过滤以支持跨管理员协作。
    ranked_versions = (
        select(
            KnowledgeVersionModel.id.label("version_id"),
            func.row_number()
            .over(
                partition_by=KnowledgeVersionModel.document_id,
                order_by=(
                    KnowledgeVersionModel.created_at.desc(),
                    KnowledgeVersionModel.id.desc(),
                ),
            )
            .label("row_num"),
        )
        .where(
            KnowledgeVersionModel.document_id.in_(unique_ids),
        )
        .subquery()
    )
    rows = await session.scalars(
        select(KnowledgeVersionModel).join(
            ranked_versions,
            (KnowledgeVersionModel.id == ranked_versions.c.version_id)
            & (ranked_versions.c.row_num == 1),
        )
    )
    return {row.document_id: _version(row) for row in rows}


def _version(model: KnowledgeVersionModel) -> DocumentVersion:
    return DocumentVersion(
        id=model.id,
        document_id=model.document_id,
        state=DocumentState(model.state),
        error_code=model.error_code,
        warnings=tuple(model.warnings),
        chunk_count=model.chunk_count,
        created_at=model.created_at,
        completed_at=model.completed_at,
    )


def _locator(value: dict[str, object]) -> ChunkLocator:
    return ChunkLocator(
        kind=str(value.get("kind", "text")),
        page=_optional_int(value.get("page")),
        start_line=_optional_int(value.get("start_line")),
        end_line=_optional_int(value.get("end_line")),
        heading=_optional_str(value.get("heading")),
        sheet=_optional_str(value.get("sheet")),
        cell_range=_optional_str(value.get("cell_range")),
        slide=_optional_int(value.get("slide")),
        shape=_optional_str(value.get("shape")),
        media_id=_optional_str(value.get("media_id")),
    )


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _tsquery_token(token: str) -> str:
    """转义受控 token 中的反斜杠与单引号，避免用户输入破坏 tsquery 词法或被解释为运算符。"""
    escaped = token.replace("\\", "\\\\").replace("'", "''")
    return "'" + escaped + "'"


def _visible_chunks_query(
    owner_id: str, base_ids: list[str]
) -> Select[KnowledgeChunkModel, str, str]:
    return (
        select(
            KnowledgeChunkModel,
            KnowledgeDocumentModel.title,
            KnowledgeDocumentModel.knowledge_base_id,
        )
        .join(KnowledgeVersionModel, KnowledgeVersionModel.id == KnowledgeChunkModel.version_id)
        .join(KnowledgeDocumentModel, KnowledgeDocumentModel.id == KnowledgeChunkModel.document_id)
        .join(KnowledgeBaseModel, KnowledgeBaseModel.id == KnowledgeDocumentModel.knowledge_base_id)
        .where(
            KnowledgeDocumentModel.knowledge_base_id.in_(base_ids),
            KnowledgeDocumentModel.deleted_time.is_(None),
            KnowledgeBaseModel.deleted_time.is_(None),
            _personal_privacy_guard(owner_id),
            KnowledgeDocumentModel.active_version_id == KnowledgeVersionModel.id,
            KnowledgeVersionModel.state == "ready",
        )
    )


def _search_hit(
    chunk: KnowledgeChunkModel,
    title: str,
    base_id: str,
    channel: str,
    position: int,
) -> SearchHit:
    return SearchHit(
        chunk_id=chunk.id,
        document_id=chunk.document_id,
        version_id=chunk.version_id,
        title=title,
        text=chunk.content,
        locators=(_locator(chunk.locator),),
        channels=(channel,),
        rank=float(position),
        chunk_ordinal=chunk.ordinal,
        chunk_ids=(chunk.id,),
        knowledge_base_id=base_id,
    )


async def _next_version_time(
    session: AsyncSession, document_id: str, requested: datetime
) -> datetime:
    """同一文档内把并发或固定时钟下的版本创建时间严格递增。"""
    latest = await session.scalar(
        select(func.max(KnowledgeVersionModel.created_at)).where(
            KnowledgeVersionModel.document_id == document_id,
        )
    )
    if latest is not None:
        # 兼容 SQLite 返回无时区时间：补齐 UTC 后再与带时区时间比较。
        normalized_latest = latest.replace(tzinfo=UTC) if latest.tzinfo is None else latest
        normalized_requested = (
            requested.replace(tzinfo=UTC) if requested.tzinfo is None else requested
        )
        if normalized_latest >= normalized_requested:
            return normalized_latest + timedelta(microseconds=1)
    return requested


def _retry_delay_seconds(attempt: int, jitter_factor: float | None = None) -> float:
    """返回5/30/120秒指数退避，并乘以±20%随机抖动。"""
    if attempt < 1:
        raise ValueError("attempt_invalid")
    base = (5, 30, 120)[min(attempt - 1, 2)]
    factor = random.SystemRandom().uniform(0.8, 1.2) if jitter_factor is None else jitter_factor
    if not 0.8 <= factor <= 1.2:
        raise ValueError("jitter_factor_invalid")
    return base * factor
