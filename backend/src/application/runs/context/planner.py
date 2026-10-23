from __future__ import annotations

from collections.abc import Callable

from application.runs.context.contracts import (
    ContextPlan,
    ContextQuery,
    FileQuery,
    GraphQuery,
    KnowledgeQuery,
    MemoryQuery,
)


class InvalidContextResource(ValueError):
    """Root 请求的文件或知识库不属于当前有效资源目录。"""


class ContextPlanner:
    """把 Root 的具体查询绑定到当前有效资源范围。"""

    def __init__(
        self,
        *,
        max_files: int = 5,
        limit_per_file: int = 5,
        max_items: int = 12,
        max_tokens: int = 4000,
        recall_limit: int = 30,
        rerank_limit: int = 10,
        token_counter: Callable[[str], int] | None = None,
    ) -> None:
        """保存检索资源限制和模型 token 计数器。

        参数：各 ``max_*``/``limit_*`` 值限制检索规模；``token_counter`` 统计资料正文。
        返回：无。
        异常：无业务异常。
        """
        self._max_files = max_files
        self._limit_per_file = limit_per_file
        self._max_items = max_items
        self._max_tokens = max_tokens
        self._recall_limit = recall_limit
        self._rerank_limit = rerank_limit
        self._token_counter = token_counter or (lambda text: len(text.split()))

    def plan(
        self,
        queries: tuple[ContextQuery, ...],
        *,
        attachment_ids: tuple[str, ...],
        knowledge_base_ids: tuple[str, ...],
    ) -> ContextPlan:
        """根据已校验查询生成 Provider 计划。
        Args:
            queries: 每个检索通道至多一个具体查询。
            attachment_ids: 当前请求已验证且可读的附件 ID。
            knowledge_base_ids: 当前系统中真实存在的知识库 ID。
        Returns:
            可直接交给 ContextAcquisitionService 的有界计划。
        Raises:
            InvalidContextResource: 查询引用了目录之外的附件或知识库。
        """
        available_attachments = set(attachment_ids)
        available_knowledge_bases = set(knowledge_base_ids)
        memory_query = ""
        file_query = ""
        file_ids: tuple[str, ...] = ()
        knowledge_query = ""
        selected_knowledge_base_ids: tuple[str, ...] = ()
        graph_query = ""
        for query in queries:
            if isinstance(query, MemoryQuery):
                memory_query = query.query
            elif isinstance(query, FileQuery):
                requested_ids = query.file_ids or attachment_ids
                if not set(requested_ids).issubset(available_attachments):
                    raise InvalidContextResource("context_file_outside_current_request")
                file_query = query.query
                file_ids = requested_ids
            elif isinstance(query, KnowledgeQuery):
                requested_ids = query.knowledge_base_ids or knowledge_base_ids
                if not set(requested_ids).issubset(available_knowledge_bases):
                    raise InvalidContextResource("context_knowledge_base_unavailable")
                knowledge_query = query.query
                selected_knowledge_base_ids = requested_ids
            elif isinstance(query, GraphQuery):
                graph_query = query.query
        return ContextPlan(
            providers=tuple(query.provider for query in queries),
            memory_query=memory_query,
            file_query=file_query,
            file_ids=file_ids,
            knowledge_query=knowledge_query,
            knowledge_base_ids=selected_knowledge_base_ids,
            graph_query=graph_query,
            max_files=self._max_files,
            limit_per_file=self._limit_per_file,
            max_items=self._max_items,
            max_tokens=self._max_tokens,
            recall_limit=self._recall_limit,
            rerank_limit=self._rerank_limit,
        )

    @property
    def max_items(self) -> int:
        """当前 ContextBundle 可保留的最大条目数。"""
        return self._max_items

    @property
    def max_tokens(self) -> int:
        """当前 ContextBundle 可保留的 token 上限。"""
        return self._max_tokens

    def count_tokens(self, text: str) -> int:
        """使用装配时提供的计数器统计一段资料正文。

        参数：``text`` 是待计数的资料正文。
        返回：token 数量。
        异常：计数器自身抛出的异常会传播给调用方。
        """
        return self._token_counter(text)


__all__ = ["ContextPlanner", "InvalidContextResource"]
