"""知识库文档、版本、原件与上传幂等预检的 SQLAlchemy 子仓储。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.auth.model import UserModel
from adapter.knowledge._shared import (
    _document,
    _FileSourceInput,
    _load_latest_versions,
    _lock_base,
    _lock_document,
    _new_queued_job,
    _next_version_time,
    _personal_privacy_guard,
    _version,
    _visible_documents_query,
)
from adapter.knowledge.model import (
    KnowledgeBaseModel,
    KnowledgeDocumentModel,
    KnowledgeJobModel,
    KnowledgeRequestModel,
    KnowledgeSourceModel,
    KnowledgeVersionModel,
)
from common.ids import new_id
from domain.knowledge.model import Document, DocumentVersion, SourceReference


class KnowledgeDocumentStore:
    """负责文档与版本的生命周期写入、幂等重放、配额校验与原件引用查询。"""

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
        """无锁复核用户当前文档数与原件字节配额（调用方已在事务入口核验用户存在）。"""
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
        if int(documents or 0) + additional_documents > 1000:
            raise ValueError("knowledge_document_quota_exceeded")
        if int(byte_count or 0) + additional_bytes > 1024 * 1024 * 1024:
            raise ValueError("knowledge_storage_quota_exceeded")

    async def _ensure_owner_exists(self, session: AsyncSession, owner_id: str) -> None:
        """无锁核验操作者账号存在，严禁对 users 表加行锁以避免与认证写操作争用。"""
        owner = await session.scalar(select(UserModel.id).where(UserModel.id == owner_id))
        if owner is None:
            raise LookupError("owner_not_found")

    async def _lookup_idempotent_request(
        self,
        session: AsyncSession,
        *,
        owner_id: str,
        operation: str,
        request_key: str,
        request_hash: str,
    ) -> tuple[Document, str] | None:
        """核对已完成幂等请求：哈希一致返回重放文档与版本，哈希不一致抛出冲突异常。"""
        existing = await session.scalar(
            select(KnowledgeRequestModel).where(
                KnowledgeRequestModel.owner_id == owner_id,
                KnowledgeRequestModel.operation == operation,
                KnowledgeRequestModel.request_key == request_key,
            )
        )
        if existing is None:
            return None
        if existing.request_hash != request_hash:
            raise ValueError("idempotency_conflict")
        # 协作场景下文档创建者可能与替换操作者不同，幂等重放仅按 document_id 查找。
        existing_document = await session.scalar(
            select(KnowledgeDocumentModel).where(
                KnowledgeDocumentModel.id == existing.document_id,
            )
        )
        if existing_document is None:
            raise LookupError("document_not_found")
        return _document(existing_document), existing.version_id

    async def is_storage_key_referenced(self, storage_key: str) -> bool:
        """校验单个对象 key 是否仍被来源表引用。"""
        async with self._sessions() as session:
            source_reference = exists().where(KnowledgeSourceModel.storage_key == storage_key)
            return bool(await session.scalar(select(source_reference)))

    async def find_referenced_storage_keys(self, storage_keys: Sequence[str]) -> set[str]:
        """单条 SQL 批量核验一组对象 key 的来源引用状态，避免孤儿扫描 N+1 查库。"""
        unique_keys = tuple(dict.fromkeys(storage_keys))
        if not unique_keys:
            return set()
        query = select(KnowledgeSourceModel.storage_key).where(
            KnowledgeSourceModel.storage_key.in_(unique_keys)
        )
        async with self._sessions() as session:
            rows = await session.scalars(query)
            return set(rows)

    async def preflight_file_upload(
        self,
        *,
        owner_id: str,
        operation: str,
        request_key: str,
        request_hash: str,
        base_id: str,
        document_id: str | None,
        size_bytes: int,
    ) -> tuple[Document, str] | None:
        """写 MinIO 前的无锁只读预检：拦截重放/冲突、无效目标、进行中替换与超配额。"""
        if operation not in {"create", "replace"} or size_bytes < 1:
            raise ValueError("knowledge_upload_preflight_invalid")
        if operation == "replace" and not document_id:
            raise ValueError("knowledge_upload_preflight_invalid")
        async with self._sessions() as session:
            await self._ensure_owner_exists(session, owner_id)
            replay = await self._lookup_idempotent_request(
                session,
                owner_id=owner_id,
                operation=operation,
                request_key=request_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay
            base = await session.scalar(
                select(KnowledgeBaseModel.id).where(
                    KnowledgeBaseModel.id == base_id,
                    _personal_privacy_guard(owner_id),
                    KnowledgeBaseModel.deleted_time.is_(None),
                )
            )
            if base is None:
                raise LookupError("knowledge_base_not_found")
            if operation == "replace":
                document = await session.scalar(
                    select(KnowledgeDocumentModel).where(
                        KnowledgeDocumentModel.id == document_id,
                        KnowledgeDocumentModel.knowledge_base_id == base_id,
                        KnowledgeDocumentModel.deleted_time.is_(None),
                    )
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
            return None

    async def get_document(self, owner_id: str, base_id: str, document_id: str) -> Document | None:
        """读取可见文档及最新版本摘要。"""
        async with self._sessions() as session:
            model = await session.scalar(
                _visible_documents_query(owner_id, base_id).where(
                    KnowledgeDocumentModel.id == document_id,
                )
            )
            if model is None:
                return None
            # 在同一读取会话中装配最新版本摘要，不按版本创建者过滤以支持跨管理员协作。
            latest_by_doc = await _load_latest_versions(session, (model.id,))
            return _document(model, latest_version=latest_by_doc.get(model.id))

    async def _save_document_version(
        self,
        *,
        operation: str,
        owner_id: str,
        base_id: str,
        document_id: str,
        title: str | None,
        source_text: str | None,
        file_source: _FileSourceInput | None,
        content_hash: str,
        request_key: str,
        request_hash: str,
        version_id: str,
        job_id: str,
        profile_id: str,
        at: datetime,
    ) -> tuple[Document, str]:
        """单事务保存文档创建/替换版本、原件、入库任务与幂等记录。"""
        async with self._sessions() as session, session.begin():
            await self._ensure_owner_exists(session, owner_id)
            # 锁前快速幂等检查：重放或跨库同键冲突直接返回/报错，避免无效行锁等待。
            replay = await self._lookup_idempotent_request(
                session,
                owner_id=owner_id,
                operation=operation,
                request_key=request_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay
            # 锁定目标知识库串行化同库写入与配额核验，再复查幂等记录防止并发同键撞主键。
            base = await _lock_base(session, base_id, owner_id=owner_id)
            if base is None:
                raise LookupError("knowledge_base_not_found")
            replay = await self._lookup_idempotent_request(
                session,
                owner_id=owner_id,
                operation=operation,
                request_key=request_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay
            if operation == "replace":
                # 部门/公司库允许不同管理员替换既有文档，不按 KnowledgeDocumentModel.owner_id 过滤。
                document = await _lock_document(session, document_id, base_id=base_id)
                if document is None:
                    raise LookupError("document_not_found")
                if document.pending_version_id is not None:
                    raise ValueError("document_update_in_progress")
                version_created_at = await _next_version_time(session, document_id, at)
            else:
                assert title is not None
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
                version_created_at = at
            await self._check_owner_quota(
                session,
                owner_id,
                additional_bytes=file_source.size_bytes if file_source is not None else 0,
                additional_documents=1 if operation == "create" else 0,
            )
            source_id: str | None = None
            if file_source is not None:
                # 每次上传均分配独立 source_id 并编入四段式 storage_key，直接写入新原件记录。
                source = KnowledgeSourceModel(
                    id=file_source.source_id,
                    owner_id=owner_id,
                    storage_key=file_source.storage_key,
                    filename=file_source.filename,
                    media_type=file_source.media_type,
                    size_bytes=file_source.size_bytes,
                    sha256=file_source.sha256,
                    created_at=at,
                )
                session.add(source)
                source_id = source.id
            version = KnowledgeVersionModel(
                id=version_id,
                document_id=document_id,
                owner_id=owner_id,
                source_id=source_id,
                source_text=source_text,
                content_hash=content_hash,
                profile_id=profile_id,
                state="queued",
                warnings=[],
                chunk_count=0,
                created_at=version_created_at,
            )
            job = _new_queued_job(
                job_id=job_id,
                owner_id=owner_id,
                kind="ingestion",
                target_id=version_id,
                at=at,
            )
            request = KnowledgeRequestModel(
                owner_id=owner_id,
                operation=operation,
                request_key=request_key,
                request_hash=request_hash,
                document_id=document_id,
                version_id=version_id,
                created_at=at,
            )
            if operation == "create":
                session.add(document)
            else:
                document.pending_version_id = version_id
                document.updated_at = at
            session.add_all([version, job, request])
            await session.flush()
            return _document(document), version_id

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
        return await self._save_document_version(
            operation="create",
            owner_id=owner_id,
            base_id=base_id,
            document_id=document_id,
            title=title,
            source_text=text,
            file_source=None,
            content_hash=content_hash,
            request_key=request_key,
            request_hash=request_hash,
            version_id=version_id,
            job_id=job_id,
            profile_id=profile_id,
            at=at,
        )

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
        job_id: str,
        profile_id: str,
        at: datetime,
    ) -> tuple[Document, str]:
        """在单一事务中登记上传原件、待处理版本和幂等结果。"""
        return await self._save_document_version(
            operation="create",
            owner_id=owner_id,
            base_id=base_id,
            document_id=document_id,
            title=title,
            source_text=None,
            file_source=_FileSourceInput(
                source_id=source_id,
                filename=filename,
                media_type=media_type,
                storage_key=storage_key,
                size_bytes=size_bytes,
                sha256=sha256,
            ),
            content_hash=sha256,
            request_key=request_key,
            request_hash=request_hash,
            version_id=version_id,
            job_id=job_id,
            profile_id=profile_id,
            at=at,
        )

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
        return await self._save_document_version(
            operation="replace",
            owner_id=owner_id,
            base_id=base_id,
            document_id=document_id,
            title=None,
            source_text=text,
            file_source=None,
            content_hash=content_hash,
            request_key=request_key,
            request_hash=request_hash,
            version_id=version_id,
            job_id=job_id,
            profile_id=profile_id,
            at=at,
        )

    async def replace_file_document(
        self,
        *,
        owner_id: str,
        base_id: str,
        document_id: str,
        filename: str,
        media_type: str,
        storage_key: str,
        size_bytes: int,
        sha256: str,
        request_key: str,
        request_hash: str,
        version_id: str,
        source_id: str,
        job_id: str,
        profile_id: str,
        at: datetime,
    ) -> tuple[Document, str]:
        """以幂等事务保存新原件和版本，保留当前活动版本直到处理成功。"""
        return await self._save_document_version(
            operation="replace",
            owner_id=owner_id,
            base_id=base_id,
            document_id=document_id,
            title=None,
            source_text=None,
            file_source=_FileSourceInput(
                source_id=source_id,
                filename=filename,
                media_type=media_type,
                storage_key=storage_key,
                size_bytes=size_bytes,
                sha256=sha256,
            ),
            content_hash=sha256,
            request_key=request_key,
            request_hash=request_hash,
            version_id=version_id,
            job_id=job_id,
            profile_id=profile_id,
            at=at,
        )

    async def list_documents(
        self, owner_id: str, base_id: str, *, offset: int, limit: int
    ) -> list[Document]:
        """分页列出指定知识库下可见文档，并批量装配最新版本摘要。"""
        async with self._sessions() as session:
            rows = list(
                await session.scalars(
                    _visible_documents_query(owner_id, base_id)
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
        """在知识库→文档行锁顺序内重命名文档并返回最新摘要。"""
        async with self._sessions() as session, session.begin():
            base = await _lock_base(session, base_id, owner_id=owner_id)
            if base is None:
                return None
            document = await _lock_document(session, document_id, base_id=base_id)
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
            base = await _lock_base(session, base_id, owner_id=owner_id, active_only=False)
            if base is None:
                return None
            document = await _lock_document(
                session, document_id, base_id=base_id, active_only=False
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
                _new_queued_job(
                    job_id=new_id("kjob"),
                    owner_id=owner_id,
                    kind="cleanup",
                    target_id=f"doc:{document_id}",
                    at=func.clock_timestamp(),
                )
            )
            return []

    async def list_versions(
        self, owner_id: str, base_id: str, document_id: str
    ) -> list[DocumentVersion] | None:
        """列出指定可见文档的全部版本历史（按创建时间倒序）。"""
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
            base = await _lock_base(session, base_id, owner_id=owner_id)
            if base is None:
                return None
            document = await _lock_document(session, document_id, base_id=base_id)
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
                existing_job = _new_queued_job(
                    job_id=job_id,
                    owner_id=owner_id,
                    kind="ingestion",
                    target_id=version_id,
                    at=at,
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
        """读取指定版本的原件引用（文本版本直接返回正文，文件版本返回 storage_key）。"""
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
