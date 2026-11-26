"""Knowledge service contract tests for ownership, idempotency, and search scope."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

import pytest
from access_support import make_test_access_policy
from knowledge_fakes import MemoryOriginalStore

from adapter.access.repository import SqlAlchemyAccessControlRepository
from app.access import AccessPolicyService
from app.knowledge.service import KnowledgeInputError, KnowledgeNotFoundError, KnowledgeService
from domain.access import (
    GrantTargetType,
    ResourceAccessTarget,
    ResourceType,
    UserAccessSnapshot,
    VisibilityGrant,
    VisibilityGrantInput,
    VisibilityScope,
)
from domain.knowledge.model import (
    Document,
    KnowledgeBase,
    OriginalStorageUnavailableError,
    SearchHit,
    SourceReference,
)
from domain.knowledge.ports import KnowledgeRepositoryPort


class MemoryKnowledgeRepository:
    def __init__(self) -> None:
        self.bases: dict[str, KnowledgeBase] = {}
        self.requests: dict[tuple[str, str], tuple[str, str, str]] = {}
        self.content_hashes: dict[tuple[str, str], str] = {}
        self.scope_calls: list[tuple[str, list[str]]] = []
        self.keyword_calls: list[tuple[str, list[str], tuple[str, ...], int]] = []
        self.semantic_calls: list[tuple[str, list[str], list[float], int, str | None]] = []
        self.deleted_documents: list[str] = []
        self.source_reference: SourceReference | None = None
        self.update_base_calls: list[dict[str, object]] = []

    async def create_base(
        self,
        item: KnowledgeBase,
        *,
        visibility_grants: Sequence[VisibilityGrantInput] = (),
    ) -> KnowledgeBase:
        del visibility_grants
        self.bases[item.id] = item
        return item

    async def get_base_by_id(self, base_id: str) -> KnowledgeBase | None:
        item = self.bases.get(base_id)
        return item if item and item.deleted_time is None else None

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
        del user_id, snapshot
        items = [
            item
            for item in self.bases.values()
            if item.deleted_time is None
            and (visibility_scope is None or item.visibility_scope is visibility_scope)
            and (owner_department_id is None or item.owner_department_id == owner_department_id)
        ]
        return items[offset : offset + limit]

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
        self.update_base_calls.append(
            {
                "base_id": base_id,
                "name": name,
                "description": description,
                "visibility_scope": visibility_scope,
                "owner_department_id": owner_department_id,
                "visibility_grants": visibility_grants,
            }
        )
        item = await self.get_base_by_id(base_id)
        if item is None:
            return None
        next_scope = item.visibility_scope if visibility_scope is None else visibility_scope
        next_dept = item.owner_department_id if visibility_scope is None else owner_department_id
        if visibility_scope is not None and visibility_grants is not None:
            next_grants = tuple(
                VisibilityGrant(
                    id=f"rvg_{idx}",
                    resource_type=ResourceType.KNOWLEDGE_BASE,
                    resource_id=item.id,
                    target_type=grant.target_type,
                    target_id=grant.target_id,
                    include_subdepartments=grant.include_subdepartments,
                    created_at=at,
                )
                for idx, grant in enumerate(visibility_grants, start=1)
            )
        else:
            next_grants = item.visibility_grants
        updated = KnowledgeBase(
            item.id,
            item.owner_id,
            name,
            description,
            item.created_at,
            at,
            visibility_scope=next_scope,
            owner_department_id=next_dept,
            visibility_grants=next_grants,
        )
        self.bases[base_id] = updated
        return updated

    async def soft_delete_base(self, owner_id: str, base_id: str, at: datetime) -> bool:
        del owner_id
        item = await self.get_base_by_id(base_id)
        if item is None:
            return False
        self.bases[base_id] = KnowledgeBase(
            item.id,
            item.owner_id,
            item.name,
            item.description,
            item.created_at,
            item.updated_at,
            at,
        )
        return True

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
        del text, job_id, profile_id
        key = (owner_id, request_key)
        existing = self.requests.get(key)
        if existing:
            prior_hash, prior_document_id, prior_version_id = existing
            if prior_hash != request_hash:
                raise ValueError("idempotency_conflict")
            return Document(
                prior_document_id,
                owner_id,
                base_id,
                title,
                None,
                prior_version_id,
                at,
                at,
            ), prior_version_id
        self.requests[key] = (request_hash, document_id, version_id)
        self.content_hashes[key] = content_hash

        return Document(
            document_id,
            owner_id,
            base_id,
            title,
            None,
            version_id,
            at,
            at,
        ), version_id

    async def list_documents(
        self, owner_id: str, base_id: str, *, offset: int, limit: int
    ) -> list[Document]:
        del owner_id, base_id, offset, limit
        return []

    async def get_document(self, owner_id: str, base_id: str, document_id: str) -> Document | None:
        del owner_id, base_id, document_id
        return None

    async def search_keyword(
        self, owner_id: str, base_ids: list[str], tokens: tuple[str, ...], limit: int
    ) -> tuple[SearchHit, ...]:
        self.keyword_calls.append((owner_id, list(base_ids), tokens, limit))
        return ()

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
        del min_score
        self.semantic_calls.append((owner_id, list(base_ids), vector, limit, profile_id))
        return ()

    async def has_incompatible_profile(
        self, owner_id: str, base_ids: list[str], profile_id: str
    ) -> bool:
        del owner_id, base_ids, profile_id
        return False

    async def filter_active_hits(
        self, owner_id: str, hits: tuple[SearchHit, ...]
    ) -> tuple[SearchHit, ...]:
        del owner_id
        return hits

    async def delete_document(
        self, owner_id: str, base_id: str, document_id: str
    ) -> list[str] | None:
        del owner_id
        if await self.get_base_by_id(base_id) is None:
            return None
        self.deleted_documents.append(document_id)
        return []

    async def get_source(
        self, owner_id: str, base_id: str, document_id: str, version_id: str
    ) -> SourceReference | None:
        del owner_id, document_id, version_id
        if await self.get_base_by_id(base_id) is None:
            return None
        return self.source_reference


class Tokenizer:
    def tokenize(self, text: str) -> tuple[str, ...]:
        return tuple(text.split())


class Embedding:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.25] * 1024 for _ in texts]


def _access_policy_for(repository: MemoryKnowledgeRepository) -> AccessPolicyService:
    """为内存知识库仓储构建动态反映个人/部门归属的真实 AccessPolicyService。"""

    def _targets(resource_type: ResourceType) -> tuple[ResourceAccessTarget, ...]:
        if resource_type is not ResourceType.KNOWLEDGE_BASE:
            return ()
        return tuple(
            ResourceAccessTarget(
                resource_type=ResourceType.KNOWLEDGE_BASE,
                resource_id=base.id,
                visibility_scope=base.visibility_scope,
                owner_id=base.owner_id,
                owner_department_id=base.owner_department_id,
            )
            for base in repository.bases.values()
            if base.deleted_time is None
        )

    return make_test_access_policy(targets_provider=_targets, permissive_unknown=False)


def _service(repository: MemoryKnowledgeRepository) -> KnowledgeService:
    return KnowledgeService(
        cast(KnowledgeRepositoryPort, repository),
        Tokenizer(),
        profile_id="bge-m3-default",
        id_factory=lambda prefix: f"{prefix}_fixed",
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        access_policy=_access_policy_for(repository),
    )


def test_owner_scope_hides_another_users_base() -> None:
    repository = MemoryKnowledgeRepository()
    service = _service(repository)
    asyncio.run(service.create_base("owner_a", "private", None))

    with pytest.raises(KnowledgeNotFoundError):
        asyncio.run(service.get_base("owner_b", "kb_fixed"))
    with pytest.raises(KnowledgeNotFoundError):
        asyncio.run(service.create_text_document("owner_b", "kb_fixed", "title", "body", "key"))


def test_rename_base_metadata_only_preserves_visibility_and_grants() -> None:
    """仅改名称/说明调用 rename_base 时不传可见性参数，且原样保留部门归属与授权清单。"""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    grant = VisibilityGrant(
        id="rvg_fin_1",
        resource_type=ResourceType.KNOWLEDGE_BASE,
        resource_id="kb_dept_fin",
        target_type=GrantTargetType.DEPARTMENT,
        target_id="dept_fin",
        include_subdepartments=True,
        created_at=now,
    )
    repository = MemoryKnowledgeRepository()
    repository.bases["kb_dept_fin"] = KnowledgeBase(
        id="kb_dept_fin",
        owner_id="admin_fin",
        name="原财务制度",
        description="原说明",
        created_at=now,
        updated_at=now,
        visibility_scope=VisibilityScope.DEPARTMENT,
        owner_department_id="dept_fin",
        visibility_grants=(grant,),
    )
    service = _service(repository)

    renamed = asyncio.run(service.rename_base("admin_fin", "kb_dept_fin", "新财务制度", "新说明"))

    assert renamed.name == "新财务制度"
    assert renamed.description == "新说明"
    assert renamed.visibility_scope is VisibilityScope.DEPARTMENT
    assert renamed.owner_department_id == "dept_fin"
    assert renamed.visibility_grants == (grant,)
    assert repository.update_base_calls == [
        {
            "base_id": "kb_dept_fin",
            "name": "新财务制度",
            "description": "新说明",
            "visibility_scope": None,
            "owner_department_id": None,
            "visibility_grants": None,
        }
    ]


def test_text_import_is_idempotent_and_conflicts_when_payload_changes() -> None:
    repository = MemoryKnowledgeRepository()
    service = _service(repository)
    asyncio.run(service.create_base("owner_a", "private", None))
    first, version_1 = asyncio.run(
        service.create_text_document("owner_a", "kb_fixed", "title", "body", "request-1")
    )
    replay, version_2 = asyncio.run(
        service.create_text_document("owner_a", "kb_fixed", "title", "body", "request-1")
    )

    assert replay.id == first.id
    assert version_2 == version_1
    request_hash = repository.requests[("owner_a", "request-1")][0]
    assert request_hash != hashlib.sha256(b"body").hexdigest()
    assert (
        repository.content_hashes[("owner_a", "request-1")] == hashlib.sha256(b"body").hexdigest()
    )
    with pytest.raises(ValueError, match="idempotency_conflict"):
        asyncio.run(
            service.create_text_document("owner_a", "kb_fixed", "title", "changed", "request-1")
        )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    asyncio.run(
        repository.create_base(KnowledgeBase("kb_other", "owner_a", "other", None, now, now))
    )
    with pytest.raises(ValueError, match="idempotency_conflict"):
        asyncio.run(
            service.create_text_document("owner_a", "kb_other", "title", "body", "request-1")
        )
    with pytest.raises(ValueError, match="idempotency_conflict"):
        asyncio.run(
            service.create_text_document(
                "owner_a", "kb_fixed", "different title", "body", "request-1"
            )
        )


def test_search_passes_owner_scope_and_same_tokens_and_vector_to_repository() -> None:
    repository = MemoryKnowledgeRepository()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    asyncio.run(repository.create_base(KnowledgeBase("kb_a", "owner_a", "a", None, now, now)))
    service = KnowledgeService(
        cast(KnowledgeRepositoryPort, repository),
        Tokenizer(),
        Embedding(),
        profile_id="bge-m3-default",
        access_policy=_access_policy_for(repository),
    )
    result = asyncio.run(service.search("owner_a", ["kb_a"], "PostgreSQL 中文", limit=7))

    assert repository.keyword_calls == [("owner_a", ["kb_a"], ("PostgreSQL", "中文"), 40)]
    owner, bases, vector, limit, profile_id = repository.semantic_calls[0]
    assert (owner, bases, limit, profile_id) == ("owner_a", ["kb_a"], 40, "bge-m3-default")
    assert vector == [0.25] * 1024
    assert result.channels == ("keyword", "semantic")
    with pytest.raises(KnowledgeNotFoundError, match="knowledge_base_not_found"):
        asyncio.run(service.search("owner_b", ["kb_a"], "PostgreSQL 中文", limit=7))


@pytest.mark.parametrize("query,limit", [("", 8), ("good", 0), ("good", 21)])
def test_search_rejects_invalid_query_or_limit(query: str, limit: int) -> None:
    with pytest.raises(KnowledgeInputError):
        asyncio.run(
            _service(MemoryKnowledgeRepository()).search("owner_a", ["kb_a"], query, limit=limit)
        )


def test_document_delete_never_accesses_original_storage() -> None:
    class FailingStore(MemoryOriginalStore):
        async def delete(self, key: str) -> None:
            raise AssertionError(f"delete must not touch storage: {key}")

    async def scenario() -> None:
        repository = MemoryKnowledgeRepository()
        service = KnowledgeService(
            cast(KnowledgeRepositoryPort, repository),
            Tokenizer(),
            profile_id="bge-m3-default",
            originals=FailingStore(),
            access_policy=_access_policy_for(repository),
        )
        base = await service.create_base("owner_a", "private", None)
        await service.delete_document("owner_a", base.id, "doc_a")
        assert repository.deleted_documents == ["doc_a"]

    asyncio.run(scenario())


def test_source_authorization_failure_does_not_read_original_storage() -> None:
    class FailingStore(MemoryOriginalStore):
        async def get(self, key: str) -> bytes:
            raise AssertionError(f"get must not touch storage: {key}")

    async def scenario() -> None:
        repository = MemoryKnowledgeRepository()
        service = KnowledgeService(
            cast(KnowledgeRepositoryPort, repository),
            Tokenizer(),
            profile_id="bge-m3-default",
            originals=FailingStore(),
            access_policy=_access_policy_for(repository),
        )
        base = await service.create_base("owner_a", "private", None)
        digest = hashlib.sha256(b"private").hexdigest()
        repository.source_reference = SourceReference(
            "private.txt",
            "text/plain",
            f"{'a' * 64}/ksrc_private/{digest[:2]}/{digest}",
            None,
        )

        with pytest.raises(KnowledgeNotFoundError, match="knowledge_base_not_found"):
            await service.source("owner_b", base.id, "doc_a", "ver_a")

    asyncio.run(scenario())


def test_text_source_does_not_require_an_original_store() -> None:
    async def scenario() -> None:
        repository = MemoryKnowledgeRepository()
        service = KnowledgeService(
            cast(KnowledgeRepositoryPort, repository),
            Tokenizer(),
            profile_id="bge-m3-default",
            access_policy=_access_policy_for(repository),
        )
        base = await service.create_base("owner_a", "private", None)
        repository.source_reference = SourceReference("notes.txt", "text/plain", None, "body")

        filename, media_type, content = await service.source("owner_a", base.id, "doc_a", "ver_a")
        assert (filename, media_type, content) == ("notes.txt", "text/plain", b"body")

    asyncio.run(scenario())


def test_missing_source_object_maps_to_source_not_found() -> None:
    async def scenario() -> None:
        repository = MemoryKnowledgeRepository()
        store = MemoryOriginalStore()
        service = KnowledgeService(
            cast(KnowledgeRepositoryPort, repository),
            Tokenizer(),
            profile_id="bge-m3-default",
            originals=store,
            access_policy=_access_policy_for(repository),
        )
        base = await service.create_base("owner_a", "private", None)
        digest = hashlib.sha256(b"missing").hexdigest()
        repository.source_reference = SourceReference(
            "missing.txt",
            "text/plain",
            f"{'a' * 64}/ksrc_missing/{digest[:2]}/{digest}",
            None,
        )

        with pytest.raises(KnowledgeNotFoundError, match="source_not_found"):
            await service.source("owner_a", base.id, "doc_a", "ver_a")
        assert store.get_calls

    asyncio.run(scenario())


def test_storage_unavailability_during_download_is_not_hidden_as_not_found() -> None:
    class UnavailableStore(MemoryOriginalStore):
        async def get(self, key: str) -> bytes:
            del key
            raise OriginalStorageUnavailableError()

    async def scenario() -> None:
        repository = MemoryKnowledgeRepository()
        service = KnowledgeService(
            cast(KnowledgeRepositoryPort, repository),
            Tokenizer(),
            profile_id="bge-m3-default",
            originals=UnavailableStore(),
            access_policy=_access_policy_for(repository),
        )
        base = await service.create_base("owner_a", "private", None)
        digest = hashlib.sha256(b"unavailable").hexdigest()
        repository.source_reference = SourceReference(
            "unavailable.txt",
            "text/plain",
            f"{'a' * 64}/ksrc_unavailable/{digest[:2]}/{digest}",
            None,
        )
        with pytest.raises(OriginalStorageUnavailableError):
            await service.source("owner_a", base.id, "doc_a", "ver_a")

    asyncio.run(scenario())


def test_list_documents_batch_loads_latest_version_in_single_query_and_skips_when_empty() -> None:
    """仓储 list_documents 应在列表为空时跳过版本查询，非空时用单次批量 SQL 装配最新版本。"""
    from typing import Any

    from pg_unit_harness import create_pg_unit_db
    from sqlalchemy import event

    from adapter.auth.model import UserModel
    from adapter.knowledge.model import (
        KnowledgeBaseModel,
        KnowledgeDocumentModel,
        KnowledgeVersionModel,
    )
    from adapter.knowledge.repository import KnowledgeRepository
    from domain.knowledge.model import DocumentState

    async def scenario() -> None:
        engine, factory = await create_pg_unit_db()
        executed_sql: list[str] = []

        def _record_sql(
            conn: Any,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            del conn, cursor, parameters, context, executemany
            executed_sql.append(statement)

        try:
            repository = KnowledgeRepository(factory)
            service = KnowledgeService(
                cast(KnowledgeRepositoryPort, repository),
                Tokenizer(),
                profile_id="bge-m3-default",
                access_policy=AccessPolicyService(SqlAlchemyAccessControlRepository(factory)),
            )
            now = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)
            same_time = datetime(2026, 3, 1, 8, 5, tzinfo=UTC)

            async with factory() as session, session.begin():
                session.add_all(
                    [
                        UserModel(
                            id="owner_a",
                            email="owner_a@service.test",
                            password_hash="unused",
                            display_name=None,
                            status="active",
                            role="member",
                            created_at=now,
                            updated_at=now,
                        ),
                        UserModel(
                            id="owner_b",
                            email="owner_b@service.test",
                            password_hash="unused",
                            display_name=None,
                            status="active",
                            role="member",
                            created_at=now,
                            updated_at=now,
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        KnowledgeBaseModel(
                            id="kb_empty",
                            owner_id="owner_a",
                            name="空知识库",
                            description=None,
                            created_at=now,
                            updated_at=now,
                        ),
                        KnowledgeBaseModel(
                            id="kb_populated",
                            owner_id="owner_a",
                            name="多版本知识库",
                            description=None,
                            created_at=now,
                            updated_at=now,
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        KnowledgeDocumentModel(
                            id="doc_multi",
                            owner_id="owner_a",
                            knowledge_base_id="kb_populated",
                            title="多版本文档",
                            active_version_id="ver_001",
                            pending_version_id=None,
                            created_at=now,
                            updated_at=datetime(2026, 3, 1, 8, 10, tzinfo=UTC),
                        ),
                        KnowledgeDocumentModel(
                            id="doc_tiebreak",
                            owner_id="owner_a",
                            knowledge_base_id="kb_populated",
                            title="同时间戳按 ID 倒序文档",
                            active_version_id=None,
                            pending_version_id="ver_z",
                            created_at=now,
                            updated_at=datetime(2026, 3, 1, 8, 9, tzinfo=UTC),
                        ),
                        KnowledgeDocumentModel(
                            id="doc_no_version",
                            owner_id="owner_a",
                            knowledge_base_id="kb_populated",
                            title="无版本文档",
                            active_version_id=None,
                            pending_version_id=None,
                            created_at=now,
                            updated_at=datetime(2026, 3, 1, 8, 8, tzinfo=UTC),
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        KnowledgeVersionModel(
                            id="ver_001",
                            document_id="doc_multi",
                            owner_id="owner_a",
                            source_id=None,
                            source_text="v1",
                            content_hash="1" * 64,
                            profile_id="bge-m3-default",
                            state="ready",
                            error_code=None,
                            warnings=["w1"],
                            chunk_count=4,
                            created_at=datetime(2026, 3, 1, 8, 1, tzinfo=UTC),
                            completed_at=datetime(2026, 3, 1, 8, 2, tzinfo=UTC),
                        ),
                        KnowledgeVersionModel(
                            id="ver_002",
                            document_id="doc_multi",
                            owner_id="owner_a",
                            source_id=None,
                            source_text="v2",
                            content_hash="2" * 64,
                            profile_id="bge-m3-default",
                            state="failed",
                            error_code="embedding_timeout",
                            warnings=["partial"],
                            chunk_count=0,
                            created_at=datetime(2026, 3, 1, 8, 3, tzinfo=UTC),
                            completed_at=datetime(2026, 3, 1, 8, 4, tzinfo=UTC),
                        ),
                        # 相同 created_at 下必须按 id DESC 选取 ver_z，与 list_versions 保持一致。
                        KnowledgeVersionModel(
                            id="ver_a",
                            document_id="doc_tiebreak",
                            owner_id="owner_a",
                            source_id=None,
                            source_text="tie-a",
                            content_hash="3" * 64,
                            profile_id="bge-m3-default",
                            state="ready",
                            error_code=None,
                            warnings=[],
                            chunk_count=5,
                            created_at=same_time,
                            completed_at=same_time,
                        ),
                        KnowledgeVersionModel(
                            id="ver_z",
                            document_id="doc_tiebreak",
                            owner_id="owner_a",
                            source_id=None,
                            source_text="tie-z",
                            content_hash="4" * 64,
                            profile_id="bge-m3-default",
                            state="processing",
                            error_code=None,
                            warnings=[],
                            chunk_count=0,
                            created_at=same_time,
                            completed_at=None,
                        ),
                    ]
                )

            event.listen(engine.sync_engine, "before_cursor_execute", _record_sql)
            try:
                # 1) 空列表分支：严禁对 knowledge_document_versions 发多余查询。
                executed_sql.clear()
                empty_docs = await repository.list_documents(
                    "owner_a", "kb_empty", offset=0, limit=20
                )
                assert empty_docs == []
                assert len(executed_sql) == 1
                assert all("knowledge_document_versions" not in sql for sql in executed_sql)

                # 2) 多文档分支：仓储层严格只发 2 条 SQL（1 条查文档、1 条单次批量查最新版本）。
                executed_sql.clear()
                docs = await repository.list_documents(
                    "owner_a", "kb_populated", offset=0, limit=20
                )
                assert len(executed_sql) == 2
                version_queries = [
                    sql for sql in executed_sql if "knowledge_document_versions" in sql
                ]
                assert len(version_queries) == 1
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", _record_sql)

            by_id = {doc.id: doc for doc in docs}
            assert set(by_id) == {"doc_multi", "doc_tiebreak", "doc_no_version"}
            assert by_id["doc_multi"].latest_version is not None
            assert by_id["doc_multi"].latest_version.id == "ver_002"
            assert by_id["doc_multi"].latest_version.state is DocumentState.FAILED
            assert by_id["doc_multi"].latest_version.error_code == "embedding_timeout"
            assert by_id["doc_multi"].latest_version.warnings == ("partial",)

            assert by_id["doc_tiebreak"].latest_version is not None
            assert by_id["doc_tiebreak"].latest_version.id == "ver_z"
            assert by_id["doc_tiebreak"].latest_version.state is DocumentState.PROCESSING

            assert by_id["doc_no_version"].latest_version is None

            # 校验 latest_version 与 list_versions 首位 100% 一致，且通过 service 透传一致。
            service_docs = await service.list_documents(
                "owner_a", "kb_populated", page=1, page_size=20
            )
            assert service_docs == docs
            for doc in docs:
                versions = await service.list_versions("owner_a", "kb_populated", doc.id)
                expected_latest = versions[0] if versions else None
                assert doc.latest_version == expected_latest
                fetched = await service.get_document("owner_a", "kb_populated", doc.id)
                assert fetched.latest_version == expected_latest
                renamed = await service.rename_document(
                    "owner_a", "kb_populated", doc.id, f"{doc.title}（已重命名）"
                )
                assert renamed.title == f"{doc.title}（已重命名）"
                assert renamed.latest_version == expected_latest
        finally:
            await engine.dispose()

    asyncio.run(scenario())
