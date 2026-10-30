"""记忆只读查询用例。"""

from __future__ import annotations

from typing import Any

from domain.memory.query_ports import MemoryQueryPort
from application.memory.service import _to_response as record_to_response
from application.common.responses import MemoryPageResponse, MemoryResponse
from domain.memory import MemoryRecord, MemoryPage


class MemoryQueryService:
    """提供记忆搜索、分页、读取和修订历史查询。
    """
    def __init__(self, port: MemoryQueryPort) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `port`（MemoryQueryPort）：本用例依赖的应用端口，提供所需外部能力；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._port = port

    async def search(self, **kwargs: Any) -> list[MemoryResponse]:
        """按查询条件检索记忆并转换为响应对象。

        参数：
        - `kwargs`（Any）：转交给查询端口的可选筛选、分页或检索参数；约束：无额外运行时限制。

        返回：
        - list[MemoryResponse]：按查询、作用域和分页条件匹配的记忆响应列表。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return [_to_response(value) for value in await self._port.search(**kwargs)]

    async def list_memories(self, **kwargs: Any) -> MemoryPageResponse:
        """按筛选和分页条件列出记忆记录。

        参数：
        - `kwargs`（Any）：转交给查询端口的可选筛选、分页或检索参数；约束：无额外运行时限制。

        返回：
        - MemoryPageResponse：供调用方展示或继续处理的 `MemoryPageResponse` 响应对象。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        value = await self._port.list_memories(**kwargs)
        if isinstance(value, MemoryPage):
            return MemoryPageResponse(tuple(_to_response(item) for item in value.items), value.total, dict(value.stats))
        if isinstance(value, dict):
            items = tuple(_to_response(item) for item in value.get("items", ()))
            return MemoryPageResponse(items, int(value.get("total", len(items))), {key: int(item) for key, item in value.items() if key != "items" and isinstance(item, int)})
        return MemoryPageResponse((), 0, {})

    async def get(self, memory_id: str) -> MemoryResponse | None:
        """按稳定标识读取单条记忆并转换为响应。

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
        """读取指定记忆的修订历史。

        参数：
        - `memory_id`（str）：目标记忆记录的稳定标识；约束：无额外运行时限制。

        返回：
        - list[MemoryResponse]：指定记忆的修订历史响应列表。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return [_to_response(value) for value in await self._port.revisions(memory_id)]


def _to_response(value: Any) -> MemoryResponse:
    """将记忆端口的记录转换为应用响应。

    参数：
    - `value`（Any）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - MemoryResponse：供调用方展示或继续处理的 `MemoryResponse` 响应对象。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    if isinstance(value, MemoryResponse):
        return value
    if isinstance(value, MemoryRecord):
        return record_to_response(value)
    return MemoryResponse(str(value.get("id", "")), str(value.get("content", "")), dict(value.get("metadata") or {}), value.get("score"), value.get("source"), str(value.get("status", "active")),
        created_at=value.get("created_at", ""),
        expires_at=value.get("expires_at", None),
        last_accessed_at=value.get("last_accessed_at", None),
        access_count=value.get("access_count", 0),
        last_observed_at=value.get("last_observed_at", None),
        valid_until=value.get("valid_until", None),
        validity_status=value.get("validity_status", "valid"),
    )


__all__ = ["MemoryQueryService"]
