"""Worker result references are available only inside their execution state."""

from __future__ import annotations

from typing import cast

import pytest

from application.tools.service import ToolService
from application.orchestration.worker_tools import WorkerToolExecutor


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
    executor = WorkerToolExecutor(cast(ToolService, object()), storage=storage)
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
