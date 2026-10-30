"""知识库查询用例。"""

from __future__ import annotations

from domain.knowledge import KnowledgeBase, KnowledgeBaseRepository
from application.common.responses import KnowledgeBaseResponse


class KnowledgeBaseQueryService:
    """提供知识库只读查询用例。
    """
    def __init__(self, repository: KnowledgeBaseRepository) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `repository`（KnowledgeBaseRepository）：负责读取或持久化领域数据的仓储依赖；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._repository = repository

    async def list_knowledge_bases(self) -> list[KnowledgeBaseResponse]:
        """列出可访问的知识库并转换为响应对象。

        参数：
        - 无参数。

        返回：
        - list[KnowledgeBaseResponse]：符合条件的有序结果集合；集合成员。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return [_to_response(value) for value in await self._repository.list_knowledge_bases()]

    async def get(self, knowledge_base_id: str) -> KnowledgeBaseResponse | None:
        """读取指定知识库并转换为响应；不存在时返回 None。

        参数：
        - `knowledge_base_id`（str）：目标知识库的稳定标识，用于限定文档操作范围；约束：无额外运行时限制。

        返回：
        - KnowledgeBaseResponse | None：知识库标识、名称、说明和文档统计组成的响应。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        value = await self._repository.get(knowledge_base_id)
        return None if value is None else _to_response(value)


def _to_response(value: KnowledgeBase) -> KnowledgeBaseResponse:
    """将知识库实体转换为应用响应。

    参数：
    - `value`（KnowledgeBase）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - KnowledgeBaseResponse：知识库标识、名称、说明和文档统计组成的响应。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return KnowledgeBaseResponse(value.id, value.name, value.description, value.document_count, value.ready_document_count, value.total_size_bytes, value.created_at, value.updated_at)
