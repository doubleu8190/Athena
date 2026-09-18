"""长期记忆上下文 Provider。"""

from __future__ import annotations

import asyncio
import time

from athena.core.memory.contracts import MemoryRetrievalRequest
from athena.core.memory.retrieval import MemoryRetrievalService
from athena.runtime.context.contracts import (
    ContextItem,
    ContextPlan,
    ProviderResult,
)
from athena.runtime.task_understanding.contracts import UserTaskSpec
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class MemoryContextProvider:
    """从长期记忆中获取与当前任务相关的上下文。"""

    name = "memory"

    def __init__(
        self,
        memory_retrieval: MemoryRetrievalService,
        *,
        timeout_seconds: float = 3.0,
        limit: int = 8,
    ) -> None:
        """绑定底层记忆检索服务。

        参数：
            memory_retrieval (MemoryRetrievalService): 底层检索服务。
            timeout_seconds (float): 检索超时时间（秒）。
            limit (int): 最大记忆条目数。

        返回值：
            None。

        异常：
            不主动抛出业务异常。
        """
        self._memory_retrieval = memory_retrieval
        self._timeout_seconds = timeout_seconds
        self._limit = limit

    async def acquire(
        self,
        *,
        session_id: str,
        task: UserTaskSpec,
        plan: ContextPlan,
    ) -> ProviderResult:
        """执行记忆检索。

        参数：
            session_id (str): 当前会话唯一标识。
            task (UserTaskSpec): 当前任务理解结果。
            plan (ContextPlan): 上下文获取计划。

        返回值：
            ProviderResult: 检索结果；失败或超时时返回空结果。

        异常：
            不向调用方传播异常；所有失败都降级为空结果。
        """
        started = time.perf_counter()
        query = plan.memory_query or task.goal
        request = MemoryRetrievalRequest(
            session_id=session_id,
            query=query,
            task=task.goal,
            limit=self._limit,
        )
        try:
            async with asyncio.timeout(self._timeout_seconds):
                context = await self._memory_retrieval.get_context(request)
        except TimeoutError:
            logger.warning("memory_context_provider_timeout", session_id=session_id)
            return ProviderResult(provider=self.name, status="timeout", duration_ms=round((time.perf_counter() - started) * 1000))
        except Exception as exc:
            logger.warning(
                "memory_context_provider_failed",
                session_id=session_id,
                error=str(exc),
            )
            return ProviderResult(
                provider=self.name,
                status="failed",
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_message=str(exc),
            )
        items = []
        if context:
            content = context.replace("[相关记忆]", "").replace("[/相关记忆]", "").strip()
            for line in content.splitlines():
                line = line.strip().lstrip("- ").strip()
                if line:
                    items.append(ContextItem(provider=self.name, content=line))
        return ProviderResult(
            provider=self.name,
            status="succeeded",
            items=items,
            candidate_count=len(items),
            result_count=len(items),
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
