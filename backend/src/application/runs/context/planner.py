"""根据 Root 请求和可用资源生成有界的上下文检索计划。
"""

from __future__ import annotations

from collections.abc import Callable

from application.runs.context.contracts import (
    ContextPlan,
    ContextQuery,
    EpisodeQuery,
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
        memory_token_budget: int = 2000,
        recall_limit: int = 30,
        rerank_limit: int = 10,
        token_counter: Callable[[str], int] | None = None,
    ) -> None:
        """保存检索资源限制和模型 token 计数器。

        参数：
        - `max_files`（int）：一次上下文计划最多纳入的文件数量；约束：可省略，默认值为 `5`。
        - `limit_per_file`（int）：单个文件最多返回的候选数；约束：可省略，默认值为 `5`。
        - `max_items`（int）：上下文最多保留的条目数；约束：可省略，默认值为 `12`。
        - `max_tokens`（int）：上下文最多占用的 token 数；约束：可省略，默认值为 `4000`。
        - `memory_token_budget`（int）：记忆来源可使用的 token 预算；约束：可省略，默认值为 `2000`。
        - `recall_limit`（int）：本次召回阶段最多保留的候选数量；约束：可省略，默认值为 `30`。
        - `rerank_limit`（int）：重排阶段最多处理的候选数；约束：可省略，默认值为 `10`。
        - `token_counter`（Callable[[str], int] | None）：估算文本 token 数量的可注入函数；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._max_files = max_files
        self._limit_per_file = limit_per_file
        self._max_items = max_items
        self._max_tokens = max_tokens
        self._memory_token_budget = memory_token_budget
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

        参数：
        - `queries`（tuple[ContextQuery, ...]）：每个检索通道至多一个具体查询；约束：无额外运行时限制。
        - `attachment_ids`（tuple[str, ...]）：当前请求已验证且可读的附件 ID；约束：无额外运行时限制。
        - `knowledge_base_ids`（tuple[str, ...]）：当前系统中真实存在的知识库 ID；约束：无额外运行时限制。

        返回：
        - ContextPlan：可直接交给 ContextAcquisitionService 的有界计划。

        异常：
        - InvalidContextResource：校验条件 `not (isinstance(query, MemoryQuery))；not (isinstance(query, EpisodeQuery))；isinstance(query, FileQuery)；not set(requested_ids).issubset(available_attachments)` 成立时（异常说明：context_file_outside_current_request）。
        - InvalidContextResource：校验条件 `not (isinstance(query, MemoryQuery))；not (isinstance(query, EpisodeQuery))；not (isinstance(query, FileQuery))；isinstance(query, KnowledgeQuery)；not set(requested_ids).issubset(available_knowledge_bases)` 成立时（异常说明：context_knowledge_base_unavailable）。
        - 未列出的依赖异常按当前实现传播。
        """
        available_attachments = set(attachment_ids)
        available_knowledge_bases = set(knowledge_base_ids)
        memory_query = ""
        episode_query = ""
        episode_include_sources = False
        file_query = ""
        file_ids: tuple[str, ...] = ()
        knowledge_query = ""
        selected_knowledge_base_ids: tuple[str, ...] = ()
        graph_query = ""
        # 每种查询写入各自的计划字段，并在边界处确认资源属于当前请求。
        for query in queries:
            if isinstance(query, MemoryQuery):
                memory_query = query.query
            elif isinstance(query, EpisodeQuery):
                episode_query = query.query
                episode_include_sources = query.include_sources
            elif isinstance(query, FileQuery):
                requested_ids = query.file_ids or attachment_ids
                if not set(requested_ids).issubset(available_attachments):
                    raise InvalidContextResource("context_file_outside_current_request")
                file_query = query.query
                file_ids = requested_ids
            elif isinstance(query, KnowledgeQuery):
                requested_ids = (
                    knowledge_base_ids
                    if query.knowledge_base_ids is None
                    else query.knowledge_base_ids
                )
                if not set(requested_ids).issubset(available_knowledge_bases):
                    raise InvalidContextResource("context_knowledge_base_unavailable")
                knowledge_query = query.query
                selected_knowledge_base_ids = requested_ids
            elif isinstance(query, GraphQuery):
                graph_query = query.query
        return ContextPlan(
            providers=tuple(query.provider for query in queries),
            memory_query=memory_query,
            episode_query=episode_query,
            episode_include_sources=episode_include_sources,
            file_query=file_query,
            file_ids=file_ids,
            knowledge_query=knowledge_query,
            knowledge_base_ids=selected_knowledge_base_ids,
            graph_query=graph_query,
            max_files=self._max_files,
            limit_per_file=self._limit_per_file,
            max_items=self._max_items,
            max_tokens=self._max_tokens,
            memory_token_budget=self._memory_token_budget,
            recall_limit=self._recall_limit,
            rerank_limit=self._rerank_limit,
        )

    @property
    def max_items(self) -> int:
        """当前 ContextBundle 可保留的最大条目数。

        参数：
        - 无参数。

        返回：
        - int：当前上下文计划允许保留的最大条目数。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return self._max_items

    @property
    def max_tokens(self) -> int:
        """返回当前 ContextBundle 可保留的 token 上限。

        参数：
        - 无参数。

        返回：
        - int：当前上下文计划允许使用的最大 token 数。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return self._max_tokens

    @property
    def memory_token_budget(self) -> int:
        """长期记忆、派生目录和任务回顾共享的窗口预算。

        参数：
        - 无参数。

        返回：
        - int：长期记忆、派生索引和任务回顾共用的 token 预算。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return self._memory_token_budget

    def count_tokens(self, text: str) -> int:
        """使用装配时提供的计数器统计一段资料正文。

        参数：
        - `text`（str）：待规范化、解析或展示的文本；约束：无额外运行时限制。

        返回：
        - int：按当前 token 估算器计算出的文本 token 数。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return self._token_counter(text)


__all__ = ["ContextPlanner", "InvalidContextResource"]
