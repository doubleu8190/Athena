"""并发执行 Context Plan 的服务。"""

from __future__ import annotations

import asyncio

from typing import Protocol

from athena.core.llm.tokens import TokenCounter
from athena.core.retrieval.ports import RetrievalTraceWriter
from athena.runtime.context.contracts import (
    ContextBundle,
    ContextItem,
    ContextPlan,
    ProviderResult,
)
from athena.runtime.context.providers.file import FileContextProvider
from athena.runtime.context.providers.knowledge import KnowledgeContextProvider
from athena.runtime.context.providers.memory import MemoryContextProvider
from athena.runtime.task_understanding.contracts import UserTaskSpec
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class _ContextProvider(Protocol):
    """Context Provider 的最小协议。"""

    name: str

    async def acquire(
        self,
        *,
        session_id: str,
        task: UserTaskSpec,
        plan: ContextPlan,
    ) -> ProviderResult:
        """获取上下文。"""
        ...


class ContextAcquisitionService:
    """按 ContextPlan 并发调用 Provider 并合并结果。"""

    def __init__(
        self,
        *,
        memory_provider: MemoryContextProvider | None = None,
        knowledge_provider: KnowledgeContextProvider | None = None,
        file_provider: FileContextProvider | None = None,
        token_counter: TokenCounter | None = None,
        trace_writer: RetrievalTraceWriter | None = None,
    ) -> None:
        """绑定可用的 Provider。

        参数：
            memory_provider (MemoryContextProvider | None): Memory Provider。
            knowledge_provider (KnowledgeContextProvider | None): Knowledge Provider。
            file_provider (FileContextProvider | None): File Provider。
            token_counter (TokenCounter | None): Token 计数器，用于按预算截断。

        返回值：
            None。

        异常：
            不主动抛出业务异常。
        """
        self._providers: dict[str, _ContextProvider] = {}
        for provider in (memory_provider, knowledge_provider, file_provider):
            if provider is not None:
                self._providers[provider.name] = provider
        self._token_counter = token_counter
        self._trace_writer = trace_writer
        self._memory_provider = memory_provider

    async def acquire(
        self,
        *,
        session_id: str,
        task: UserTaskSpec,
        plan: ContextPlan,
    ) -> ContextBundle:
        """并发执行计划中的 Provider。

        参数：
            session_id (str): 当前会话唯一标识。
            task (UserTaskSpec): 当前任务理解结果。
            plan (ContextPlan): 上下文获取计划。

        返回值：
            ContextBundle: 合并后的上下文包。

        异常：
            不主动抛出异常；Provider 失败记录到 ContextBundle。
        """
        selected = [
            self._providers[provider]
            for provider in plan.providers
            if provider in self._providers
        ]
        unknown = [provider for provider in plan.providers if provider not in self._providers]
        results = await asyncio.gather(
            *(
                provider.acquire(session_id=session_id, task=task, plan=plan)
                for provider in selected
            ),
            return_exceptions=True,
        )
        normalized_results: list[ProviderResult] = []
        normalized_results.extend(
            ProviderResult(provider=provider, status="skipped", error_message="provider unavailable")
            for provider in unknown
        )
        for provider, result in zip(selected, results):
            if isinstance(result, BaseException):
                logger.warning(
                    "context_provider_unexpected_failure",
                    provider=provider.name,
                    error=str(result),
                )
                normalized_results.append(
                    ProviderResult(provider=provider.name, status="failed")
                )
            else:
                normalized_results.append(result)
        items = [item for result in normalized_results for item in result.items]
        truncated = len(items) > plan.max_items
        items = items[: plan.max_items]
        if self._token_counter is not None and plan.max_tokens:
            total_tokens = 0
            kept: list[ContextItem] = []
            for item in items:
                tokens = self._token_counter.count_text_tokens(item.content)
                if total_tokens + tokens > plan.max_tokens:
                    truncated = True
                    break
                kept.append(item)
                total_tokens += tokens
            items = kept
        # 只有最终通过全局条数和 token 预算的条目才算真正注入上下文，
        # 访问热度和检索轨迹都在这里落账，避免 provider 内部候选被误计入。
        memory_ids = [
            item.source_id
            for item in items
            if item.provider == "memory" and item.source_id
        ]
        if memory_ids and self._memory_provider is not None:
            self._memory_provider.record_selected_access(memory_ids)
        if self._trace_writer is not None:
            by_run: dict[str, list[str]] = {}
            for item in items:
                if item.retrieval_run_id and item.source_id:
                    by_run.setdefault(item.retrieval_run_id, []).append(item.source_id)
            for run_id, source_ids in by_run.items():
                try:
                    await self._trace_writer.mark_injected(run_id, source_ids)
                except Exception as exc:
                    logger.warning(
                        "retrieval_trace_injection_mark_failed",
                        run_id=run_id,
                        error=str(exc),
                    )
        return ContextBundle(
            items=items,
            provider_results=normalized_results,
            providers_succeeded=[
                result.provider
                for result in normalized_results
                if result.status == "succeeded"
            ],
            providers_failed=[
                result.provider
                for result in normalized_results
                if result.status in {"failed", "timeout"}
            ],
            truncated=truncated,
        )
