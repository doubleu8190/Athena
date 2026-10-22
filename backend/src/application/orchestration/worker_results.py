"""Worker 工具结果的大文本存储与受限读取。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
import hashlib
from typing import Any

from domain.files import FileStoragePort


@dataclass(frozen=True, slots=True)
class StoredWorkerResult:
    """工具结果的内联文本、执行内引用和私有存储键。"""

    output: str
    result_ref: str | None = None
    storage_key: str | None = None


class WorkerResultStore:
    """将大结果移出图状态，并限制 Agent 的结果读取范围。"""

    def __init__(
        self,
        storage: FileStoragePort,
        *,
        inline_result_bytes: int = 8192,
        max_read_chars: int = 16000,
    ) -> None:
        if inline_result_bytes < 1 or max_read_chars < 1:
            raise ValueError("worker result limits must be positive")
        self._storage = storage
        self._inline_result_bytes = inline_result_bytes
        self._max_read_chars = max_read_chars

    async def store(
        self, output: str, *, identity: str
    ) -> StoredWorkerResult:
        """按字节上限保存大文本，并返回当前执行可见的稳定引用。"""
        encoded = output.encode("utf-8")
        if len(encoded) <= self._inline_result_bytes:
            return StoredWorkerResult(output=output)
        blob = await self._storage.save_stream(_byte_chunks(encoded))
        reference_identity = f"{identity}:{blob.sha256}"
        result_ref = "worker-result:" + hashlib.sha256(
            reference_identity.encode("utf-8")
        ).hexdigest()[:32]
        # 字节阈值只控制存储大小；解码截断后可能略低于该字节数。
        preview = encoded[: self._inline_result_bytes].decode(
            "utf-8", errors="ignore"
        )
        return StoredWorkerResult(
            output=preview,
            result_ref=result_ref,
            storage_key=blob.storage_key,
        )

    async def read(
        self,
        tool_call: Mapping[str, Any],
        execution: Mapping[str, Any],
    ) -> dict[str, Any]:
        """读取当前 Worker 执行中登记的结果引用的一段文本。"""
        arguments = tool_call.get("arguments", tool_call.get("args", {}))
        if not isinstance(arguments, Mapping):
            return _denied("invalid_tool_arguments")
        result_ref = str(arguments.get("result_ref", ""))
        references = execution.get("_worker_result_refs", {})
        storage_key = (
            references.get(result_ref)
            if isinstance(references, Mapping)
            else None
        )
        if not result_ref or not isinstance(storage_key, str):
            return _denied("worker_result_reference_not_found")
        try:
            offset = max(0, int(arguments.get("offset", 0)))
            limit = min(
                self._max_read_chars,
                max(1, int(arguments.get("max_chars", self._max_read_chars))),
            )
        except (TypeError, ValueError):
            return _denied("invalid_tool_arguments")
        try:
            content = (await self._storage.read(storage_key)).decode(
                "utf-8", errors="replace"
            )
        except FileNotFoundError:
            return _denied("worker_result_reference_expired")
        end = min(len(content), offset + limit)
        return {
            "name": "read_worker_result",
            "result_ref": result_ref,
            "status": "success",
            "output": content[offset:end],
            "offset": offset,
            "next_offset": end,
            "has_more": end < len(content),
        }


def _denied(code: str) -> dict[str, Any]:
    """构造 Worker 可识别的结果读取拒绝回执。"""
    return {
        "name": "read_worker_result",
        "status": "denied",
        "error": {"code": code},
    }


async def _byte_chunks(value: bytes) -> AsyncIterator[bytes]:
    """把完整的内存文本接入异步文件存储接口。"""
    yield value
__all__ = ["StoredWorkerResult", "WorkerResultStore"]
