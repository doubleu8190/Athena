"""知识库目录、导入和检索用例。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Final

from app.access import AccessPolicyService
from common.ids import new_id, now_utc
from domain.access import (
    AccessPermissionDeniedError,
    InvalidVisibilityConfigError,
    ResourceAccessTarget,
    ResourceType,
    VisibilityGrantInput,
    VisibilityScope,
)
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
DEFAULT_SEMANTIC_MIN_SCORE = 0.35
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
    """资源不存在或当前用户无权访问（按 404 防探测语义返回）。"""


class KnowledgePermissionDeniedError(PermissionError):
    """当前用户可读但无权创建、修改或删除目标知识库。"""


class KnowledgeInputError(ValueError):
    """知识库请求参数不符合约束。"""


class KnowledgeSearchUnavailableError(RuntimeError):
    """关键词与语义检索通道均不可用。"""


class KnowledgeUnsetType:
    """区分 PATCH 更新中未传递字段与显式传 None 的哨兵类型。"""

    __slots__ = ()


KNOWLEDGE_UNSET: Final[KnowledgeUnsetType] = KnowledgeUnsetType()


def _kb_access_target(base: KnowledgeBase) -> ResourceAccessTarget:
    """构造知识库的统一权限引擎判定目标。"""
    return ResourceAccessTarget(
        resource_type=ResourceType.KNOWLEDGE_BASE,
        resource_id=base.id,
        visibility_scope=base.visibility_scope,
        owner_id=base.owner_id,
        owner_department_id=base.owner_department_id,
    )


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
        access_policy: AccessPolicyService | None = None,
        profile_id: str = "bge-m3-default",
        semantic_min_score: float = DEFAULT_SEMANTIC_MIN_SCORE,
        id_factory: Callable[[str], str] = new_id,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        """保存持久化、分词、权限引擎及可替换的模型档案、ID 和时钟来源。"""
        if not math.isfinite(semantic_min_score) or not 0.0 <= semantic_min_score <= 1.0:
            raise ValueError("knowledge_semantic_min_score_invalid")
        self._repository = repository
        self._tokenizer = tokenizer
        self._embedding = embedding
        self._originals = originals
        self._counter = counter
        self._access_policy = access_policy
        self._profile_id = profile_id
        self._semantic_min_score = semantic_min_score
        # 检测仓储是否支持 min_score / visibility_grants 参数，兼容既有测试中的旧版 fake 仓储。
        self._semantic_supports_min_score = _accepts_keyword_argument(
            getattr(repository, "search_semantic", None),
            "min_score",
        )
        self._create_supports_grants = _accepts_keyword_argument(
            getattr(repository, "create_base", None),
            "visibility_grants",
        )
        self._id_factory = id_factory
        self._clock = clock

    async def _require_readable_base(self, actor_user_id: str, base_id: str) -> KnowledgeBase:
        """校验当前用户对知识库拥有读权限；无权或不存在一律抛 404 防止探测 ID。"""
        owner = _required(actor_user_id, "owner_id_required")
        if self._access_policy is None:
            base = await self._repository.get_base(owner, base_id)
            if base is None:
                raise KnowledgeNotFoundError("knowledge_base_not_found")
            return base
        get_by_id = getattr(self._repository, "get_base_by_id", None)
        base = (
            await get_by_id(base_id)
            if callable(get_by_id)
            else await self._repository.get_base(owner, base_id)
        )
        if base is None:
            raise KnowledgeNotFoundError("knowledge_base_not_found")
        if not await self._access_policy.can_read_target(owner, _kb_access_target(base)):
            raise KnowledgeNotFoundError("knowledge_base_not_found")
        return base

    async def _require_manageable_base(self, actor_user_id: str, base_id: str) -> KnowledgeBase:
        """校验知识库管理权限：不可读返回 404，可读但无管理权返回 403。"""
        owner = _required(actor_user_id, "owner_id_required")
        base = await self._require_readable_base(owner, base_id)
        if self._access_policy is not None:
            if not await self._access_policy.can_manage_resource(owner, _kb_access_target(base)):
                raise KnowledgePermissionDeniedError("knowledge_permission_denied")
        return base

    async def create_base(
        self,
        owner_id: str,
        name: str,
        description: str | None,
        *,
        visibility_scope: VisibilityScope = VisibilityScope.PERSONAL,
        owner_department_id: str | None = None,
        visibility_grants: Sequence[VisibilityGrantInput] | None = None,
    ) -> KnowledgeBase:
        """创建知识库并按统一权限引擎校验可见范围、归属部门与默认授权。"""
        owner = _required(owner_id, "owner_id_required")
        normalized_name = _clean_text(name, maximum=120, code="knowledge_base_name_invalid")
        normalized_description = (
            _clean_text(description, maximum=500, code="knowledge_base_description_invalid")
            if description is not None
            else None
        )
        resolved_scope = visibility_scope
        resolved_department_id = owner_department_id
        resolved_grants: tuple[VisibilityGrantInput, ...] = ()
        if self._access_policy is not None:
            try:
                (
                    resolved_scope,
                    resolved_department_id,
                    maybe_grants,
                ) = await self._access_policy.resolve_and_validate_visibility(
                    user_id=owner,
                    resource_type=ResourceType.KNOWLEDGE_BASE,
                    visibility_scope=visibility_scope,
                    owner_department_id=owner_department_id,
                    visibility_grants=visibility_grants,
                    for_create=True,
                )
            except AccessPermissionDeniedError as error:
                raise KnowledgePermissionDeniedError(error.code) from error
            except InvalidVisibilityConfigError as error:
                raise KnowledgeInputError(error.code) from error
            resolved_grants = maybe_grants or ()
        else:
            if visibility_scope is not VisibilityScope.PERSONAL or owner_department_id is not None:
                raise KnowledgeInputError("visibility_scope_requires_access_policy")

        at = _utc(self._clock())
        item = KnowledgeBase(
            self._id_factory("kb"),
            owner,
            normalized_name,
            normalized_description,
            at,
            at,
            visibility_scope=resolved_scope,
            owner_department_id=resolved_department_id,
        )
        if self._create_supports_grants:
            return await self._repository.create_base(item, visibility_grants=resolved_grants)
        return await self._repository.create_base(item)

    async def list_bases(
        self,
        owner_id: str,
        *,
        page: int,
        page_size: int,
        visibility_scope: VisibilityScope | None = None,
        owner_department_id: str | None = None,
    ) -> list[KnowledgeBase]:
        """分页读取当前用户全部可读知识库，支持按可见范围与归属部门筛选。"""
        if page < 1 or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise KnowledgeInputError("invalid_pagination")
        owner = _required(owner_id, "owner_id_required")
        if self._access_policy is None:
            return await self._repository.list_bases(
                owner,
                offset=(page - 1) * page_size,
                limit=page_size,
            )
        readable_ids = await self._access_policy.filter_readable_ids(
            owner,
            ResourceType.KNOWLEDGE_BASE,
        )
        if not readable_ids:
            return []
        return await self._repository.list_bases_by_ids(
            tuple(readable_ids),
            offset=(page - 1) * page_size,
            limit=page_size,
            visibility_scope=visibility_scope,
            owner_department_id=owner_department_id,
        )

    async def get_base(self, owner_id: str, base_id: str) -> KnowledgeBase:
        """读取当前用户可见知识库。"""
        return await self._require_readable_base(owner_id, base_id)

    async def rename_base(
        self,
        owner_id: str,
        base_id: str,
        name: str | None,
        description: str | None,
        *,
        visibility_scope: VisibilityScope | KnowledgeUnsetType = KNOWLEDGE_UNSET,
        owner_department_id: str | None | KnowledgeUnsetType = KNOWLEDGE_UNSET,
        visibility_grants: (
            Sequence[VisibilityGrantInput] | None | KnowledgeUnsetType
        ) = KNOWLEDGE_UNSET,
    ) -> KnowledgeBase:
        """修改知识库名称、说明或可见性配置（不可读返回 404，无管理权返回 403）。"""
        has_visibility_patch = (
            not isinstance(visibility_scope, KnowledgeUnsetType)
            or not isinstance(owner_department_id, KnowledgeUnsetType)
            or not isinstance(visibility_grants, KnowledgeUnsetType)
        )
        if name is None and description is None and not has_visibility_patch:
            raise KnowledgeInputError("knowledge_base_patch_empty")
        owner = _required(owner_id, "owner_id_required")
        current = await self._require_manageable_base(owner, base_id)
        next_name = (
            _clean_text(name, maximum=120, code="knowledge_base_name_invalid")
            if name is not None
            else current.name
        )
        next_description = (
            _clean_text(description, maximum=500, code="knowledge_base_description_invalid")
            if description is not None
            else current.description
        )
        if not has_visibility_patch:
            updated = await self._repository.rename_base(
                owner,
                base_id,
                next_name,
                next_description,
                _utc(self._clock()),
            )
            if updated is None:
                raise KnowledgeNotFoundError("knowledge_base_not_found")
            return updated

        if self._access_policy is None:
            raise KnowledgeInputError("visibility_scope_requires_access_policy")

        target_scope = (
            current.visibility_scope
            if isinstance(visibility_scope, KnowledgeUnsetType)
            else visibility_scope
        )
        if isinstance(owner_department_id, KnowledgeUnsetType):
            # 切换为 company/personal 且未显式传 owner_department_id 时自动清空归属部门。
            target_dept = (
                current.owner_department_id if target_scope is VisibilityScope.DEPARTMENT else None
            )
        else:
            target_dept = owner_department_id
        explicit_grants = (
            None if isinstance(visibility_grants, KnowledgeUnsetType) else visibility_grants
        )
        try:
            (
                resolved_scope,
                resolved_dept,
                resolved_grants,
            ) = await self._access_policy.resolve_and_validate_visibility(
                user_id=owner,
                resource_type=ResourceType.KNOWLEDGE_BASE,
                visibility_scope=target_scope,
                owner_department_id=target_dept,
                visibility_grants=explicit_grants,
                for_create=False,
                existing_target=_kb_access_target(current),
                preserve_existing_grants_when_none=isinstance(
                    visibility_grants,
                    KnowledgeUnsetType,
                ),
            )
        except AccessPermissionDeniedError as error:
            raise KnowledgePermissionDeniedError(error.code) from error
        except InvalidVisibilityConfigError as error:
            raise KnowledgeInputError(error.code) from error

        updated = await self._repository.update_base(
            base_id,
            name=next_name,
            description=next_description,
            visibility_scope=resolved_scope,
            owner_department_id=resolved_dept,
            visibility_grants=resolved_grants,
            at=_utc(self._clock()),
        )
        if updated is None:
            raise KnowledgeNotFoundError("knowledge_base_not_found")
        return updated

    async def delete_base(self, owner_id: str, base_id: str) -> None:
        """软删除知识库（不可读返回 404，无管理权返回 403），后续由清理任务回收内容。"""
        owner = _required(owner_id, "owner_id_required")
        if self._access_policy is not None:
            await self._require_manageable_base(owner, base_id)
        deleted = await self._repository.soft_delete_base(owner, base_id, _utc(self._clock()))
        if not deleted:
            raise KnowledgeNotFoundError("knowledge_base_not_found")

    async def create_text_document(
        self, owner_id: str, base_id: str, title: str, text: str, request_key: str
    ) -> tuple[Document, str]:
        """幂等登记文本导入任务，返回文档和版本身份。"""
        owner = _required(owner_id, "owner_id_required")
        await self._require_manageable_base(owner, base_id)
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
        await self._require_manageable_base(owner, base_id)
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
        await self._require_manageable_base(owner, base_id)
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
        await self._require_manageable_base(owner, base_id)
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
        try:
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
        except ValueError as error:
            # 将仓储冲突/并发更新等业务 ValueError 统一转为 KnowledgeInputError，
            # 与 replace_text_document 对齐，确保 API 层稳定映射为 409。
            raise KnowledgeInputError(str(error)) from error
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
        try:
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
        except ValueError as error:
            raise KnowledgeInputError(str(error)) from error

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
        if self._access_policy is not None:
            await self._require_readable_base(owner_id, base_id)
        document = await self._repository.get_document(owner_id, base_id, document_id)
        if document is None:
            raise KnowledgeNotFoundError("document_not_found")
        return document

    async def rename_document(
        self, owner_id: str, base_id: str, document_id: str, title: str
    ) -> Document:
        """修改文档标题，不改变活动或待处理版本。"""
        normalized = _clean_text(title, maximum=255, code="document_title_invalid")
        await self._require_manageable_base(owner_id, base_id)
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
        await self._require_manageable_base(owner_id, base_id)
        deleted = await self._repository.delete_document(owner_id, base_id, document_id)
        if deleted is None:
            raise KnowledgeNotFoundError("document_not_found")

    async def list_versions(
        self, owner_id: str, base_id: str, document_id: str
    ) -> list[DocumentVersion]:
        """按时间倒序读取可见文档的版本历史。"""
        if self._access_policy is not None:
            await self._require_readable_base(owner_id, base_id)
        versions = await self._repository.list_versions(owner_id, base_id, document_id)
        if versions is None:
            raise KnowledgeNotFoundError("document_not_found")
        return versions

    async def retry_version(
        self, owner_id: str, base_id: str, document_id: str, version_id: str
    ) -> DocumentVersion:
        """重试最新失败版本，复用其来源内容。"""
        if self._access_policy is not None:
            await self._require_manageable_base(owner_id, base_id)
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
        if self._access_policy is not None:
            await self._require_readable_base(owner_id, base_id)
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
        if self._access_policy is not None:
            await self._require_readable_base(owner_id, base_id)
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
        if self._access_policy is not None:
            readable_ids = await self._access_policy.filter_readable_ids(
                owner_id,
                ResourceType.KNOWLEDGE_BASE,
                base_ids,
            )
            if not set(base_ids) <= readable_ids:
                raise KnowledgeNotFoundError("knowledge_base_not_found")
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
            if self._semantic_supports_min_score:
                hits = await self._repository.search_semantic(
                    owner_id,
                    base_ids,
                    vector,
                    40,
                    self._profile_id,
                    min_score=self._semantic_min_score,
                )
            else:
                hits = await self._repository.search_semantic(
                    owner_id, base_ids, vector, 40, self._profile_id
                )
            profile_degraded = has_incompatible
            if has_incompatible and not hits:
                # 混合 profile 且启用相关性下限时，低频探测是否仍存在兼容切片：
                # 若仅因相似度低于 min_score 返回空命中，保留 semantic 通道并记录降级提示。
                if self._semantic_supports_min_score and self._semantic_min_score > 0.0:
                    compatible_probe = await self._repository.search_semantic(
                        owner_id,
                        base_ids,
                        vector,
                        1,
                        self._profile_id,
                        min_score=0.0,
                    )
                    if compatible_probe:
                        return ()
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


def _accepts_keyword_argument(func: object, name: str) -> bool:
    """判断可调用对象是否接受指定关键字参数，兼容旧测试替身签名。"""
    if not callable(func):
        return False
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    if name in parameters:
        return parameters[name].kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    return any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
