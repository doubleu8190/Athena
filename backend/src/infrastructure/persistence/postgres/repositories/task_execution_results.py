"""统一解析内联与外置的 Task 执行结果。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import TaskExecutionModel, TaskExecutionResultModel


async def load_task_execution_outputs(
    session: AsyncSession,
    executions: Sequence[TaskExecutionModel],
) -> dict[str, dict[str, Any]]:
    """批量读取执行结果，并校验外置结果的引用、长度和摘要。

    Args:
        session: 当前事务使用的 PostgreSQL 会话。
        executions: 需要解析结果的执行记录。

    Returns:
        以 execution_id 为键、完整 Task 输出为值的映射；尚无结果的执行不出现在映射中。

    Raises:
        RuntimeError: 结果引用缺失、归属不匹配或存储内容校验失败时抛出。
    """
    result_refs = [
        execution.result_ref
        for execution in executions
        if execution.result_ref is not None
    ]
    artifacts_by_ref: dict[str, TaskExecutionResultModel] = {}
    if result_refs:
        artifacts = (
            await session.execute(
                select(TaskExecutionResultModel).where(
                    TaskExecutionResultModel.result_ref.in_(result_refs)
                )
            )
        ).scalars().all()
        artifacts_by_ref = {
            artifact.result_ref: artifact for artifact in artifacts
        }

    outputs_by_execution: dict[str, dict[str, Any]] = {}
    for execution in executions:
        if execution.result_ref is not None:
            artifact = artifacts_by_ref.get(execution.result_ref)
            if artifact is None or artifact.execution_id != execution.execution_id:
                raise RuntimeError("task_execution_result_reference_missing")
            if not isinstance(artifact.result_json, Mapping):
                raise RuntimeError("task_execution_result_document_invalid")
            output = dict(artifact.result_json)
            serialized = json.dumps(
                output,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if (
                len(serialized) != artifact.content_bytes
                or hashlib.sha256(serialized).hexdigest()
                != artifact.content_sha256
            ):
                raise RuntimeError("task_execution_result_integrity_failure")
            outputs_by_execution[execution.execution_id] = output
            continue

        # 小结果仍内联在执行记录中，读取时也统一返回原始 Task 输出。
        result_document = execution.result_json
        if isinstance(result_document, Mapping):
            output_document = result_document.get("output")
            if isinstance(output_document, Mapping):
                outputs_by_execution[execution.execution_id] = dict(
                    output_document
                )

    return outputs_by_execution


def strict_result_json(value: Mapping[str, Any]) -> str:
    """按稳定键顺序序列化 JSON 结果，拒绝隐式字符串化对象。

    Args:
        value: Worker 返回的 Task 输出。

    Returns:
        UTF-8 结果文本对应的 JSON 字符串。

    Raises:
        TypeError: 结果包含 JSON 不支持的对象时抛出。
        ValueError: 结果包含非有限浮点数等非标准 JSON 值时抛出。
    """
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


__all__ = ["load_task_execution_outputs", "strict_result_json"]
