"""HTTP authorization and error mapping for the knowledge API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
            UserRole.USER,
        ),
        "session_test",
    )


def _app(authenticated: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(build_knowledge_router(cast(KnowledgeService, ApiService())))
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
