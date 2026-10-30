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
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `storage`（FileStoragePort）：负责保存、读取或删除文件内容的存储依赖；约束：无额外运行时限制。
        - `inline_result_bytes`（int）：结果以内联方式保存的最大字节数；约束：可省略，默认值为 `8192`；须通过方法内校验；失败条件见下方异常说明。
        - `max_read_chars`（int）：单次读取结果允许返回的最大字符数；约束：可省略，默认值为 `16000`；须通过方法内校验；失败条件见下方异常说明。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - ValueError：校验条件 `inline_result_bytes < 1 or max_read_chars < 1` 成立时（异常说明：worker result limits must be positive）。
        - 未列出的依赖异常按当前实现传播。
        """
        if inline_result_bytes < 1 or max_read_chars < 1:
            raise ValueError("worker result limits must be positive")
        self._storage = storage
        self._inline_result_bytes = inline_result_bytes
        self._max_read_chars = max_read_chars

    async def store(
        self, output: str, *, identity: str
    ) -> StoredWorkerResult:
        """按字节上限保存大文本，并返回当前执行可见的稳定引用。

        参数：
        - `output`（str）：待处理或生成的输出内容；约束：无额外运行时限制。
        - `identity`（str）：经校验的领域身份组合，用于确认操作范围；约束：无额外运行时限制。

        返回：
        - StoredWorkerResult：封装本次操作输出或状态的 `StoredWorkerResult` 结果对象。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
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
        """读取当前 Worker 执行中登记的结果引用的一段文本。

        参数：
        - `tool_call`（Mapping[str, Any]）：Worker 请求执行的工具名称、参数及关联信息；约束：无额外运行时限制。
        - `execution`（Mapping[str, Any]）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：按 Worker 结果引用恢复的结构化结果字段。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
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
    """构造 Worker 可识别的结果读取拒绝回执。

    参数：
    - `code`（str）：用于区分错误或结果类型的代码；约束：无额外运行时限制。

    返回：
    - dict[str, Any]：包含拒绝状态、错误代码及工具标识的 Worker 回执。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return {
        "name": "read_worker_result",
        "status": "denied",
        "error": {"code": code},
    }


async def _byte_chunks(value: bytes) -> AsyncIterator[bytes]:
    """把完整的内存文本接入异步文件存储接口。

    参数：
    - `value`（bytes）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - AsyncIterator[bytes]：按顺序异步或同步产出的结果流。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    yield value
__all__ = ["StoredWorkerResult", "WorkerResultStore"]
