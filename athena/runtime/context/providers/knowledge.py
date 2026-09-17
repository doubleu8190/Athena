"""全局知识库文档上下文 Provider。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from athena.core.files.runtime import FileIntelligenceRuntime
from athena.infrastructure.sqlite.repositories.file_repository import FileRepository
from athena.models.file import AttachmentStatus
from athena.runtime.context.contracts import (
    ContextItem,
    ContextPlan,
    ProviderResult,
)
from athena.runtime.task_understanding.contracts import UserTaskSpec
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class KnowledgeContextProvider:
    """对全局知识库文档执行跨文档检索并合并结果。"""

    name = "knowledge"

    def __init__(
        self,
        repository: FileRepository,
        file_runtime: FileIntelligenceRuntime,
        *,
        timeout_seconds: float = 5.0,
        concurrency: int = 3,
    ) -> None:
        """绑定文件持久化层和检索运行时。

        参数：
            repository (FileRepository): 文件持久化仓库。
            file_runtime (FileIntelligenceRuntime): 文件混合检索运行时。
            timeout_seconds (float): 单文档检索超时时间（秒）。
            concurrency (int): 最大并发文档数。

        返回值：
            None。

        异常：
            不主动抛出业务异常。
        """
        self._repository = repository
        self._file_runtime = file_runtime
        self._timeout_seconds = timeout_seconds
        self._concurrency = concurrency

    async def acquire(
        self,
        *,
        session_id: str,
        task: UserTaskSpec,
        plan: ContextPlan,
    ) -> ProviderResult:
        """并发检索全局知识库文档。

        参数：
            session_id (str): 发起检索的会话唯一标识，仅用于日志和文件运行时校验。
            task (UserTaskSpec): 当前任务理解结果。
            plan (ContextPlan): 上下文获取计划。

        返回值：
            ProviderResult: 合并排序后的检索结果；失败时返回空结果。

        异常：
            不向调用方传播异常；所有失败都降级为空结果。
        """
        started = time.perf_counter()
        query = plan.knowledge_query or task.goal
        try:
            documents = await self._repository.list_global_knowledge_documents()
        except Exception as exc:
            logger.warning("knowledge_context_provider_list_failed", error=str(exc))
            return ProviderResult(
                provider=self.name,
                status="failed",
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_message=str(exc),
            )
        ready_documents = [
            document
            for document in documents
            if document.status == AttachmentStatus.READY
        ][: plan.max_files]
        if not ready_documents:
            return ProviderResult(
                provider=self.name,
                status="succeeded",
                duration_ms=round((time.perf_counter() - started) * 1000),
            )
        semaphore = asyncio.Semaphore(self._concurrency)

        async def search_one(document) -> list[dict[str, Any]]:
            """检索单个文档并限制并发。"""
            async with semaphore:
                try:
                    async with asyncio.timeout(self._timeout_seconds):
                        response = await self._file_runtime.search_file(
                            session_id,
                            document.id,
                            query,
                            plan.limit_per_file,
                        )
                    return list(response.get("results", []))
                except Exception as exc:
                    logger.warning(
                        "knowledge_context_provider_document_failed",
                        file_id=document.id,
                        error=str(exc),
                    )
                    return []

        result_groups = await asyncio.gather(
            *(search_one(document) for document in ready_documents)
        )
        candidates: list[tuple[float, dict[str, Any], Any]] = []
        for document, results in zip(ready_documents, result_groups):
            for rank, result in enumerate(results, 1):
                candidates.append((1 / (60 + rank), result, document))
        candidates.sort(key=lambda value: value[0], reverse=True)
        items = [
            ContextItem(
                provider=self.name,
                content=result.get("content", ""),
                source_id=document.id,
                title=document.filename,
                locator=result.get("locator", {}),
                score=rrf_score,
                metadata={"knowledge_base_id": document.knowledge_base_id},
            )
            for rrf_score, result, document in candidates[: plan.max_items]
        ]
        return ProviderResult(
            provider=self.name,
            status="succeeded",
            items=items,
            candidate_count=len(candidates),
            result_count=len(items),
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
