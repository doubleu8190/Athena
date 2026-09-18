"""长期记忆上下文 Provider。"""

from __future__ import annotations

import asyncio
import time

from athena.core.memory.contracts import MemoryRetrievalRequest
from athena.core.memory.retrieval import (
    MemoryRetrievalResult,
    MemoryRetrievalService,
)
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
            query=query,
            limit=self._limit,
        )
        try:
            async with asyncio.timeout(self._timeout_seconds):
                results = await self._memory_retrieval.get_context(request)
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
        items = [self._to_context_item(result) for result in results]
        return ProviderResult(
            provider=self.name,
            status="succeeded",
            items=items,
            candidate_count=len(items),
            result_count=len(items),
            duration_ms=round((time.perf_counter() - started) * 1000),
        )

    @staticmethod
    def _to_context_item(result: MemoryRetrievalResult) -> ContextItem:
        """将记忆检索结果映射为保留证据元数据的上下文条目。

        参数：
            result (MemoryRetrievalResult): 已通过相关度和 token 预算筛选的记忆。

        返回值：
            ContextItem: 带记忆 ID、相关度和来源元数据的上下文条目。

        异常：
            不主动抛出业务异常；输入元数据缺失时按空字段处理。
        """
        metadata = {
            key: result.metadata[key]
            for key in (
                "source",
                "source_turn_id",
                "confidence",
                "category",
                "type",
                "validity_status",
                "revision",
            )
            if result.metadata.get(key) not in (None, "")
        }
        metadata["retrieval_source"] = result.source
        score = next(
            (
                value
                for value in (
                    result.rerank_score,
                    result.fused_score,
                    result.native_score,
                )
                if value is not None
            ),
            None,
        )
        return ContextItem(
            provider=MemoryContextProvider.name,
            content=result.content,
            source_id=result.memory_id,
            score=score,
            metadata=metadata,
        )
