"""知识库目录与导入记录的 SQLAlchemy 仓储门面（组合子仓储实现）。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.knowledge._shared import (
    MAX_KEYWORD_TOKENS as MAX_KEYWORD_TOKENS,
)
from adapter.knowledge._shared import (
    PUBLISH_CHUNK_BATCH_SIZE as PUBLISH_CHUNK_BATCH_SIZE,
)
from adapter.knowledge._shared import (
    _retry_delay_seconds as _retry_delay_seconds,
)
from adapter.knowledge._shared import (
    _select_keyword_tokens as _select_keyword_tokens,
)
from adapter.knowledge._shared import (
    _tsquery_token as _tsquery_token,
)
from adapter.knowledge._shared import (
    _visible_chunks_query as _visible_chunks_query,
)
from adapter.knowledge.base_store import KnowledgeBaseStore
from adapter.knowledge.document_store import KnowledgeDocumentStore
from adapter.knowledge.job_store import KnowledgeJobStore
from adapter.knowledge.search_store import KnowledgeSearchStore
from domain.access import UserAccessSnapshot, VisibilityGrantInput, VisibilityScope
from domain.knowledge.model import (
    Document,
    DocumentVersion,
    KnowledgeBase,
    SearchHit,
    SourceReference,
)
from domain.knowledge.ports import ClaimedKnowledgeJob


class KnowledgeRepository:
    """执行知识库事务与 owner 范围查询的组合门面。"""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._bases = KnowledgeBaseStore(sessions)
        self._documents = KnowledgeDocumentStore(sessions)
        self._jobs = KnowledgeJobStore(sessions)
        self._search = KnowledgeSearchStore(sessions)

    async def is_storage_key_referenced(self, storage_key: str) -> bool:
        """校验单个对象 key 是否仍被来源表引用。"""
        return await self._documents.is_storage_key_referenced(storage_key)

    async def find_referenced_storage_keys(self, storage_keys: Sequence[str]) -> set[str]:
        """单条 SQL 批量核验一组对象 key 的来源引用状态，避免孤儿扫描 N+1 查库。"""
        return await self._documents.find_referenced_storage_keys(storage_keys)

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
        return await self._documents.preflight_file_upload(
            owner_id=owner_id,
            operation=operation,
            request_key=request_key,
            request_hash=request_hash,
            base_id=base_id,
            document_id=document_id,
            size_bytes=size_bytes,
        )

    async def claim_job(
        self, *, lease_seconds: int, owner_concurrency: int = 2
    ) -> ClaimedKnowledgeJob | None:
        """无锁核验用户活跃并发上限，并利用任务表自身 SKIP LOCKED 认领任务递增 fencing token。"""
        return await self._jobs.claim_job(
            lease_seconds=lease_seconds,
            owner_concurrency=owner_concurrency,
        )

    async def heartbeat_job(self, job_id: str, fencing_token: int, *, lease_seconds: int) -> bool:
        """仅当前 fencing token 可续租。"""
        return await self._jobs.heartbeat_job(
            job_id,
            fencing_token,
            lease_seconds=lease_seconds,
        )

    async def publish_job(
        self,
        job: ClaimedKnowledgeJob,
        *,
        chunks: list[tuple[str, dict[str, object], int, str, list[float]]],
        warnings: list[str],
    ) -> bool:
        """短事务重建片段并发布活动版本，拒绝过期 worker 写入。"""
        return await self._jobs.publish_job(job, chunks=chunks, warnings=warnings)

    async def fail_job(self, job: ClaimedKnowledgeJob, *, error_code: str, retryable: bool) -> None:
        """按 fencing token 记录失败并安排有限退避重试。"""
        await self._jobs.fail_job(job, error_code=error_code, retryable=retryable)

    async def finish_cleanup_job(self, job: ClaimedKnowledgeJob) -> bool:
        """fencing 校验后清除已软删除文档或知识库的数据库记录。"""
        return await self._jobs.finish_cleanup_job(job)

    async def list_project_bases(self, owner_id: str, project_id: str) -> list[KnowledgeBase]:
        """读取用户项目关联且当前未删除的知识库（含可见性授权信息）。"""
        return await self._bases.list_project_bases(owner_id, project_id)

    async def bind_project_base(
        self, owner_id: str, project_id: str, base_id: str, at: datetime
    ) -> bool:
        """按项目→知识库锁顺序幂等关联用户项目与有权限的知识库。"""
        return await self._bases.bind_project_base(owner_id, project_id, base_id, at)

    async def unbind_project_base(self, owner_id: str, project_id: str, base_id: str) -> bool:
        """按项目→知识库锁顺序幂等解除关联（即使调岗后仍允许解绑旧部门库）。"""
        return await self._bases.unbind_project_base(owner_id, project_id, base_id)

    async def create_base(
        self,
        item: KnowledgeBase,
        *,
        visibility_grants: Sequence[VisibilityGrantInput] = (),
    ) -> KnowledgeBase:
        """在单事务内原子创建知识库并写入关联的可见性授权。"""
        return await self._bases.create_base(item, visibility_grants=visibility_grants)

    async def get_base_by_id(self, base_id: str) -> KnowledgeBase | None:
        """按主键读取未软删除的知识库（供权限引擎统一鉴权使用）。"""
        return await self._bases.get_base_by_id(base_id)

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
        return await self._bases.list_readable_bases(
            user_id,
            offset=offset,
            limit=limit,
            visibility_scope=visibility_scope,
            owner_department_id=owner_department_id,
            snapshot=snapshot,
        )

    async def update_base(
        self,
        base_id: str,
        *,
        name: str,
        description: str | None,
        at: datetime,
        visibility_scope: VisibilityScope | None = None,
        owner_department_id: str | None = None,
        visibility_grants: Sequence[VisibilityGrantInput] | None = None,
    ) -> KnowledgeBase | None:
        """在单事务行锁内更新知识库元数据，仅当提供 visibility_scope 时才变更可见性与授权。"""
        return await self._bases.update_base(
            base_id,
            name=name,
            description=description,
            at=at,
            visibility_scope=visibility_scope,
            owner_department_id=owner_department_id,
            visibility_grants=visibility_grants,
        )

    async def soft_delete_base(self, owner_id: str, base_id: str, at: datetime) -> bool:
        """在单事务内软删除知识库、清理全部项目绑定及可见性授权并入队清理任务。"""
        return await self._bases.soft_delete_base(owner_id, base_id, at)

    async def get_document(self, owner_id: str, base_id: str, document_id: str) -> Document | None:
        """读取可见文档及最新版本摘要。"""
        return await self._documents.get_document(owner_id, base_id, document_id)

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
        return await self._documents.create_text_document(
            owner_id=owner_id,
            base_id=base_id,
            title=title,
            text=text,
            request_key=request_key,
            request_hash=request_hash,
            content_hash=content_hash,
            document_id=document_id,
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
        return await self._documents.create_file_document(
            owner_id=owner_id,
            base_id=base_id,
            title=title,
            filename=filename,
            media_type=media_type,
            storage_key=storage_key,
            size_bytes=size_bytes,
            sha256=sha256,
            request_key=request_key,
            request_hash=request_hash,
            document_id=document_id,
            version_id=version_id,
            source_id=source_id,
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
        return await self._documents.replace_text_document(
            owner_id=owner_id,
            base_id=base_id,
            document_id=document_id,
            text=text,
            request_key=request_key,
            request_hash=request_hash,
            content_hash=content_hash,
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
        return await self._documents.replace_file_document(
            owner_id=owner_id,
            base_id=base_id,
            document_id=document_id,
            filename=filename,
            media_type=media_type,
            storage_key=storage_key,
            size_bytes=size_bytes,
            sha256=sha256,
            request_key=request_key,
            request_hash=request_hash,
            version_id=version_id,
            source_id=source_id,
            job_id=job_id,
            profile_id=profile_id,
            at=at,
        )

    async def list_documents(
        self, owner_id: str, base_id: str, *, offset: int, limit: int
    ) -> list[Document]:
        """分页列出指定知识库下可见文档，并批量装配最新版本摘要。"""
        return await self._documents.list_documents(owner_id, base_id, offset=offset, limit=limit)

    async def rename_document(
        self, owner_id: str, base_id: str, document_id: str, title: str, at: datetime
    ) -> Document | None:
        """在知识库→文档行锁顺序内重命名文档并返回最新摘要。"""
        return await self._documents.rename_document(owner_id, base_id, document_id, title, at)

    async def delete_document(
        self, owner_id: str, base_id: str, document_id: str
    ) -> list[str] | None:
        """软删除文档并清除派生版本，返回不再被引用的原件 key。"""
        return await self._documents.delete_document(owner_id, base_id, document_id)

    async def list_versions(
        self, owner_id: str, base_id: str, document_id: str
    ) -> list[DocumentVersion] | None:
        """列出指定可见文档的全部版本历史（按创建时间倒序）。"""
        return await self._documents.list_versions(owner_id, base_id, document_id)

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
        return await self._documents.retry_version(
            owner_id, base_id, document_id, version_id, job_id, at
        )

    async def get_source(
        self, owner_id: str, base_id: str, document_id: str, version_id: str
    ) -> SourceReference | None:
        """读取指定版本的原件引用（文本版本直接返回正文，文件版本返回 storage_key）。"""
        return await self._documents.get_source(owner_id, base_id, document_id, version_id)

    async def get_chunk(self, owner_id: str, base_id: str, chunk_id: str) -> SearchHit | None:
        """按知识库范围读取单个分片的正文与定位信息。"""
        return await self._search.get_chunk(owner_id, base_id, chunk_id)

    async def search_keyword(
        self, owner_id: str, base_ids: list[str], tokens: tuple[str, ...], limit: int
    ) -> tuple[SearchHit, ...]:
        """执行基于 GIN tsvector 的受控关键词检索并按相关性排序。"""
        return await self._search.search_keyword(owner_id, base_ids, tokens, limit)

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
        """在事务局部开启 pgvector HNSW 迭代扫描并执行余弦距离语义检索。"""
        return await self._search.search_semantic(
            owner_id,
            base_ids,
            vector,
            limit,
            profile_id,
            min_score=min_score,
        )

    async def has_incompatible_profile(
        self, owner_id: str, base_ids: list[str], profile_id: str
    ) -> bool:
        """检查目标知识库集合中是否存在使用不同嵌入 profile 的活跃分片。"""
        return await self._search.has_incompatible_profile(owner_id, base_ids, profile_id)

    async def filter_active_hits(
        self, owner_id: str, hits: tuple[SearchHit, ...]
    ) -> tuple[SearchHit, ...]:
        """仅保留当前仍属于文档活动版本且未软删除的命中分片。"""
        return await self._search.filter_active_hits(owner_id, hits)


__all__ = [
    "KnowledgeRepository",
    "MAX_KEYWORD_TOKENS",
    "PUBLISH_CHUNK_BATCH_SIZE",
    "_retry_delay_seconds",
    "_select_keyword_tokens",
    "_tsquery_token",
    "_visible_chunks_query",
]
