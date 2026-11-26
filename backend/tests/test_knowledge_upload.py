"""文件上传、只读 preflight 预检、幂等与仓储事务测试。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from knowledge_fakes import MemoryOriginalStore
from pg_unit_harness import pg_unit_db

from adapter.auth.model import UserModel
from adapter.knowledge.model import KnowledgeDocumentModel
from adapter.knowledge.repository import KnowledgeRepository
from app.knowledge.service import KnowledgeService
from domain.access import VisibilityGrantInput
from domain.knowledge.model import Document, KnowledgeBase, OriginalStorageUnavailableError
from domain.knowledge.ports import KnowledgeRepositoryPort


class _UploadRepository:
    def __init__(self) -> None:
        self.bases: dict[str, KnowledgeBase] = {}
        self.requests: dict[tuple[str, str, str], tuple[str, Document, str]] = {}
        self.documents: dict[str, Document] = {}
        self.content_hashes: list[str] = []
        self.referenced: set[str] = set()
        self.storage_keys: list[str] = []
        self.preflight_calls = 0
        self.quota_limit_bytes = 1024 * 1024
        self.committed_sizes: dict[str, int] = {}
        self.fail_next = False
        self.fail_after_commit = False

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
        self.preflight_calls += 1
        existing = self.requests.get((owner_id, operation, request_key))
        if existing is not None:
            saved_hash, document, version_id = existing
            if saved_hash != request_hash:
                raise ValueError("idempotency_conflict")
            return document, version_id
        if base_id not in self.bases:
            raise LookupError("knowledge_base_not_found")
        if operation == "replace":
            if document_id is None or document_id not in self.documents:
                raise LookupError("document_not_found")
            if self.documents[document_id].pending_version_id is not None:
                raise ValueError("document_update_in_progress")
        if sum(self.committed_sizes.values()) + size_bytes > self.quota_limit_bytes:
            raise ValueError("knowledge_storage_quota_exceeded")
        return None

    async def create_base(
        self,
        item: KnowledgeBase,
        *,
        visibility_grants: Sequence[VisibilityGrantInput] = (),
    ) -> KnowledgeBase:
        del visibility_grants
        self.bases[item.id] = item
        return item

    async def get_base(self, owner_id: str, base_id: str) -> KnowledgeBase | None:
        base = self.bases.get(base_id)
        return base if base and base.owner_id == owner_id else None

    async def get_base_by_id(self, base_id: str) -> KnowledgeBase | None:
        return self.bases.get(base_id)

    async def find_referenced_storage_keys(self, storage_keys: Sequence[str]) -> set[str]:
        return {key for key in storage_keys if key in self.referenced}

    async def create_file_document(self, **values: Any) -> tuple[Document, str]:
        return await self._save_file("create", **values)

    async def replace_file_document(self, **values: Any) -> tuple[Document, str]:
        return await self._save_file("replace", **values)

    async def _save_file(self, operation: str, **values: Any) -> tuple[Document, str]:
        req = (values["owner_id"], operation, values["request_key"])
        previous = self.requests.get(req)
        if previous:
            request_hash, document, version_id = previous
            if request_hash != values["request_hash"]:
                raise ValueError("idempotency_conflict")
            return document, version_id
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated_database_failure")
        if operation == "replace":
            current = self.documents[values["document_id"]]
            document = replace(
                current,
                pending_version_id=values["version_id"],
                updated_at=values["at"],
            )
        else:
            document = Document(
                values["document_id"],
                values["owner_id"],
                values["base_id"],
                values["title"],
                None,
                values["version_id"],
                values["at"],
                values["at"],
            )
        self.documents[document.id] = document
        self.requests[req] = (values["request_hash"], document, values["version_id"])
        self.content_hashes.append(values["sha256"])
        self.referenced.add(values["storage_key"])
        self.storage_keys.append(values["storage_key"])
        self.committed_sizes[values["storage_key"]] = values["size_bytes"]
        if self.fail_after_commit:
            self.fail_after_commit = False
            raise RuntimeError("simulated_commit_result_unknown")
        return document, values["version_id"]

    async def is_storage_key_referenced(self, key: str) -> bool:
        return key in self.referenced

    async def get_document(self, owner_id: str, base_id: str, document_id: str) -> Document | None:
        document = self.documents.get(document_id)
        if (
            document is not None
            and document.owner_id == owner_id
            and document.knowledge_base_id == base_id
        ):
            return document
        return None

    async def rename_document(
        self, owner_id: str, base_id: str, document_id: str, title: str, at: datetime
    ) -> Document | None:
        del at
        current = await self.get_document(owner_id, base_id, document_id)
        if current is None:
            return None
        renamed = replace(current, title=title)
        self.documents[document_id] = renamed
        for request, (request_hash, document, version_id) in list(self.requests.items()):
            if document.id == document_id:
                self.requests[request] = (request_hash, renamed, version_id)
        return renamed


class _Tokenizer:
    def tokenize(self, text: str) -> tuple[str, ...]:
        return tuple(text.split())


class _FailAfterStoreOriginalStore(MemoryOriginalStore):
    async def put(self, key: str, content: bytes) -> None:
        await super().put(key, content)
        raise OriginalStorageUnavailableError()


def _make(
    originals: MemoryOriginalStore | None = None,
) -> tuple[KnowledgeService, _UploadRepository]:
    repository = _UploadRepository()
    now = datetime(2026, 11, 7, tzinfo=UTC)
    identities = iter(range(1, 100))
    service = KnowledgeService(
        cast(KnowledgeRepositoryPort, repository),
        _Tokenizer(),
        originals=originals or MemoryOriginalStore(),
        id_factory=lambda kind: f"{kind}_{next(identities)}",
        clock=lambda: now,
    )
    return service, repository


def test_owner_scoped_uploads_same_bytes_to_distinct_storage_keys() -> None:
    async def scenario() -> None:
        service, repository = _make()
        alice_base = await service.create_base("alice", "private", None)
        bob_base = await service.create_base("bob", "private", None)
        alice, _ = await service.create_file_document(
            "alice", alice_base.id, "notes", "notes.txt", b"same bytes", "alice-1"
        )
        bob, _ = await service.create_file_document(
            "bob", bob_base.id, "notes", "notes.txt", b"same bytes", "bob-1"
        )
        assert alice.owner_id != bob.owner_id
        assert len(repository.referenced) == 2
        owner_scopes = {key.split("/")[0] for key in repository.storage_keys}
        assert owner_scopes == {
            hashlib.sha256(b"alice").hexdigest(),
            hashlib.sha256(b"bob").hexdigest(),
        }
        originals = cast(MemoryOriginalStore, service._originals)
        assert originals is not None
        assert originals.keys() == set(repository.storage_keys)

    asyncio.run(scenario())


def test_upload_idempotency_replay_and_conflict_skip_original_write() -> None:
    """幂等重放与哈希冲突均在只读 preflight 拦截，不向对象存储写入多余孤儿。"""

    async def scenario() -> None:
        service, repository = _make()
        base = await service.create_base("alice", "private", None)
        first, version_a = await service.create_file_document(
            "alice", base.id, "notes", "notes.txt", b"same bytes", "request-1"
        )
        replay, version_b = await service.create_file_document(
            "alice", base.id, "notes", "notes.txt", b"same bytes", "request-1"
        )
        assert (first.id, version_a) == (replay.id, version_b)
        request_hash = repository.requests[("alice", "create", "request-1")][0]
        assert request_hash != hashlib.sha256(b"same bytes").hexdigest()
        assert repository.content_hashes == [hashlib.sha256(b"same bytes").hexdigest()]
        assert len(repository.referenced) == 1
        assert len(repository.storage_keys) == 1
        originals = cast(MemoryOriginalStore, service._originals)
        assert len(originals.put_calls) == 1

        with pytest.raises(ValueError, match="idempotency_conflict"):
            await service.create_file_document(
                "alice", base.id, "notes", "notes.txt", b"different", "request-1"
            )
        with pytest.raises(ValueError, match="idempotency_conflict"):
            await service.create_file_document(
                "alice", base.id, "renamed", "renamed.txt", b"same bytes", "request-1"
            )
        assert len(repository.referenced) == 1
        assert len(originals.put_calls) == 1

    asyncio.run(scenario())


def test_quota_rejection_happens_before_original_write() -> None:
    """超配额请求在无锁只读 preflight 阶段拦截，不向对象存储写入字节。"""

    async def scenario() -> None:
        service, repository = _make()
        base = await service.create_base("alice", "private", None)
        repository.quota_limit_bytes = 2

        with pytest.raises(ValueError, match="knowledge_storage_quota_exceeded"):
            await service.create_file_document(
                "alice", base.id, "notes", "notes.txt", b"too large", "request-1"
            )

        originals = cast(MemoryOriginalStore, service._originals)
        assert originals.keys() == set()
        assert originals.put_calls == []

    asyncio.run(scenario())


def test_finalize_failure_leaves_unreferenced_orphan_for_worker_sweep() -> None:
    """数据库提交失败时不会登记来源引用，残留对象由 24h 宽限期孤儿扫描安全回收。"""

    async def scenario() -> None:
        service, repository = _make()
        base = await service.create_base("alice", "private", None)
        repository.fail_next = True
        with pytest.raises(RuntimeError, match="simulated_database_failure"):
            await service.create_file_document(
                "alice", base.id, "notes", "notes.txt", b"orphan", "request-1"
            )
        digest = hashlib.sha256(b"orphan").hexdigest()
        originals = cast(MemoryOriginalStore, service._originals)
        orphan = next(key for key in originals.keys() if key.endswith(digest))
        assert originals.content(orphan) == b"orphan"
        assert not await repository.is_storage_key_referenced(orphan)

    asyncio.run(scenario())


def test_store_error_does_not_register_document_or_source() -> None:
    """对象存储返回故障时立即失败，不写入数据库来源与文档记录。"""

    async def scenario() -> None:
        storage = _FailAfterStoreOriginalStore()
        service, repository = _make(storage)
        base = await service.create_base("alice", "private", None)

        with pytest.raises(OriginalStorageUnavailableError):
            await service.create_file_document(
                "alice", base.id, "notes", "notes.txt", b"expected", "request-1"
            )

        assert repository.documents == {}
        assert repository.referenced == set()

    asyncio.run(scenario())


def test_uncertain_commit_keeps_referenced_original() -> None:
    async def scenario() -> None:
        service, repository = _make()
        base = await service.create_base("alice", "private", None)
        repository.fail_after_commit = True
        with pytest.raises(RuntimeError, match="simulated_commit_result_unknown"):
            await service.create_file_document(
                "alice", base.id, "notes", "notes.txt", b"committed", "request-1"
            )

        assert len(repository.storage_keys) == 1
        key = repository.storage_keys[0]
        originals = cast(MemoryOriginalStore, service._originals)
        assert originals is not None
        assert originals.content(key) == b"committed"
        assert key in repository.referenced
        assert sum(repository.committed_sizes.values()) == len(b"committed")
        assert key in originals.keys()

    asyncio.run(scenario())


def test_file_replacement_uses_same_owner_scope_and_persists_bytes() -> None:
    async def scenario() -> None:
        service, repository = _make()
        base = await service.create_base("alice", "private", None)
        original, _ = await service.create_file_document(
            "alice", base.id, "notes", "notes.txt", b"old", "request-1"
        )
        # 模拟原版本已完成处理（清空 pending_version_id），允许后续替换。
        repository.documents[original.id] = replace(
            repository.documents[original.id],
            active_version_id=original.pending_version_id,
            pending_version_id=None,
        )
        result, version = await service.replace_file_document(
            "alice",
            base.id,
            original.id,
            "new.txt",
            b"replacement",
            "request-2",
        )
        assert result.owner_id == "alice" and version.startswith("kver_")
        hashes = {key.rsplit("/", 1)[-1] for key in repository.referenced}
        assert hashes == {
            hashlib.sha256(b"old").hexdigest(),
            hashlib.sha256(b"replacement").hexdigest(),
        }

    asyncio.run(scenario())


def test_replace_request_replays_after_document_rename() -> None:
    async def scenario() -> None:
        service, repository = _make()
        base = await service.create_base("alice", "private", None)
        original, _ = await service.create_file_document(
            "alice", base.id, "notes", "notes.txt", b"original", "create-request"
        )
        repository.documents[original.id] = replace(
            repository.documents[original.id],
            active_version_id=original.pending_version_id,
            pending_version_id=None,
        )
        replacement, version = await service.replace_file_document(
            "alice", base.id, original.id, "next.txt", b"replacement", "replace-request"
        )
        renamed = await service.rename_document("alice", base.id, original.id, "renamed")
        replay, replay_version = await service.replace_file_document(
            "alice", base.id, original.id, "next.txt", b"replacement", "replace-request"
        )

        assert renamed.title == "renamed"
        assert replay.id == replacement.id == original.id
        assert (replay.id, replay_version) == (replacement.id, version)
        assert len(repository.storage_keys) == 2

    asyncio.run(scenario())


def test_repository_preflight_and_unified_save_document_version_postgresql_contracts() -> None:
    """用真实 PostgreSQL 验证无锁 preflight 与合并后的 4 个创建/替换文档事务方法契约。"""

    async def scenario() -> None:
        async with pg_unit_db() as (_engine, sessions):
            now = datetime(2026, 11, 20, tzinfo=UTC)
            async with sessions() as session, session.begin():
                session.add(
                    UserModel(
                        id="owner_1",
                        email="owner1@example.test",
                        password_hash="unused",
                        display_name=None,
                        status="active",
                        role="member",
                        created_at=now,
                        updated_at=now,
                    )
                )
            repo = KnowledgeRepository(sessions)
            await repo.create_base(KnowledgeBase("kb_1", "owner_1", "Base", None, now, now))

            # 1. preflight 新文件通过并返回 None
            assert (
                await repo.preflight_file_upload(
                    owner_id="owner_1",
                    operation="create",
                    request_key="req-file-1",
                    request_hash="hash-file-1",
                    base_id="kb_1",
                    document_id=None,
                    size_bytes=16,
                )
                is None
            )

            # 2. 创建文件文档后，同 key preflight 返回重放元组，不同 hash 抛冲突
            doc, ver = await repo.create_file_document(
                owner_id="owner_1",
                base_id="kb_1",
                title="File Doc",
                filename="a.txt",
                media_type="text/plain",
                storage_key="owner/ksrc_1/aa/aaa",
                size_bytes=16,
                sha256="a" * 64,
                request_key="req-file-1",
                request_hash="hash-file-1",
                document_id="kdoc_1",
                version_id="kver_1",
                source_id="ksrc_1",
                job_id="kjob_1",
                profile_id="prof_1",
                at=now,
            )
            assert (doc.id, ver) == ("kdoc_1", "kver_1")
            preflight_replay = await repo.preflight_file_upload(
                owner_id="owner_1",
                operation="create",
                request_key="req-file-1",
                request_hash="hash-file-1",
                base_id="kb_1",
                document_id=None,
                size_bytes=16,
            )
            assert preflight_replay is not None
            assert (
                preflight_replay[0].id,
                preflight_replay[0].pending_version_id,
                preflight_replay[1],
            ) == (doc.id, ver, ver)
            with pytest.raises(ValueError, match="idempotency_conflict"):
                await repo.preflight_file_upload(
                    owner_id="owner_1",
                    operation="create",
                    request_key="req-file-1",
                    request_hash="different-hash",
                    base_id="kb_1",
                    document_id=None,
                    size_bytes=16,
                )

            # 3. 存在 pending_version_id 时，replace preflight 拦截返回 document_update_in_progress
            with pytest.raises(ValueError, match="document_update_in_progress"):
                await repo.preflight_file_upload(
                    owner_id="owner_1",
                    operation="replace",
                    request_key="req-rep-1",
                    request_hash="hash-rep-1",
                    base_id="kb_1",
                    document_id="kdoc_1",
                    size_bytes=12,
                )

            # 清空 pending_version_id 后允许替换文件与文本版本，并校验幂等重放
            async with sessions() as session, session.begin():
                row = await session.get(KnowledgeDocumentModel, "kdoc_1")
                assert row is not None
                row.active_version_id = "kver_1"
                row.pending_version_id = None

            replaced, rep_ver = await repo.replace_file_document(
                owner_id="owner_1",
                base_id="kb_1",
                document_id="kdoc_1",
                filename="b.txt",
                media_type="text/plain",
                storage_key="owner/ksrc_2/bb/bbb",
                size_bytes=12,
                sha256="b" * 64,
                request_key="req-rep-1",
                request_hash="hash-rep-1",
                version_id="kver_2",
                source_id="ksrc_2",
                job_id="kjob_2",
                profile_id="prof_1",
                at=now,
            )
            assert replaced.pending_version_id == "kver_2"
            assert rep_ver == "kver_2"
            replace_replay = await repo.replace_file_document(
                owner_id="owner_1",
                base_id="kb_1",
                document_id="kdoc_1",
                filename="b.txt",
                media_type="text/plain",
                storage_key="owner/ksrc_ignored/cc/ccc",
                size_bytes=12,
                sha256="b" * 64,
                request_key="req-rep-1",
                request_hash="hash-rep-1",
                version_id="kver_ignored",
                source_id="ksrc_ignored",
                job_id="kjob_ignored",
                profile_id="prof_1",
                at=now,
            )
            assert (
                replace_replay[0].id,
                replace_replay[0].pending_version_id,
                replace_replay[1],
            ) == (
                replaced.id,
                "kver_2",
                "kver_2",
            )

            # 4. 文本创建与替换同样支持幂等与冲突校验
            text_doc, text_ver = await repo.create_text_document(
                owner_id="owner_1",
                base_id="kb_1",
                title="Text Doc",
                text="hello text",
                request_key="req-text-1",
                request_hash="hash-text-1",
                content_hash="c" * 64,
                document_id="kdoc_text",
                version_id="kver_text_1",
                job_id="kjob_text_1",
                profile_id="prof_1",
                at=now,
            )
            assert (text_doc.id, text_ver) == ("kdoc_text", "kver_text_1")
            async with sessions() as session, session.begin():
                row = await session.get(KnowledgeDocumentModel, "kdoc_text")
                assert row is not None
                row.active_version_id = "kver_text_1"
                row.pending_version_id = None
            rep_text_doc, rep_text_ver = await repo.replace_text_document(
                owner_id="owner_1",
                base_id="kb_1",
                document_id="kdoc_text",
                text="updated text",
                request_key="req-text-rep-1",
                request_hash="hash-text-rep-1",
                content_hash="d" * 64,
                version_id="kver_text_2",
                job_id="kjob_text_2",
                profile_id="prof_1",
                at=now,
            )
            assert rep_text_doc.pending_version_id == "kver_text_2"
            assert rep_text_ver == "kver_text_2"

    asyncio.run(scenario())
