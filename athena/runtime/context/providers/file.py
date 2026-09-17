"""当前请求附件上下文 Provider。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from athena.core.files.runtime import FileIntelligenceRuntime
from athena.runtime.context.contracts import (
    ContextItem,
    ContextPlan,
    ProviderResult,
)
from athena.runtime.task_understanding.contracts import UserTaskSpec
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class FileContextProvider:
    """对当前消息附件执行预检索。"""

    name = "file"

    def __init__(
        self,
        file_runtime: FileIntelligenceRuntime,
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        """绑定文件检索运行时。

        参数：
            file_runtime (FileIntelligenceRuntime): 文件混合检索运行时。
            timeout_seconds (float): 单文件检索超时时间（秒）。

        返回值：
            None。

        异常：
            不主动抛出业务异常。
        """
        self._file_runtime = file_runtime
        self._timeout_seconds = timeout_seconds

    async def acquire(
        self,
        *,
        session_id: str,
        task: UserTaskSpec,
        plan: ContextPlan,
    ) -> ProviderResult:
        """并发检索当前附件。

        参数：
            session_id (str): 当前会话唯一标识。
            task (UserTaskSpec): 当前任务理解结果。
            plan (ContextPlan): 上下文获取计划。

        返回值：
            ProviderResult: 检索结果；失败或超时返回空结果。

        异常：
            不向调用方传播异常；所有失败都降级为空结果。
        """
        started = time.perf_counter()
        query = plan.knowledge_query or task.goal

        async def search_one(file_id: str) -> list[dict[str, Any]]:
            """检索单个附件并降级错误。"""
            try:
                async with asyncio.timeout(self._timeout_seconds):
                    response = await self._file_runtime.search_file(
                        session_id, file_id, query, plan.limit_per_file
                    )
                return list(response.get("results", []))
            except Exception as exc:
                logger.warning(
                    "file_context_provider_failed",
                    session_id=session_id,
                    file_id=file_id,
                    error=str(exc),
                )
                return []

        result_groups = await asyncio.gather(
            *(search_one(file_id) for file_id in plan.file_ids)
        )
        items = [
            ContextItem(
                provider=self.name,
                content=result.get("content", ""),
                source_id=file_id,
                locator=result.get("locator", {}),
                score=result.get("score"),
            )
            for file_id, results in zip(plan.file_ids, result_groups)
            for result in results[: plan.limit_per_file]
        ][: plan.max_items]
        return ProviderResult(
            provider=self.name,
            status="succeeded",
            items=items,
            candidate_count=sum(len(group) for group in result_groups),
            result_count=len(items),
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
