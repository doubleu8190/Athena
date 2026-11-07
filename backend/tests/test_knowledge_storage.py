"""Safety and limits for local knowledge source storage."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from infrastructure.knowledge.storage import LocalOriginalStore
from infrastructure.knowledge.tokenizer import JiebaChineseTokenizer


async def _chunks(*items: bytes) -> AsyncIterator[bytes]:
    for item in items:
        yield item


def test_stream_storage_is_bounded_atomic_and_content_addressed(tmp_path: Path) -> None:
    store = LocalOriginalStore(tmp_path)
    content = b"knowledge source"
    digest = hashlib.sha256(content).hexdigest()
    key = f"{'a' * 64}/{digest[:2]}/{digest}"
    stored_hash, size = asyncio.run(
        store.put_stream(key, _chunks(b"knowledge ", b"source"), max_bytes=100)
    )
    assert stored_hash == digest and size == len(content)
    assert asyncio.run(store.get(key)) == content
    asyncio.run(store.put_stream(key, _chunks(content), max_bytes=100))
    with pytest.raises(ValueError, match="knowledge_file_too_large"):
        asyncio.run(store.put_stream(f"{'a' * 64}/00/{'0' * 64}", _chunks(content), max_bytes=2))
    asyncio.run(store.delete(key))
    with pytest.raises(FileNotFoundError):
        asyncio.run(store.get(key))


@pytest.mark.parametrize("key", ["../secret", "aa/../secret", "AA/" + "0" * 64])
def test_storage_rejects_unsafe_keys(tmp_path: Path, key: str) -> None:
    store = LocalOriginalStore(tmp_path)
    with pytest.raises(ValueError, match="knowledge_storage_key_invalid"):
        asyncio.run(store.get(key))


def test_storage_rejects_symlink(tmp_path: Path) -> None:
    store = LocalOriginalStore(tmp_path)
    outside = tmp_path.parent / "outside-source"
    outside.write_bytes(b"private")
    key = f"{'a' * 64}/aa/{'0' * 64}"
    link = tmp_path / key
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="knowledge_storage_path_invalid"):
        asyncio.run(store.get(key))


def test_chinese_search_tokenizer_preserves_identifiers() -> None:
    tokens = JiebaChineseTokenizer().tokenize("Athena 知识检索 PostgreSQL 17.2 ERR-404")
    assert "知识" in tokens
    assert "检索" in tokens
    assert "postgresql" in tokens
    assert "17.2" in tokens
    assert "err-404" in tokens
