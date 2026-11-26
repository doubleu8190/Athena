"""HTTP authorization and error mapping for the knowledge API."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth_middleware import AuthPrecheckMiddleware
from api.knowledge import build_knowledge_router
from api.security import get_current_auth
from app.knowledge.service import KnowledgeInputError, KnowledgeNotFoundError, KnowledgeService
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.knowledge.model import (
    Document,
    DocumentState,
    DocumentVersion,
    KnowledgeBase,
    OriginalStorageUnavailableError,
    SearchResult,
)
from infrastructure.knowledge.parsers import LocalDocumentParser


class ApiService:
    def __init__(self) -> None:
        self.owner = "owner_a"
        self.base = KnowledgeBase(
            "kb_a",
            self.owner,
            "private",
            None,
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 1, tzinfo=UTC),
        )

    async def create_base(self, owner_id: str, name: str, description: str | None) -> KnowledgeBase:
        del owner_id, name, description
        return self.base

    async def list_bases(self, owner_id: str, *, page: int, page_size: int) -> list[KnowledgeBase]:
        del page, page_size
        return [self.base] if owner_id == self.owner else []

    async def get_base(self, owner_id: str, base_id: str) -> KnowledgeBase:
        if owner_id != self.owner or base_id != self.base.id:
            raise KnowledgeNotFoundError("knowledge_base_not_found")
        return self.base

    async def rename_base(
        self, owner_id: str, base_id: str, name: str | None, description: str | None
    ) -> KnowledgeBase:
        del name, description
        await self.get_base(owner_id, base_id)
        return self.base

    async def delete_base(self, owner_id: str, base_id: str) -> None:
        await self.get_base(owner_id, base_id)

    async def create_text_document(
        self, owner_id: str, base_id: str, title: str, text: str, key: str
    ) -> tuple[Document, str]:
        await self.get_base(owner_id, base_id)
        if key == "conflict":
            raise ValueError("idempotency_conflict")
        if title == "invalid":
            raise KnowledgeInputError("document_title_invalid")
        if title == "quota-doc":
            raise KnowledgeInputError("knowledge_document_quota_exceeded")
        now = datetime(2026, 1, 1, tzinfo=UTC)
        return Document("doc_a", owner_id, base_id, title, None, None, now, now), "version_a"

    async def create_file_document(
        self,
        owner_id: str,
        base_id: str,
        title: str,
        filename: str,
        content: bytes,
        key: str,
    ) -> tuple[Document, str]:
        del filename, key
        await self.get_base(owner_id, base_id)
        if not content:
            raise KnowledgeInputError("document_file_empty")
        if title == "quota-bytes":
            raise KnowledgeInputError("knowledge_storage_quota_exceeded")
        if title == "storage-down":
            raise OriginalStorageUnavailableError()
        now = datetime(2026, 1, 1, tzinfo=UTC)
        return Document(
            "doc_upload", owner_id, base_id, title, None, None, now, now
        ), "version_upload"

    async def replace_text_document(
        self, owner_id: str, base_id: str, document_id: str, text: str, key: str
    ) -> tuple[Document, str]:
        del text
        await self.get_base(owner_id, base_id)
        if key == "conflict":
            raise KnowledgeInputError("document_update_in_progress")
        if key == "idempotency-conflict":
            raise KnowledgeInputError("idempotency_conflict")
        if key == "quota-doc":
            raise KnowledgeInputError("knowledge_document_quota_exceeded")
        now = datetime(2026, 1, 1, tzinfo=UTC)
        return Document(
            document_id, owner_id, base_id, "replacement", None, None, now, now
        ), "version_b"

    async def replace_file_document(
        self,
        owner_id: str,
        base_id: str,
        document_id: str,
        filename: str,
        content: bytes,
        key: str,
    ) -> tuple[Document, str]:
        del filename
        await self.get_base(owner_id, base_id)
        if not content:
            raise KnowledgeInputError("document_file_empty")
        if key == "quota-bytes":
            raise KnowledgeInputError("knowledge_storage_quota_exceeded")
        if key == "storage-down":
            raise OriginalStorageUnavailableError()
        now = datetime(2026, 1, 1, tzinfo=UTC)
        return Document(
            document_id, owner_id, base_id, "replacement", None, None, now, now
        ), "version_file_b"

    async def list_documents(
        self, owner_id: str, base_id: str, *, page: int, page_size: int
    ) -> list[Document]:
        del page, page_size
        await self.get_base(owner_id, base_id)
        return []

    async def get_document(self, owner_id: str, base_id: str, document_id: str) -> Document:
        del document_id
        await self.get_base(owner_id, base_id)
        raise KnowledgeNotFoundError("document_not_found")

    async def list_versions(
        self, owner_id: str, base_id: str, document_id: str
    ) -> list[DocumentVersion]:
        await self.get_base(owner_id, base_id)
        if owner_id != self.owner:
            raise KnowledgeNotFoundError("document_not_found")
        return [
            DocumentVersion(
                "version_failed",
                document_id,
                DocumentState.FAILED,
                "parser_failed",
                (),
                0,
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 1, tzinfo=UTC),
            )
        ]

    async def retry_version(
        self, owner_id: str, base_id: str, document_id: str, version_id: str
    ) -> DocumentVersion:
        await self.get_base(owner_id, base_id)
        if version_id != "version_failed":
            raise KnowledgeInputError("version_superseded")
        return DocumentVersion(
            version_id,
            document_id,
            DocumentState.QUEUED,
            None,
            (),
            0,
            datetime(2026, 1, 1, tzinfo=UTC),
            None,
        )

    async def delete_document(self, owner_id: str, base_id: str, document_id: str) -> None:
        del document_id
        await self.get_base(owner_id, base_id)

    async def source(
        self, owner_id: str, base_id: str, document_id: str, version_id: str
    ) -> tuple[str, str, bytes]:
        del document_id
        await self.get_base(owner_id, base_id)
        if version_id == "missing":
            raise KnowledgeNotFoundError("source_not_found")
        if version_id == "storage-down":
            raise OriginalStorageUnavailableError()
        return "notes.txt", "text/plain", b"source"

    async def search(
        self, owner_id: str, base_ids: list[str], query: str, *, limit: int
    ) -> SearchResult:
        del limit
        await self.get_base(owner_id, base_ids[0])
        if query == "invalid":
            raise KnowledgeInputError("search_request_invalid")
        return SearchResult((), ())


def _context(owner_id: str) -> AuthContext:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return AuthContext(
        User(
            owner_id,
            f"{owner_id}@example.com",
            "unused",
            None,
            UserStatus.ACTIVE,
            now,
            now,
            UserRole.MEMBER,
        ),
        "session_test",
    )


def _app(authenticated: bool = True) -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthPrecheckMiddleware, fastapi_app=app)
    # 上传校验继续使用真实解析器，锁定端口注入后的校验行为。
    app.include_router(
        build_knowledge_router(cast(KnowledgeService, ApiService()), LocalDocumentParser())
    )
    if authenticated:
        app.dependency_overrides[get_current_auth] = lambda: _context("owner_a")
    return app


def test_knowledge_routes_require_authentication() -> None:
    response = TestClient(_app(authenticated=False)).get("/api/v1/knowledge-bases")
    assert response.status_code == 401
    assert response.json()["detail"] == "not_authenticated"


def test_cross_owner_base_is_mapped_to_not_found() -> None:
    app = _app()
    app.dependency_overrides[get_current_auth] = lambda: _context("owner_b")
    response = TestClient(app).get("/api/v1/knowledge-bases/kb_a")
    assert response.status_code == 404
    assert response.json()["detail"] == "knowledge_base_not_found"


def test_text_import_maps_idempotency_conflict_and_validation() -> None:
    client = TestClient(_app())
    conflict = client.post(
        "/api/v1/knowledge-bases/kb_a/documents/text",
        headers={"Idempotency-Key": "conflict"},
        json={"title": "title", "text": "body"},
    )
    invalid = client.post(
        "/api/v1/knowledge-bases/kb_a/documents/text",
        headers={"Idempotency-Key": "valid"},
        json={"title": "invalid", "text": "body"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "idempotency_conflict"
    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "document_title_invalid"


def test_version_retry_and_delete_errors_follow_http_contract() -> None:
    client = TestClient(_app())
    versions = client.get("/api/v1/knowledge-bases/kb_a/documents/doc_a/versions")
    retry = client.post(
        "/api/v1/knowledge-bases/kb_a/documents/doc_a/versions/version_failed/retry"
    )
    superseded = client.post("/api/v1/knowledge-bases/kb_a/documents/doc_a/versions/older/retry")
    deleted = client.delete("/api/v1/knowledge-bases/kb_a/documents/doc_a")

    assert versions.status_code == 200
    assert versions.json()[0]["error_code"] == "parser_failed"
    assert retry.status_code == 202
    assert retry.json()["state"] == "queued"
    assert superseded.status_code == 422
    assert superseded.json()["detail"] == "version_superseded"
    assert deleted.status_code == 204


def test_replacement_while_another_update_runs_maps_to_conflict() -> None:
    response = TestClient(_app()).post(
        "/api/v1/knowledge-bases/kb_a/documents/doc_a/versions/text",
        headers={"Idempotency-Key": "conflict"},
        json={"title": "replacement", "text": "new text"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "document_update_in_progress"


def test_upload_endpoint_requires_idempotency_and_returns_accepted_identity() -> None:
    client = TestClient(_app())
    accepted = client.post(
        "/api/v1/knowledge-bases/kb_a/documents/upload",
        headers={"Idempotency-Key": "upload-1"},
        data={"title": "notes"},
        files={"file": ("notes.txt", b"private source", "text/plain")},
    )
    missing_key = client.post(
        "/api/v1/knowledge-bases/kb_a/documents/upload",
        data={"title": "notes"},
        files={"file": ("notes.txt", b"private source", "text/plain")},
    )

    assert accepted.status_code == 202
    assert accepted.json()["document_id"] == "doc_upload"
    assert accepted.json()["version_id"] == "version_upload"
    assert missing_key.status_code == 422


def test_upload_validation_errors_map_to_415_422_and_413(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.knowledge import service as knowledge_service

    client = TestClient(_app())
    unsupported = client.post(
        "/api/v1/knowledge-bases/kb_a/documents/upload",
        headers={"Idempotency-Key": "unsupported"},
        data={"title": "unsupported"},
        files={"file": ("payload.exe", b"content", "application/octet-stream")},
    )
    legacy = client.post(
        "/api/v1/knowledge-bases/kb_a/documents/upload",
        headers={"Idempotency-Key": "legacy"},
        data={"title": "legacy"},
        files={"file": ("payload.doc", b"content", "application/msword")},
    )
    corrupt = client.post(
        "/api/v1/knowledge-bases/kb_a/documents/upload",
        headers={"Idempotency-Key": "corrupt"},
        data={"title": "corrupt"},
        files={"file": ("payload.pdf", b"not a pdf", "application/pdf")},
    )
    monkeypatch.setattr(knowledge_service, "MAX_FILE_BYTES", 4)
    oversized = client.post(
        "/api/v1/knowledge-bases/kb_a/documents/upload",
        headers={"Idempotency-Key": "oversized"},
        data={"title": "oversized"},
        files={"file": ("payload.txt", b"12345", "text/plain")},
    )

    assert unsupported.status_code == 415
    assert unsupported.json()["detail"] == "knowledge_file_type_unsupported"
    assert legacy.status_code == 415
    assert legacy.json()["detail"] == "knowledge_legacy_office_unsupported"
    assert corrupt.status_code == 422
    assert corrupt.json()["detail"] == "knowledge_file_signature_invalid"
    assert oversized.status_code == 413
    assert oversized.json()["detail"] == "document_file_too_large"


def test_document_and_storage_quota_errors_map_to_too_many_requests() -> None:
    client = TestClient(_app())
    text_create = client.post(
        "/api/v1/knowledge-bases/kb_a/documents/text",
        headers={"Idempotency-Key": "quota-create-doc"},
        json={"title": "quota-doc", "text": "body"},
    )
    file_create = client.post(
        "/api/v1/knowledge-bases/kb_a/documents/upload",
        headers={"Idempotency-Key": "quota-create-bytes"},
        data={"title": "quota-bytes"},
        files={"file": ("quota.txt", b"file body", "text/plain")},
    )
    text_replace = client.post(
        "/api/v1/knowledge-bases/kb_a/documents/doc_a/versions/text",
        headers={"Idempotency-Key": "quota-doc"},
        json={"title": "ignored for replacement", "text": "replacement"},
    )
    file_replace = client.post(
        "/api/v1/knowledge-bases/kb_a/documents/doc_a/versions/upload",
        headers={"Idempotency-Key": "quota-bytes"},
        files={"file": ("quota.txt", b"replacement body", "text/plain")},
    )

    assert text_create.status_code == 429
    assert text_create.json()["detail"] == "knowledge_document_quota_exceeded"
    assert file_create.status_code == 429
    assert file_create.json()["detail"] == "knowledge_storage_quota_exceeded"
    assert text_replace.status_code == 429
    assert text_replace.json()["detail"] == "knowledge_document_quota_exceeded"
    assert file_replace.status_code == 429
    assert file_replace.json()["detail"] == "knowledge_storage_quota_exceeded"


def test_write_route_rejects_cross_origin_request() -> None:
    response = TestClient(_app()).post(
        "/api/v1/knowledge-bases",
        headers={"Origin": "https://attacker.example"},
        json={"name": "private"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "origin_not_allowed"


def test_storage_failures_map_to_safe_service_unavailable() -> None:
    client = TestClient(_app())
    create = client.post(
        "/api/v1/knowledge-bases/kb_a/documents/upload",
        headers={"Idempotency-Key": "storage-create"},
        data={"title": "storage-down"},
        files={"file": ("notes.txt", b"source", "text/plain")},
    )
    replace = client.post(
        "/api/v1/knowledge-bases/kb_a/documents/doc_a/versions/upload",
        headers={"Idempotency-Key": "storage-down"},
        files={"file": ("notes.txt", b"source", "text/plain")},
    )
    download = client.get(
        "/api/v1/knowledge-bases/kb_a/documents/doc_a/versions/storage-down/source"
    )

    for response in (create, replace, download):
        assert response.status_code == 503
        assert response.json()["detail"] == "knowledge_storage_unavailable"


def test_missing_source_maps_to_not_found_not_storage_unavailable() -> None:
    response = TestClient(_app()).get(
        "/api/v1/knowledge-bases/kb_a/documents/doc_a/versions/missing/source"
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "source_not_found"


@pytest.mark.parametrize(
    ("stage", "conflict_code"),
    [
        ("preflight", "document_update_in_progress"),
        ("preflight", "idempotency_conflict"),
        ("replace", "document_update_in_progress"),
        ("replace", "idempotency_conflict"),
    ],
)
def test_replace_file_document_conflicts_return_http_409(
    stage: str,
    conflict_code: str,
) -> None:
    """文件替换在预检或提交阶段触发并发更新/幂等冲突时，API 必须返回 409 而非 422。"""
    from knowledge_fakes import MemoryOriginalStore

    from domain.knowledge.ports import KnowledgeRepositoryPort

    now = datetime(2026, 1, 1, tzinfo=UTC)

    class _SimpleTokenizer:
        def tokenize(self, text: str) -> tuple[str, ...]:
            return tuple(text.split())

    class _ConflictRepo:
        async def get_base(self, owner_id: str, base_id: str) -> KnowledgeBase | None:
            return KnowledgeBase(base_id, owner_id, "Base", None, now, now)

        async def get_document(
            self, owner_id: str, base_id: str, document_id: str
        ) -> Document | None:
            return Document(document_id, owner_id, base_id, "Doc", None, None, now, now)

        async def preflight_file_upload(self, **kwargs: object) -> tuple[Document, str] | None:
            del kwargs
            if stage == "preflight":
                raise ValueError(conflict_code)
            return None

        async def replace_file_document(self, **kwargs: object) -> tuple[Document, str]:
            del kwargs
            raise ValueError(conflict_code)

    service = KnowledgeService(
        cast(KnowledgeRepositoryPort, _ConflictRepo()),
        _SimpleTokenizer(),
        originals=MemoryOriginalStore(),
    )
    app = FastAPI()
    app.include_router(build_knowledge_router(service, LocalDocumentParser()))
    app.dependency_overrides[get_current_auth] = lambda: _context("owner_a")

    response = TestClient(app).post(
        "/api/v1/knowledge-bases/kb_a/documents/doc_a/versions/upload",
        headers={"Idempotency-Key": f"replace-{stage}-{conflict_code}"},
        files={"file": ("notes.txt", b"replacement body", "text/plain")},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == conflict_code


def test_list_documents_endpoint_serializes_latest_version_summary_and_null(
    tmp_path: Path,
) -> None:
    """文档列表接口应返回 latest_version 摘要，覆盖多版本取最新、状态字段与无版本 null。"""
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from adapter.auth.model import UserModel
    from adapter.knowledge.model import (
        KnowledgeBaseModel,
        KnowledgeDocumentModel,
        KnowledgeVersionModel,
    )
    from adapter.knowledge.repository import KnowledgeRepository
    from adapter.models import Base
    from domain.knowledge.ports import KnowledgeRepositoryPort

    class _SimpleTokenizer:
        def tokenize(self, text: str) -> tuple[str, ...]:
            return tuple(text.split())

    db_path = tmp_path / "knowledge_list_latest_version.db"
    # 使用独立文件库与 NullPool，避免 asyncio.run 与 TestClient 后台事件循环共享连接。
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", poolclass=NullPool)
    now = datetime(2026, 2, 1, 10, 0, tzinfo=UTC)

    async def seed() -> async_sessionmaker[AsyncSession]:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            session.add(
                UserModel(
                    id="owner_a",
                    email="owner_a@example.com",
                    password_hash="unused",
                    display_name=None,
                    status="active",
                    role="member",
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            session.add(
                KnowledgeBaseModel(
                    id="kb_a",
                    owner_id="owner_a",
                    name="API 列表测试知识库",
                    description=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            session.add_all(
                [
                    KnowledgeDocumentModel(
                        id="doc_ready",
                        owner_id="owner_a",
                        knowledge_base_id="kb_a",
                        title="已就绪文档",
                        active_version_id="ver_ready_new",
                        pending_version_id=None,
                        created_at=now,
                        updated_at=datetime(2026, 2, 1, 10, 4, tzinfo=UTC),
                    ),
                    KnowledgeDocumentModel(
                        id="doc_failed",
                        owner_id="owner_a",
                        knowledge_base_id="kb_a",
                        title="替换失败文档",
                        active_version_id="ver_failed_old_ready",
                        pending_version_id=None,
                        created_at=now,
                        updated_at=datetime(2026, 2, 1, 10, 3, tzinfo=UTC),
                    ),
                    KnowledgeDocumentModel(
                        id="doc_processing",
                        owner_id="owner_a",
                        knowledge_base_id="kb_a",
                        title="处理中文档",
                        active_version_id=None,
                        pending_version_id="ver_processing",
                        created_at=now,
                        updated_at=datetime(2026, 2, 1, 10, 2, tzinfo=UTC),
                    ),
                    KnowledgeDocumentModel(
                        id="doc_empty",
                        owner_id="owner_a",
                        knowledge_base_id="kb_a",
                        title="尚无版本文档",
                        active_version_id=None,
                        pending_version_id=None,
                        created_at=now,
                        updated_at=datetime(2026, 2, 1, 10, 1, tzinfo=UTC),
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    KnowledgeVersionModel(
                        id="ver_ready_old",
                        document_id="doc_ready",
                        owner_id="owner_a",
                        source_id=None,
                        source_text="old ready body",
                        content_hash="a" * 64,
                        profile_id="bge-m3-default",
                        processing_config={},
                        state="ready",
                        error_code=None,
                        warnings=[],
                        chunk_count=2,
                        created_at=datetime(2026, 2, 1, 10, 0, 1, tzinfo=UTC),
                        completed_at=datetime(2026, 2, 1, 10, 0, 2, tzinfo=UTC),
                    ),
                    KnowledgeVersionModel(
                        id="ver_ready_new",
                        document_id="doc_ready",
                        owner_id="owner_a",
                        source_id=None,
                        source_text="new ready body",
                        content_hash="b" * 64,
                        profile_id="bge-m3-default",
                        processing_config={},
                        state="ready",
                        error_code=None,
                        warnings=["ocr_low_confidence", "trimmed_table"],
                        chunk_count=7,
                        created_at=datetime(2026, 2, 1, 10, 0, 5, tzinfo=UTC),
                        completed_at=datetime(2026, 2, 1, 10, 0, 9, tzinfo=UTC),
                    ),
                    KnowledgeVersionModel(
                        id="ver_failed_old_ready",
                        document_id="doc_failed",
                        owner_id="owner_a",
                        source_id=None,
                        source_text="active ready body",
                        content_hash="c" * 64,
                        profile_id="bge-m3-default",
                        processing_config={},
                        state="ready",
                        error_code=None,
                        warnings=[],
                        chunk_count=3,
                        created_at=datetime(2026, 2, 1, 10, 0, 1, tzinfo=UTC),
                        completed_at=datetime(2026, 2, 1, 10, 0, 2, tzinfo=UTC),
                    ),
                    KnowledgeVersionModel(
                        id="ver_failed_latest",
                        document_id="doc_failed",
                        owner_id="owner_a",
                        source_id=None,
                        source_text="broken update body",
                        content_hash="d" * 64,
                        profile_id="bge-m3-default",
                        processing_config={},
                        state="failed",
                        error_code="parser_failed",
                        warnings=["partial_extract"],
                        chunk_count=0,
                        created_at=datetime(2026, 2, 1, 10, 0, 10, tzinfo=UTC),
                        completed_at=datetime(2026, 2, 1, 10, 0, 12, tzinfo=UTC),
                    ),
                    KnowledgeVersionModel(
                        id="ver_processing",
                        document_id="doc_processing",
                        owner_id="owner_a",
                        source_id=None,
                        source_text="processing body",
                        content_hash="e" * 64,
                        profile_id="bge-m3-default",
                        processing_config={},
                        state="processing",
                        error_code=None,
                        warnings=[],
                        chunk_count=0,
                        created_at=datetime(2026, 2, 1, 10, 0, 3, tzinfo=UTC),
                        completed_at=None,
                    ),
                ]
            )
        return factory

    try:
        factory = asyncio.run(seed())
        repository = KnowledgeRepository(factory)
        service = KnowledgeService(cast(KnowledgeRepositoryPort, repository), _SimpleTokenizer())
        app = FastAPI()
        app.include_router(build_knowledge_router(service, LocalDocumentParser()))
        app.dependency_overrides[get_current_auth] = lambda: _context("owner_a")
        client = TestClient(app)

        list_resp = client.get("/api/v1/knowledge-bases/kb_a/documents")
        assert list_resp.status_code == 200
        items = list_resp.json()["items"]
        assert [item["id"] for item in items] == [
            "doc_ready",
            "doc_failed",
            "doc_processing",
            "doc_empty",
        ]
        by_id = {item["id"]: item for item in items}

        ready_latest = by_id["doc_ready"]["latest_version"]
        assert ready_latest is not None
        assert ready_latest["id"] == "ver_ready_new"
        assert ready_latest["state"] == "ready"
        assert ready_latest["warnings"] == ["ocr_low_confidence", "trimmed_table"]
        assert ready_latest["chunk_count"] == 7
        assert ready_latest["error_code"] is None
        assert ready_latest["completed_at"] is not None

        failed_latest = by_id["doc_failed"]["latest_version"]
        assert failed_latest is not None
        assert failed_latest["id"] == "ver_failed_latest"
        assert failed_latest["state"] == "failed"
        assert failed_latest["error_code"] == "parser_failed"
        assert failed_latest["warnings"] == ["partial_extract"]
        assert failed_latest["chunk_count"] == 0

        processing_latest = by_id["doc_processing"]["latest_version"]
        assert processing_latest is not None
        assert processing_latest["id"] == "ver_processing"
        assert processing_latest["state"] == "processing"
        assert processing_latest["completed_at"] is None
        assert processing_latest["chunk_count"] == 0

        assert by_id["doc_empty"]["latest_version"] is None

        # 单文档查询与重命名接口也应返回一致的 latest_version 结构。
        detail_resp = client.get("/api/v1/knowledge-bases/kb_a/documents/doc_failed")
        assert detail_resp.status_code == 200
        assert detail_resp.json()["latest_version"] == failed_latest

        rename_resp = client.patch(
            "/api/v1/knowledge-bases/kb_a/documents/doc_failed",
            json={"title": "重命名后的失败文档"},
        )
        assert rename_resp.status_code == 200
        assert rename_resp.json()["title"] == "重命名后的失败文档"
        assert rename_resp.json()["latest_version"] == failed_latest

        rename_empty_resp = client.patch(
            "/api/v1/knowledge-bases/kb_a/documents/doc_empty",
            json={"title": "重命名后的空文档"},
        )
        assert rename_empty_resp.status_code == 200
        assert rename_empty_resp.json()["title"] == "重命名后的空文档"
        assert rename_empty_resp.json()["latest_version"] is None
    finally:
        asyncio.run(engine.dispose())
