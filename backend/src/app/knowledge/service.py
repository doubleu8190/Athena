"""知识库目录、导入和检索用例。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import unicodedata
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import PurePath

from common.ids import new_id, now_utc
from domain.knowledge.model import (
    MAX_ORIGINAL_BYTES,
    Document,
    DocumentVersion,
    KnowledgeBase,
    OriginalStorageNotFoundError,
    OriginalStorageUnavailableError,
    SearchHit,
    SearchResult,
)
from domain.knowledge.ports import (
    ChineseTokenizer,
    KnowledgeRepositoryPort,
    OriginalStore,
)
from domain.llm.ports import EmbeddingPort, TokenCounterPort

MAX_PAGE_SIZE = 100
MAX_TEXT_LENGTH = 1_000_000
MAX_FILE_BYTES = MAX_ORIGINAL_BYTES
DEFAULT_CONTEXT_BUDGET = 4000
_MEDIA_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


class KnowledgeNotFoundError(LookupError):
    """资源不存在或不属于当前用户。"""


class KnowledgeInputError(ValueError):
    """知识库请求参数不符合约束。"""


class KnowledgeSearchUnavailableError(RuntimeError):
    """关键词与语义检索通道均不可用。"""


class KnowledgeService:
    """通过领域端口管理知识库并触发后台处理。"""

    def __init__(
        self,
        repository: KnowledgeRepositoryPort,
        tokenizer: ChineseTokenizer,
        embedding: EmbeddingPort | None = None,
        originals: OriginalStore | None = None,
        counter: TokenCounterPort | None = None,
        *,
        profile_id: str = "bge-m3-default",
        id_factory: Callable[[str], str] = new_id,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        """保存持久化、分词及可替换的模型档案、ID 和时钟来源。"""
        self._repository = repository
        self._tokenizer = tokenizer
        self._embedding = embedding
        self._originals = originals
        self._counter = counter
        self._profile_id = profile_id
        self._id_factory = id_factory
        self._clock = clock

    async def create_base(self, owner_id: str, name: str, description: str | None) -> KnowledgeBase:
        """创建用户私有知识库并校验名称和说明。"""
        owner = _required(owner_id, "owner_id_required")
        normalized_name = _clean_text(name, maximum=120, code="knowledge_base_name_invalid")
        normalized_description = (
            _clean_text(description, maximum=500, code="knowledge_base_description_invalid")
            if description is not None
            else None
        )
        at = _utc(self._clock())
        return await self._repository.create_base(
            KnowledgeBase(
                self._id_factory("kb"),
                owner,
                normalized_name,
                normalized_description,
                at,
                at,
            )
        )

    async def list_bases(self, owner_id: str, *, page: int, page_size: int) -> list[KnowledgeBase]:
        """分页读取当前用户的知识库。"""
        if page < 1 or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise KnowledgeInputError("invalid_pagination")
        return await self._repository.list_bases(
            _required(owner_id, "owner_id_required"),
            offset=(page - 1) * page_size,
            limit=page_size,
        )

    async def get_base(self, owner_id: str, base_id: str) -> KnowledgeBase:
        """读取当前用户可见知识库。"""
        base = await self._repository.get_base(_required(owner_id, "owner_id_required"), base_id)
        if base is None:
            raise KnowledgeNotFoundError("knowledge_base_not_found")
        return base

    async def rename_base(
        self, owner_id: str, base_id: str, name: str | None, description: str | None
    ) -> KnowledgeBase:
        """修改知识库名称或说明。"""
        if name is None and description is None:
            raise KnowledgeInputError("knowledge_base_patch_empty")
        current = await self.get_base(owner_id, base_id)
        updated = await self._repository.rename_base(
            current.owner_id,
            base_id,
            _clean_text(name, maximum=120, code="knowledge_base_name_invalid")
            if name is not None
            else current.name,
            _clean_text(description, maximum=500, code="knowledge_base_description_invalid")
            if description is not None
            else current.description,
            _utc(self._clock()),
        )
        if updated is None:
            raise KnowledgeNotFoundError("knowledge_base_not_found")
        return updated

    async def delete_base(self, owner_id: str, base_id: str) -> None:
        """软删除知识库，后续由清理任务回收内容。"""
        deleted = await self._repository.soft_delete_base(
            _required(owner_id, "owner_id_required"), base_id, _utc(self._clock())
        )
        if not deleted:
            raise KnowledgeNotFoundError("knowledge_base_not_found")

    async def create_text_document(
        self, owner_id: str, base_id: str, title: str, text: str, request_key: str
    ) -> tuple[Document, str]:
        """幂等登记文本导入任务，返回文档和版本身份。"""
        owner = _required(owner_id, "owner_id_required")
        await self.get_base(owner, base_id)
        normalized_title = _clean_text(title, maximum=255, code="document_title_invalid")
        normalized_text = unicodedata.normalize("NFC", text.strip())
        if not normalized_text or len(normalized_text) > MAX_TEXT_LENGTH:
            raise KnowledgeInputError("document_text_invalid")
        key = _required(request_key, "idempotency_key_required")
        if len(key) > 128:
            raise KnowledgeInputError("idempotency_key_invalid")
        digest = hashlib.sha256(normalized_text.encode()).hexdigest()
        request_hash = _canonical_request_hash(
            operation="create",
            target_id=base_id,
            title=normalized_title,
            filename=None,
            media_type=None,
            source_kind="text",
            content_hash=digest,
        )
        at = _utc(self._clock())
        return await self._repository.create_text_document(
            owner_id=owner,
            base_id=base_id,
            title=normalized_title,
            text=normalized_text,
            request_key=key,
            request_hash=request_hash,
            content_hash=digest,
            document_id=self._id_factory("kdoc"),
            version_id=self._id_factory("kver"),
            job_id=self._id_factory("kjob"),
            profile_id=self._profile_id,
            at=at,
        )

    async def create_file_document(
        self,
        owner_id: str,
        base_id: str,
        title: str,
        filename: str,
        content: bytes,
        request_key: str,
    ) -> tuple[Document, str]:
        """保存受限格式原件并原子登记文档、版本和后台任务。"""
        if self._originals is None:
            raise OriginalStorageUnavailableError()
        owner = _required(owner_id, "owner_id_required")
        await self.get_base(owner, base_id)
        normalized_title = _clean_text(title, maximum=255, code="document_title_invalid")
        safe_filename = unicodedata.normalize(
            "NFC", PurePath(filename.replace("\\", "/")).name.strip()
        )
        extension = PurePath(safe_filename).suffix.lower()
        media_type = _MEDIA_TYPES.get(extension)
        if not safe_filename or len(safe_filename) > 255 or media_type is None:
            raise KnowledgeInputError("document_type_unsupported")
        if not content or len(content) > MAX_FILE_BYTES:
            raise KnowledgeInputError("document_file_size_invalid")
        key = _required(request_key, "idempotency_key_required")
        if len(key) > 128:
            raise KnowledgeInputError("idempotency_key_invalid")
        sha256 = hashlib.sha256(content).hexdigest()
        request_hash = _canonical_request_hash(
            operation="create",
            target_id=base_id,
            title=normalized_title,
            filename=safe_filename,
            media_type=media_type,
            source_kind="file",
            content_hash=sha256,
        )
        owner_scope = hashlib.sha256(owner.encode()).hexdigest()
        at = _utc(self._clock())
        candidate_source_id = self._id_factory("ksrc")
        reservation = await self._repository.reserve_file_upload(
            owner_id=owner,
            operation="create",
            request_key=key,
            request_hash=request_hash,
            base_id=base_id,
            document_id=self._id_factory("kdoc"),
            size_bytes=len(content),
            reservation_token=self._id_factory("kres"),
            storage_key=(f"{owner_scope}/{candidate_source_id}/{sha256[:2]}/{sha256}"),
            source_id=candidate_source_id,
            version_id=self._id_factory("kver"),
            job_id=self._id_factory("kjob"),
            at=at,
        )
        if reservation.replay_document is not None:
            return reservation.replay_document, reservation.version_id
        if (
            reservation.reservation_token is None
            or reservation.storage_key is None
            or reservation.source_id is None
            or reservation.job_id is None
        ):
            raise RuntimeError("knowledge_upload_reservation_invalid")
        await self._originals.put(reservation.storage_key, content)
        return await self._repository.create_file_document(
            owner_id=owner,
            base_id=base_id,
            title=normalized_title,
            filename=safe_filename,
            media_type=media_type,
            storage_key=reservation.storage_key,
            size_bytes=len(content),
            sha256=sha256,
            request_key=key,
            request_hash=request_hash,
            document_id=reservation.document_id,
            version_id=reservation.version_id,
            source_id=reservation.source_id,
            job_id=reservation.job_id,
            reservation_token=reservation.reservation_token,
            profile_id=self._profile_id,
            at=at,
        )

    async def replace_text_document(
        self, owner_id: str, base_id: str, document_id: str, text: str, request_key: str
    ) -> tuple[Document, str]:
        """登记文档文本替换；处理成功前原活动版本仍可检索。"""
        owner = _required(owner_id, "owner_id_required")
        await self.get_document(owner, base_id, document_id)
        normalized = unicodedata.normalize("NFC", text.strip())
        if not normalized or len(normalized) > MAX_TEXT_LENGTH:
            raise KnowledgeInputError("document_text_invalid")
        key = _required(request_key, "idempotency_key_required")
        if len(key) > 128:
            raise KnowledgeInputError("idempotency_key_invalid")
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        request_hash = _canonical_request_hash(
            operation="replace",
            target_id=f"{base_id}/{document_id}",
            title=None,
            filename=None,
            media_type=None,
            source_kind="text",
            content_hash=digest,
        )
        at = _utc(self._clock())
        try:
            return await self._repository.replace_text_document(
                owner_id=owner,
                base_id=base_id,
                document_id=document_id,
                text=normalized,
                request_key=key,
                request_hash=request_hash,
                content_hash=digest,
                version_id=self._id_factory("kver"),
                job_id=self._id_factory("kjob"),
                profile_id=self._profile_id,
                at=at,
            )
        except ValueError as error:
            raise KnowledgeInputError(str(error)) from error

    async def replace_file_document(
        self,
        owner_id: str,
        base_id: str,
        document_id: str,
        filename: str,
        content: bytes,
        request_key: str,
    ) -> tuple[Document, str]:
        """将文件原件登记为待处理替换版本。"""
        if self._originals is None:
            raise OriginalStorageUnavailableError()
        owner = _required(owner_id, "owner_id_required")
        document = await self.get_document(owner, base_id, document_id)
        safe_filename = unicodedata.normalize(
            "NFC", PurePath(filename.replace("\\", "/")).name.strip()
        )
        extension = PurePath(safe_filename).suffix.lower()
        media_type = _MEDIA_TYPES.get(extension)
        if not safe_filename or len(safe_filename) > 255 or media_type is None:
            raise KnowledgeInputError("document_type_unsupported")
        if not content or len(content) > MAX_FILE_BYTES:
            raise KnowledgeInputError("document_file_size_invalid")
        key = _required(request_key, "idempotency_key_required")
        if len(key) > 128:
            raise KnowledgeInputError("idempotency_key_invalid")
        sha256 = hashlib.sha256(content).hexdigest()
        request_hash = _canonical_request_hash(
            operation="replace",
            target_id=f"{base_id}/{document_id}",
            title=None,
            filename=safe_filename,
            media_type=media_type,
            source_kind="file",
            content_hash=sha256,
        )
        owner_scope = hashlib.sha256(owner.encode()).hexdigest()
        at = _utc(self._clock())
        candidate_source_id = self._id_factory("ksrc")
        reservation = await self._repository.reserve_file_upload(
            owner_id=owner,
            operation="replace",
            request_key=key,
            request_hash=request_hash,
            base_id=base_id,
            document_id=document_id,
            size_bytes=len(content),
            reservation_token=self._id_factory("kres"),
            storage_key=(f"{owner_scope}/{candidate_source_id}/{sha256[:2]}/{sha256}"),
            source_id=candidate_source_id,
            version_id=self._id_factory("kver"),
            job_id=self._id_factory("kjob"),
            at=at,
        )
        if reservation.replay_document is not None:
            return reservation.replay_document, reservation.version_id
        if (
            reservation.reservation_token is None
            or reservation.storage_key is None
            or reservation.source_id is None
            or reservation.job_id is None
        ):
            raise RuntimeError("knowledge_upload_reservation_invalid")
        await self._originals.put(reservation.storage_key, content)
        return await self._repository.replace_file_document(
            owner_id=owner,
            base_id=base_id,
            document_id=document_id,
            title=document.title,
            filename=safe_filename,
            media_type=media_type,
            storage_key=reservation.storage_key,
            size_bytes=len(content),
            sha256=sha256,
            request_key=key,
            request_hash=request_hash,
            version_id=reservation.version_id,
            source_id=reservation.source_id,
            job_id=reservation.job_id,
            reservation_token=reservation.reservation_token,
            profile_id=self._profile_id,
            at=at,
        )

    async def list_documents(
        self, owner_id: str, base_id: str, *, page: int, page_size: int
    ) -> list[Document]:
        """分页列出知识库文档。"""
        await self.get_base(owner_id, base_id)
        if page < 1 or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise KnowledgeInputError("invalid_pagination")
        return await self._repository.list_documents(
            owner_id,
            base_id,
            offset=(page - 1) * page_size,
            limit=page_size,
        )

    async def get_document(self, owner_id: str, base_id: str, document_id: str) -> Document:
        """读取知识库内文档身份及当前版本状态指针。"""
        document = await self._repository.get_document(owner_id, base_id, document_id)
        if document is None:
            raise KnowledgeNotFoundError("document_not_found")
        return document

    async def rename_document(
        self, owner_id: str, base_id: str, document_id: str, title: str
    ) -> Document:
        """修改文档标题，不改变活动或待处理版本。"""
        normalized = _clean_text(title, maximum=255, code="document_title_invalid")
        await self.get_base(owner_id, base_id)
        document = await self._repository.rename_document(
            owner_id,
            base_id,
            document_id,
            normalized,
            _utc(self._clock()),
        )
        if document is None:
            raise KnowledgeNotFoundError("document_not_found")
        return document

    async def delete_document(self, owner_id: str, base_id: str, document_id: str) -> None:
        """软删除文档并登记后台原件回收，不依赖同步存储可用性。"""
        await self.get_base(owner_id, base_id)
        deleted = await self._repository.delete_document(owner_id, base_id, document_id)
        if deleted is None:
            raise KnowledgeNotFoundError("document_not_found")

    async def list_versions(
        self, owner_id: str, base_id: str, document_id: str
    ) -> list[DocumentVersion]:
        """按时间倒序读取可见文档的版本历史。"""
        versions = await self._repository.list_versions(owner_id, base_id, document_id)
        if versions is None:
            raise KnowledgeNotFoundError("document_not_found")
        return versions

    async def retry_version(
        self, owner_id: str, base_id: str, document_id: str, version_id: str
    ) -> DocumentVersion:
        """重试最新失败版本，复用其来源内容。"""
        try:
            version = await self._repository.retry_version(
                owner_id,
                base_id,
                document_id,
                version_id,
                self._id_factory("kjob"),
                _utc(self._clock()),
            )
        except ValueError as error:
            raise KnowledgeInputError(str(error)) from error
        if version is None:
            raise KnowledgeNotFoundError("version_not_found")
        return version

    async def source(
        self, owner_id: str, base_id: str, document_id: str, version_id: str
    ) -> tuple[str, str, bytes]:
        """授权读取指定历史版本原件或手动文本副本。"""
        reference = await self._repository.get_source(owner_id, base_id, document_id, version_id)
        if reference is None:
            raise KnowledgeNotFoundError("source_not_found")
        content = (
            reference.text.encode("utf-8")
            if reference.text is not None
            else await self._read_file_source(reference.storage_key)
        )
        return reference.filename, reference.media_type, content

    async def _read_file_source(self, storage_key: str | None) -> bytes:
        if storage_key is None:
            raise KnowledgeNotFoundError("source_not_found")
        if self._originals is None:
            raise OriginalStorageUnavailableError()
        try:
            return await self._originals.get(storage_key)
        except OriginalStorageNotFoundError as error:
            raise KnowledgeNotFoundError("source_not_found") from error

    async def chunk(self, owner_id: str, base_id: str, chunk_id: str) -> SearchHit:
        """读取指定知识库内的历史版本片段。"""
        hit = await self._repository.get_chunk(owner_id, base_id, chunk_id)
        if hit is None:
            raise KnowledgeNotFoundError("chunk_not_found")
        return hit

    async def search(
        self,
        owner_id: str,
        base_ids: list[str],
        query: str,
        *,
        limit: int = 8,
        context_budget: int = DEFAULT_CONTEXT_BUDGET,
    ) -> SearchResult:
        """隔离运行两路检索、RRF合并邻接片段并限制上下文token预算。"""
        normalized = query.strip()
        if (
            not normalized
            or len(normalized) > 4000
            or not 1 <= limit <= 20
            or not 1 <= context_budget <= 20_000
        ):
            raise KnowledgeInputError("search_request_invalid")
        if not base_ids or len(base_ids) > 20:
            raise KnowledgeInputError("knowledge_base_ids_invalid")
        await self._repository.validate_search_scope(owner_id, base_ids)
        tokens: tuple[str, ...] = ()
        keyword_reason: str | None = None
        try:
            tokens = self._tokenizer.tokenize(normalized)
        except Exception:
            keyword_reason = "keyword_tokenizer_unavailable"
        vector, semantic_reason = await self._query_vector(normalized)
        profile_degraded = False

        async def keyword_search() -> tuple[SearchHit, ...]:
            if keyword_reason is not None:
                raise RuntimeError(keyword_reason)
            return await self._repository.search_keyword(owner_id, base_ids, tokens, 40)

        async def semantic_search() -> tuple[SearchHit, ...]:
            nonlocal profile_degraded
            if vector is None:
                raise RuntimeError(semantic_reason or "semantic_unavailable")
            incompatible_check = getattr(self._repository, "has_incompatible_profile", None)
            has_incompatible = (
                await incompatible_check(owner_id, base_ids, self._profile_id)
                if callable(incompatible_check)
                else False
            )
            hits = await self._repository.search_semantic(
                owner_id, base_ids, vector, 40, self._profile_id
            )
            profile_degraded = has_incompatible
            if has_incompatible and not hits:
                raise RuntimeError("semantic_profile_incompatible")
            return hits

        keyword_result, semantic_result = await asyncio.gather(
            keyword_search(),
            semantic_search(),
            return_exceptions=True,
        )
        channels: list[str] = []
        degraded: list[str] = []
        ranked: dict[str, SearchHit] = {}
        scores: dict[str, float] = {}
        channel_results = (
            ("keyword", keyword_result),
            ("semantic", semantic_result),
        )
        for channel, result in channel_results:
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                reason = keyword_reason if channel == "keyword" else semantic_reason
                if channel == "semantic" and str(result) == "semantic_profile_incompatible":
                    reason = "semantic_profile_incompatible"
                degraded.append(reason or f"{channel}_search_unavailable")
                continue
            channels.append(channel)
            for position, hit in enumerate(result, start=1):
                previous = ranked.get(hit.chunk_id)
                ranked[hit.chunk_id] = (
                    replace(hit, channels=tuple(dict.fromkeys((*previous.channels, channel))))
                    if previous is not None
                    else hit
                )
                scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1 / (60 + position)
        if not channels:
            raise KnowledgeSearchUnavailableError("knowledge_search_unavailable")

        ordered = sorted(
            ranked.values(),
            key=lambda hit: (-scores[hit.chunk_id], hit.document_id, hit.chunk_ordinal),
        )
        merged = _merge_adjacent_hits(ordered, scores)
        bounded = _apply_context_budget(merged, context_budget, self._counter)
        if hasattr(self._repository, "filter_active_hits"):
            # 单次过滤已失效/已删除文档命中的片段，避免可见性变化时递归整次重跑。
            bounded = list(await self._repository.filter_active_hits(owner_id, tuple(bounded)))
        if profile_degraded:
            degraded.append("semantic_profile_incompatible")
        return SearchResult(tuple(bounded[:limit]), tuple(channels), tuple(dict.fromkeys(degraded)))

    async def _query_vector(self, query: str) -> tuple[list[float] | None, str | None]:
        if self._embedding is None:
            return None, "semantic_unavailable"
        try:
            vectors = await self._embedding.embed([query])
            if (
                len(vectors) != 1
                or len(vectors[0]) != 1024
                or not all(
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    for value in vectors[0]
                )
                or not any(value != 0 for value in vectors[0])
            ):
                return None, "embedding_profile_mismatch"
            return vectors[0], None
        except Exception:
            return None, "semantic_unavailable"


class _FallbackTokenCounter:
    """测试或嵌入场景未注入本地 tokenizer 时使用的保守字符预算。"""

    def count(self, text: str) -> int:
        return len(text)


def _merge_adjacent_hits(hits: list[SearchHit], scores: dict[str, float]) -> list[SearchHit]:
    """仅合并同文档同版本且 ordinal 连续的候选，并保留全部来源定位。"""
    by_document: dict[tuple[str, str], list[SearchHit]] = {}
    for hit in hits:
        by_document.setdefault((hit.document_id, hit.version_id), []).append(hit)
    merged: list[SearchHit] = []
    for document_hits in by_document.values():
        document_hits.sort(key=lambda hit: hit.chunk_ordinal)
        current: SearchHit | None = None
        current_score = 0.0
        for hit in document_hits:
            score = scores[hit.chunk_id]
            if current is not None and hit.chunk_ordinal == current.chunk_ordinal + 1:
                ids = current.chunk_ids or (current.chunk_id,)
                merged_ids = hit.chunk_ids or (hit.chunk_id,)
                current = replace(
                    current,
                    text=f"{current.text}\n{hit.text}",
                    locators=(*current.locators, *hit.locators),
                    channels=tuple(dict.fromkeys((*current.channels, *hit.channels))),
                    chunk_ids=(*ids, *merged_ids),
                    rank=max(current_score, score),
                    chunk_ordinal=hit.chunk_ordinal,
                )
                current_score = max(current_score, score)
                continue
            if current is not None:
                merged.append(replace(current, rank=current_score))
            current = replace(
                hit,
                rank=score,
                chunk_ids=hit.chunk_ids or (hit.chunk_id,),
            )
            current_score = score
        if current is not None:
            merged.append(replace(current, rank=current_score))
    return sorted(merged, key=lambda hit: (-hit.rank, hit.document_id, hit.chunk_ordinal))


def _apply_context_budget(
    hits: list[SearchHit], budget: int, counter: TokenCounterPort | None
) -> list[SearchHit]:
    """按排名消费 token 预算，并精确截断最后一个可容纳的片段。"""
    token_counter = counter or _FallbackTokenCounter()
    remaining = budget
    result: list[SearchHit] = []
    for hit in hits:
        token_count = token_counter.count(hit.text)
        if token_count <= remaining:
            result.append(hit)
            remaining -= token_count
            if remaining == 0:
                break
            continue
        if remaining > 0:
            prefix_length = _largest_token_prefix(hit.text, remaining, token_counter)
            if prefix_length:
                result.append(replace(hit, text=hit.text[:prefix_length]))
        break
    return result


def _largest_token_prefix(text: str, budget: int, counter: TokenCounterPort) -> int:
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if counter.count(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    return low


def _required(value: str, code: str) -> str:
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized:
        raise KnowledgeInputError(code)
    return normalized


def _canonical_request_hash(
    *,
    operation: str,
    target_id: str,
    title: str | None,
    filename: str | None,
    media_type: str | None,
    source_kind: str,
    content_hash: str,
) -> str:
    canonical = json.dumps(
        {
            "operation": operation,
            "target_id": target_id,
            "title": title,
            "filename": filename,
            "media_type": media_type,
            "source_kind": source_kind,
            "content_hash": content_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _clean_text(value: str, *, maximum: int, code: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(unicodedata.category(character) == "Cc" for character in normalized)
    ):
        raise KnowledgeInputError(code)
    return normalized


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
