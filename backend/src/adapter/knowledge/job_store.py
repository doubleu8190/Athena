"""知识库后台入库与清理任务的 SQLAlchemy 子仓储。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import cast

from sqlalchemy import delete, exists, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.access.repository import delete_resource_grants_in_session
from adapter.auth.model import UserModel
from adapter.knowledge._shared import (
    _PG_BATCH_INSERT_CHUNKS_SQL,
    PUBLISH_CHUNK_BATCH_SIZE,
    _activate_job_lease,
    _claimable_job_clauses,
    _delete_document_versions_and_requests,
    _delete_unused_sources,
    _format_pgvector_literal,
    _is_active_job_lease,
    _lease_expiry_expr,
    _lock_base,
    _lock_document,
    _lock_job,
    _retry_delay_seconds,
)
from adapter.knowledge.model import (
    KnowledgeChunkModel,
    KnowledgeDocumentModel,
    KnowledgeJobModel,
    KnowledgeSourceModel,
    KnowledgeVersionModel,
    ProjectKnowledgeBaseModel,
)
from common.ids import new_id
from domain.access import ResourceType
from domain.knowledge.ports import ClaimedKnowledgeJob


class KnowledgeJobStore:
    """负责后台 ingestion/cleanup 任务的租约认领、续租、发布、退避重试与物理清理。"""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def claim_job(
        self, *, lease_seconds: int, owner_concurrency: int = 2
    ) -> ClaimedKnowledgeJob | None:
        """无锁核验用户活跃并发上限，并利用任务表自身 SKIP LOCKED 认领任务递增 fencing token。"""
        if lease_seconds < 1 or owner_concurrency < 1:
            raise ValueError("lease_seconds_invalid")
        async with self._sessions() as session, session.begin():
            claimable = _claimable_job_clauses()
            candidates = await session.scalars(
                select(KnowledgeJobModel.owner_id)
                .where(*claimable)
                .order_by(KnowledgeJobModel.next_attempt_at, KnowledgeJobModel.created_at)
                .limit(100)
            )
            job = None
            for owner_id in dict.fromkeys(candidates):
                # 无锁存在性核验：严禁对 users 表加 FOR UPDATE，避免后台解析阻塞用户鉴权写操作。
                owner = await session.scalar(select(UserModel.id).where(UserModel.id == owner_id))
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
                # 限制单用户并发占用的任务数，避免过度占用数据库连接池与 CPU 资源。
                if int(active_count or 0) >= owner_concurrency:
                    continue
                job = await session.scalar(
                    select(KnowledgeJobModel)
                    .where(KnowledgeJobModel.owner_id == owner_id, *claimable)
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
                _activate_job_lease(job, lease_seconds)
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
            _activate_job_lease(job, lease_seconds)
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
            job = await _lock_job(session, job_id)
            now = cast(datetime, await session.scalar(select(func.clock_timestamp())))
            if not _is_active_job_lease(job, fencing_token, now):
                return False
            assert job is not None
            job.lease_until = _lease_expiry_expr(lease_seconds)
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
            # 严格维持 知识库 → 文档 → 任务 → 版本 行锁顺序，避免与管理端写操作交叉死锁。
            base = await _lock_base(session, base_id, active_only=False)
            document = await _lock_document(session, job.document_id, active_only=False)
            task = await _lock_job(session, job.job_id)
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
                or version is None
                or version.profile_id != job.profile_id
                or document.deleted_time is not None
                or not _is_active_job_lease(task, job.fencing_token, database_now)
                or document.pending_version_id != job.version_id
            ):
                return False
            assert task is not None
            await session.execute(
                delete(KnowledgeChunkModel)
                .where(KnowledgeChunkModel.version_id == job.version_id)
                .execution_options(synchronize_session=False)
            )
            # 按每批 200 条通过 PostgreSQL unnest 批量写入分片，
            # 缩短持有 knowledge_bases FOR UPDATE 行锁的事务时长。
            for batch_start in range(0, len(chunks), PUBLISH_CHUNK_BATCH_SIZE):
                batch = chunks[batch_start : batch_start + PUBLISH_CHUNK_BATCH_SIZE]
                ids: list[str] = []
                ordinals: list[int] = []
                contents: list[str] = []
                locators: list[str] = []
                token_counts: list[int] = []
                search_texts: list[str] = []
                embeddings: list[str | None] = []
                for offset, (
                    chunk_text,
                    locator,
                    token_count,
                    search_text,
                    vector,
                ) in enumerate(batch):
                    ids.append(new_id("kchunk"))
                    ordinals.append(batch_start + offset)
                    contents.append(chunk_text)
                    locators.append(json.dumps(locator, ensure_ascii=False, separators=(",", ":")))
                    token_counts.append(token_count)
                    search_texts.append(search_text)
                    embeddings.append(_format_pgvector_literal(vector))
                await session.execute(
                    _PG_BATCH_INSERT_CHUNKS_SQL,
                    {
                        "document_id": job.document_id,
                        "version_id": job.version_id,
                        "ids": ids,
                        "ordinals": ordinals,
                        "contents": contents,
                        "locators": locators,
                        "token_counts": token_counts,
                        "search_texts": search_texts,
                        "embeddings": embeddings,
                    },
                )

            # 1A 方案清理旧版本索引列：先借 ix_knowledge_versions_doc_created 查出同文档旧版本 ID，
            # 再按 version_id IN (...) 走 (version_id, ordinal) 唯一索引置空向量与全文索引列，
            # 既彻底消除 HNSW/GIN 索引膨胀又 100% 保留 get_chunk 读取历史正文与定位器的契约。
            old_version_ids = list(
                await session.scalars(
                    select(KnowledgeVersionModel.id).where(
                        KnowledgeVersionModel.document_id == job.document_id,
                        KnowledgeVersionModel.id != job.version_id,
                    )
                )
            )
            if old_version_ids:
                empty_search_document = func.to_tsvector("simple", "")
                await session.execute(
                    update(KnowledgeChunkModel)
                    .where(
                        KnowledgeChunkModel.version_id.in_(old_version_ids),
                        or_(
                            KnowledgeChunkModel.embedding.is_not(None),
                            KnowledgeChunkModel.search_document != empty_search_document,
                        ),
                    )
                    .values(
                        embedding=None,
                        search_document=empty_search_document,
                    )
                    .execution_options(synchronize_session=False)
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
                await _lock_base(session, base_id, active_only=False)
            document = await _lock_document(session, job.document_id, active_only=False)
            task = await _lock_job(session, job.job_id)
            version = await session.scalar(
                select(KnowledgeVersionModel)
                .where(KnowledgeVersionModel.id == job.version_id)
                .with_for_update()
            )
            database_now = cast(datetime, await session.scalar(select(func.clock_timestamp())))
            if not _is_active_job_lease(task, job.fencing_token, database_now):
                return
            assert task is not None
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
                await _lock_base(session, base_id, active_only=False)
                document = await _lock_document(session, document_id, active_only=False)
                task = await _lock_job(session, job.job_id)
                now = cast(datetime, await session.scalar(select(func.clock_timestamp())))
                if (
                    document is None
                    or document.deleted_time is None
                    or not _is_active_job_lease(task, job.fencing_token, now)
                ):
                    return False
                assert task is not None
                candidate_source_ids = await _delete_document_versions_and_requests(
                    session,
                    (document_id,),
                    exclude_job_id=task.id,
                )
                await _delete_unused_sources(session, candidate_source_ids)
                task.state = "done"
                task.lease_until = None
                task.updated_at = func.clock_timestamp()
                return True
            base = await _lock_base(session, job.target_id, active_only=False)
            task = await _lock_job(session, job.job_id)
            now = cast(datetime, await session.scalar(select(func.clock_timestamp())))
            if (
                base is None
                or base.deleted_time is None
                or not _is_active_job_lease(task, job.fencing_token, now)
            ):
                return False
            assert task is not None
            document_ids = list(
                await session.scalars(
                    select(KnowledgeDocumentModel.id).where(
                        KnowledgeDocumentModel.knowledge_base_id == base.id,
                    )
                )
            )
            candidate_source_ids = await _delete_document_versions_and_requests(
                session,
                document_ids,
                exclude_job_id=task.id,
                include_doc_cleanup_jobs=True,
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
            await _delete_unused_sources(session, candidate_source_ids)
            await session.delete(base)
            task.state = "done"
            task.lease_until = None
            task.updated_at = func.clock_timestamp()
            return True
