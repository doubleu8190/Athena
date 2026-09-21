"""根据 UserTaskSpec 推导上下文获取计划。"""

from __future__ import annotations

from athena.runtime.context.contracts import ContextPlan, ContextRequirement
from athena.runtime.task_understanding.contracts import UserTaskSpec


class ContextPlanner:
    """把任务理解结果映射为 Context Provider 计划。"""

    def plan(
        self,
        task: UserTaskSpec,
        *,
        requested_file_ids: list[str],
        max_files: int = 5,
        limit_per_file: int = 5,
        max_items: int = 12,
        max_tokens: int = 4000,
    ) -> ContextPlan:
        """推导 Provider 列表和检索参数。

        参数：
            task (UserTaskSpec): 当前任务理解结果。
            requested_file_ids (list[str]): 当前消息显式附件 ID。
            max_files (int): Knowledge Provider 最多检索的文档数量。
            limit_per_file (int): 每个文档的最大结果数量。
            max_items (int): Context Bundle 最大条目数。
            max_tokens (int): Context Bundle 最大 token 预算。

        返回值：
            ContextPlan: 上下文获取计划。

        异常：
            不主动抛出异常。
        """
        providers: list[ContextRequirement] = []
        for requirement in task.context_requirements:
            if requirement not in providers:
                providers.append(requirement)
        if requested_file_ids and "file" not in providers:
            providers.append("file")
        return ContextPlan(
            providers=providers,
            memory_query=task.query_hints.memory,
            knowledge_query=task.query_hints.knowledge,
            file_query=task.query_hints.file,
            file_ids=requested_file_ids,
            knowledge_base_ids=task.slots.knowledge_base_ids,
            max_files=max_files,
            limit_per_file=limit_per_file,
            max_items=max_items,
            max_tokens=max_tokens,
        )
