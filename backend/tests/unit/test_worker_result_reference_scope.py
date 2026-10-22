"""Worker result references are available only inside their execution state."""

from __future__ import annotations

import hashlib
from typing import cast
from types import SimpleNamespace

import pytest

from application.tools.service import ToolService
from application.orchestration.worker_tools import WorkerToolExecutor
from application.orchestration.worker_results import WorkerResultStore


@pytest.mark.asyncio
async def test_worker_result_reference_from_another_run_is_not_read() -> None:
    """确认另一个 Run 的结果引用不会触达文件存储。

    参数：无。
    返回：无。
    异常：断言失败时由 pytest 将测试标记为失败。
    """

    class Storage:
        """记录 Worker 结果读取请求的最小存储替身。"""

        def __init__(self) -> None:
            """初始化空的读取记录。

            参数：无。
            返回：无。
            异常：不主动抛出业务异常。
            """
            self.reads: list[str] = []

        async def read(self, storage_key: str) -> bytes:
            """读取指定存储键并记录访问。

            参数：storage_key 是待读取对象的存储键。
            返回：固定的 UTF-8 测试内容字节。
            异常：不主动抛出业务异常。
            """
            self.reads.append(storage_key)
            return b"private result"

    storage = Storage()
    executor = WorkerToolExecutor(
        cast(ToolService, object()),
        result_store=WorkerResultStore(storage),
    )
    result = await executor(
        tool_call={
            "name": "read_worker_result",
            "arguments": {"result_ref": "worker-result:another-run"},
        },
        execution={"run_id": "run-b", "_worker_result_refs": {}},
        tool_round=0,
        call_index=0,
    )

    assert result["status"] == "denied"
    assert result["error"]["code"] == "worker_result_reference_not_found"
    assert storage.reads == []


@pytest.mark.asyncio
async def test_worker_result_store_spills_large_utf8_output_and_reads_bounded_chunks():
    """大文本以稳定引用存储，读取接口按字符数量限制回传。"""

    class Storage:
        def __init__(self) -> None:
            self.values: dict[str, bytes] = {}

        async def save_stream(self, chunks):
            content = b"".join([chunk async for chunk in chunks])
            storage_key = "results/result-1"
            self.values[storage_key] = content
            return SimpleNamespace(
                storage_key=storage_key,
                sha256=hashlib.sha256(content).hexdigest(),
            )

        async def read(self, storage_key: str) -> bytes:
            return self.values[storage_key]

    store = WorkerResultStore(Storage(), inline_result_bytes=4, max_read_chars=1)
    stored = await store.store("中文A", identity="run-1:task-1:1:0:0")

    assert stored.output == "中"
    assert stored.result_ref is not None
    assert stored.storage_key == "results/result-1"
    result = await store.read(
        {
            "arguments": {
                "result_ref": stored.result_ref,
                "offset": 1,
                "max_chars": 8,
            }
        },
        {"_worker_result_refs": {stored.result_ref: stored.storage_key}},
    )

    assert result["output"] == "文"
    assert result["next_offset"] == 2
    assert result["has_more"] is True
