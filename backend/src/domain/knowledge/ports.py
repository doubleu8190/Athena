"""知识库对外端口。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from domain.access import UserAccessSnapshot, VisibilityGrantInput, VisibilityScope
from domain.knowledge.model import (
    Document,
    DocumentVersion,
    KnowledgeBase,
    SearchHit,
    SourceReference,
    TextBlock,
)


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """文档提取的文本与诊断。"""

    blocks: tuple[TextBlock, ...]
    warnings: tuple[str, ...] = ()


class DocumentParser(Protocol):
    """按已确认格式提取文档文本，必要时执行 OCR。"""

    async def parse(self, filename: str, content: bytes) -> ParsedDocument: ...


class DocumentParserFactory(Protocol):
    """在解析进程内创建文档解析器；实现必须可跨进程序列化。"""

    def create(self) -> DocumentParser: ...


class DocumentFileValidator(Protocol):
    """在持久化前校验上传文件的大小、签名与格式。"""

    def validate_file(
        self, filename: str, path: Path, *, max_bytes: int = 20 * 1024 * 1024
    ) -> None: ...


class ChineseTokenizer(Protocol):
    """对中文全文检索输入进行确定性分词。"""

    def tokenize(self, text: str) -> tuple[str, ...]: ...


class OriginalStore(Protocol):
    """知识库原件的受控存储。"""

    async def put(self, key: str, content: bytes) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...

    def iter_orphan_candidates(self, *, older_than_seconds: int = 86400) -> AsyncIterator[str]: ...


class KnowledgeRepositoryPort(Protocol):
    """知识库应用服务使用的持久化边界。"""

    async def create_base(
        self,
        item: KnowledgeBase,
        *,
        visibility_grants: Sequence[VisibilityGrantInput] = (),
    ) -> KnowledgeBase: ...

    async def get_base(self, owner_id: str, base_id: str) -> KnowledgeBase | None: ...

    async def get_base_by_id(self, base_id: str) -> KnowledgeBase | None: ...

    async def is_storage_key_referenced(self, storage_key: str) -> bool: ...

    async def find_referenced_storage_keys(self, storage_keys: Sequence[str]) -> set[str]: ...

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
    ) -> tuple[Document, str] | None: ...

    async def list_bases(
        self, owner_id: str, *, offset: int, limit: int
    ) -> list[KnowledgeBase]: ...

    async def list_readable_bases(
        self,
        user_id: str,
        *,
        offset: int,
        limit: int,
        visibility_scope: VisibilityScope | None = None,
        owner_department_id: str | None = None,
        snapshot: UserAccessSnapshot | None = None,
    ) -> list[KnowledgeBase]: ...

    async def list_bases_by_ids(
        self,
        base_ids: Sequence[str],
        *,
        offset: int,
        limit: int,
        visibility_scope: VisibilityScope | None = None,
        owner_department_id: str | None = None,
    ) -> list[KnowledgeBase]: ...

    async def rename_base(
        self, owner_id: str, base_id: str, name: str, description: str | None, at: datetime
    ) -> KnowledgeBase | None: ...

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
    ) -> KnowledgeBase | None: ...

    async def soft_delete_base(self, owner_id: str, base_id: str, at: datetime) -> bool: ...

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
    ) -> tuple[Document, str]: ...

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
    ) -> tuple[Document, str]: ...

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
    ) -> tuple[Document, str]: ...

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
    ) -> tuple[Document, str]: ...

    async def list_documents(
        self, owner_id: str, base_id: str, *, offset: int, limit: int
    ) -> list[Document]: ...

    async def rename_document(
        self, owner_id: str, base_id: str, document_id: str, title: str, at: datetime
    ) -> Document | None: ...

    async def delete_document(
        self, owner_id: str, base_id: str, document_id: str
    ) -> list[str] | None: ...

    async def list_versions(
        self, owner_id: str, base_id: str, document_id: str
    ) -> list[DocumentVersion] | None: ...

    async def retry_version(
        self,
        owner_id: str,
        base_id: str,
        document_id: str,
        version_id: str,
        job_id: str,
        at: datetime,
    ) -> DocumentVersion | None: ...

    async def get_source(
        self, owner_id: str, base_id: str, document_id: str, version_id: str
    ) -> SourceReference | None: ...

    async def get_chunk(self, owner_id: str, base_id: str, chunk_id: str) -> SearchHit | None: ...

    async def get_document(
        self, owner_id: str, base_id: str, document_id: str
    ) -> Document | None: ...

    async def validate_search_scope(self, owner_id: str, base_ids: list[str]) -> None: ...

    async def search_keyword(
        self, owner_id: str, base_ids: list[str], tokens: tuple[str, ...], limit: int
    ) -> tuple[SearchHit, ...]: ...

    async def search_semantic(
        self,
        owner_id: str,
        base_ids: list[str],
        vector: list[float],
        limit: int,
        profile_id: str | None = None,
        *,
        min_score: float = 0.0,
    ) -> tuple[SearchHit, ...]: ...

    async def has_incompatible_profile(
        self, owner_id: str, base_ids: list[str], profile_id: str
    ) -> bool: ...

    async def filter_active_hits(
        self, owner_id: str, hits: tuple[SearchHit, ...]
    ) -> tuple[SearchHit, ...]: ...


@dataclass(frozen=True, slots=True)
class ClaimedKnowledgeJob:
    """持有一次租约和 fencing token 的待处理工作。"""

    job_id: str
    owner_id: str
    version_id: str
    document_id: str
    title: str
    source_text: str | None
    source_key: str | None
    source_filename: str | None
    fencing_token: int
    kind: str = "ingestion"
    source_keys: tuple[str, ...] = ()
    target_id: str = ""
    profile_id: str | None = None


class KnowledgeJobRepository(Protocol):
    """后台消费者使用的短事务任务边界。"""

    async def claim_job(
        self, *, lease_seconds: int, owner_concurrency: int = 2
    ) -> ClaimedKnowledgeJob | None: ...

    async def heartbeat_job(
        self, job_id: str, fencing_token: int, *, lease_seconds: int
    ) -> bool: ...

    async def publish_job(
        self,
        job: ClaimedKnowledgeJob,
        *,
        chunks: list[tuple[str, dict[str, object], int, str, list[float]]],
        warnings: list[str],
    ) -> bool: ...

    async def fail_job(
        self, job: ClaimedKnowledgeJob, *, error_code: str, retryable: bool
    ) -> None: ...

    async def finish_cleanup_job(self, job: ClaimedKnowledgeJob) -> bool: ...

    async def is_storage_key_referenced(self, storage_key: str) -> bool: ...

    async def find_referenced_storage_keys(self, storage_keys: Sequence[str]) -> set[str]: ...
