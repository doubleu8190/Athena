"""Grace period behavior for immutable knowledge source cleanup."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from infrastructure.knowledge.storage import LocalOriginalStore


async def _stream(content: bytes) -> AsyncIterator[bytes]:
    yield content


def test_orphan_sweep_preserves_references_and_recent_files(tmp_path: Path) -> None:
    store = LocalOriginalStore(tmp_path)
    content = b"old source"
    digest = hashlib.sha256(content).hexdigest()
    owner = "a" * 64
    referenced_key = f"{owner}/ksrc_keep/{digest[:2]}/{digest}"
    orphan_key = f"{owner}/ksrc_drop/{digest[:2]}/{digest}"
    for key in (referenced_key, orphan_key):
        asyncio.run(store.put_stream(key, _stream(content), max_bytes=100))
    old = time.time() - 90_000
    os.utime(tmp_path / orphan_key, (old, old))
    recent_temp = tmp_path / owner / digest[:2] / ".upload-recent"
    recent_temp.parent.mkdir(parents=True, exist_ok=True)
    recent_temp.write_bytes(b"partial")

    removed = asyncio.run(store.sweep_orphans({referenced_key}, older_than_seconds=86_400))

    assert removed == 1
    assert (tmp_path / referenced_key).exists()
    assert not (tmp_path / orphan_key).exists()
    assert recent_temp.exists()
    with pytest.raises(ValueError, match="knowledge_orphan_grace_period_invalid"):
        asyncio.run(store.sweep_orphans(set(), older_than_seconds=60))
