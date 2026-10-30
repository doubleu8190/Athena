"""长期记忆应用服务。"""

from __future__ import annotations

from domain.memory import (
    MemoryListRequest,
    MemoryPage,
    MemoryRepositoryPort,
    MemoryRecord,
    MemoryRelationRecord,
    MemorySearchRequest,
    MemoryWriteCommand,
)
from application.common.responses import MemoryPageResponse, MemoryResponse


class MemoryService:
    """协调记忆检索、写入、版本和生命周期操作。"""

    def __init__(self, port: MemoryRepositoryPort) -> None:
        """初始化服务。

        参数：
        - `port`（MemoryRepositoryPort）：由 infrastructure 提供的记忆能力端口；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._port = port

    async def initialize(self) -> None:
        """初始化记忆索引等外部资源。

        参数：
        - 无参数。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        await self._port.initialize()

    async def search(self, request: MemorySearchRequest) -> list[MemoryResponse]:
        """执行记忆检索。

        参数：
        - `request`（MemorySearchRequest）：当前请求及其关联身份，用于限定操作范围；约束：无额外运行时限制。

        返回：
        - list[MemoryResponse]：按查询、作用域和分页条件匹配的记忆响应列表。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return [_to_response(value) for value in await self._port.search(request)]

    async def list_memories(self, request: MemoryListRequest) -> MemoryPageResponse:
        """分页列出记忆。

        参数：
        - `request`（MemoryListRequest）：当前请求及其关联身份，用于限定操作范围；约束：无额外运行时限制。

        返回：
        - MemoryPageResponse：供调用方展示或继续处理的 `MemoryPageResponse` 响应对象。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        value = await self._port.list_memories(request)
        return MemoryPageResponse(tuple(_to_response(item) for item in value.items), value.total, dict(value.stats))

    async def get(self, memory_id: str) -> MemoryResponse | None:
        """按 ID 获取记忆。

        参数：
        - `memory_id`（str）：目标记忆记录的稳定标识；约束：无额外运行时限制。

        返回：
        - MemoryResponse | None：指定标识的记忆响应；记录不存在或不可见时返回 None。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        value = await self._port.get(memory_id)
        return None if value is None else _to_response(value)

    async def revisions(self, memory_id: str) -> list[MemoryResponse]:
        """获取记忆的不可变修订历史。

        参数：
        - `memory_id`（str）：目标记忆记录的稳定标识；约束：无额外运行时限制。

        返回：
        - list[MemoryResponse]：指定记忆的修订历史响应列表。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return [_to_response(value) for value in await self._port.revisions(memory_id)]

    async def related_entries(self, memory_id: str) -> list[MemoryRelationRecord]:
        """返回与记忆直接关联的替代或矛盾条目。

        参数：
        - `memory_id`（str）：目标记忆记录的稳定标识；约束：无额外运行时限制。

        返回：
        - list[MemoryRelationRecord]：与指定记忆存在关系且符合可见范围的记录列表。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return await self._port.related_entries(memory_id)

    async def create_memory(self, command: MemoryWriteCommand) -> str:
        """创建记忆并返回稳定 ID。

        参数：
        - `command`（MemoryWriteCommand）：领域命令，携带幂等身份和目标对象；约束：无额外运行时限制。

        返回：
        - str：新建记忆记录的稳定标识。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return await self._port.create_memory(command)

    async def revise(
        self,
        memory_id: str,
        content: str,
        *,
        metadata: dict[str, object] | None = None,
        operation_key: str | None = None,
    ) -> str | None:
        """创建新修订版并保留旧版本。

        参数：
        - `memory_id`（str）：目标记忆记录的稳定标识；约束：无额外运行时限制。
        - `content`（str）：待处理或返回的正文内容；约束：须通过方法内校验；失败条件见下方异常说明。
        - `metadata`（dict[str, object] | None）：随领域记录保存的结构化附加字段；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `operation_key`（str | None）：标识同一工具副作用操作的幂等键；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - str | None：新修订记忆记录的标识；原记录不存在或不可修改时返回 None。

        异常：
        - ValueError：校验条件 `not content.strip()` 成立时（异常说明：memory content must not be empty）。
        - 未列出的依赖异常按当前实现传播。
        """
        if not content.strip():
            raise ValueError("memory content must not be empty")
        return await self._port.revise(
            memory_id,
            content,
            metadata=metadata,
            operation_key=operation_key,
        )

    async def update_validity(
        self,
        memory_id: str,
        validity_status: str,
        *,
        valid_until: str | None = None,
    ) -> bool:
        """更新事实有效性。

        参数：
        - `memory_id`（str）：目标记忆记录的稳定标识；约束：无额外运行时限制。
        - `validity_status`（str）：记忆事实的有效性状态；约束：无额外运行时限制。
        - `valid_until`（str | None）：事实有效期的截止时间文本；None 表示没有显式截止时间；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - bool：操作是否成功或条件是否成立。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return await self._port.update_validity(
            memory_id,
            validity_status,
            valid_until=valid_until,
        )

    async def delete(self, memory_id: str) -> bool:
        """遗忘一条记忆并返回目标是否存在。

        参数：
        - `memory_id`（str）：目标记忆记录的稳定标识；约束：无额外运行时限制。

        返回：
        - bool：操作是否成功或条件是否成立。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return await self._port.delete(memory_id)

    async def flush_access_stats(self) -> int:
        """刷新检索访问统计。

        参数：
        - 无参数。

        返回：
        - int：成功写回存储层的访问统计条数。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return await self._port.flush_access_stats()


__all__ = ["MemoryService"]


def _to_response(value: MemoryRecord) -> MemoryResponse:
    """将记忆实体转换为应用响应。

    参数：
    - `value`（MemoryRecord）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - MemoryResponse：供调用方展示或继续处理的 `MemoryResponse` 响应对象。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return MemoryResponse(
        id=value.id,
        content=value.content,
        metadata=dict(value.metadata),
        score=value.score,
        source=value.source,
        status=value.status.value,
        created_at=value.created_at,
        expires_at=value.expires_at,
        last_accessed_at=value.last_accessed_at,
        access_count=value.access_count,
        last_observed_at=value.last_observed_at,
        valid_until=value.valid_until,
        validity_status=value.validity_status,

    )
