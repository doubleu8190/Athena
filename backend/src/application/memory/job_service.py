"""记忆写入队列的管理查询用例。"""

from __future__ import annotations

from domain.memory import MemoryJobStatus, MemoryJobStatusPort


class MemoryJobManagementService:
    """读取有限、脱敏的记忆作业状态。"""

    def __init__(self, repository: MemoryJobStatusPort) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `repository`（MemoryJobStatusPort）：负责读取或持久化领域数据的仓储依赖；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._repository = repository

    async def list_recent(
        self, *, limit: int, offset: int, session_id: str | None = None
    ) -> tuple[list[MemoryJobStatus], int]:
        """按分页和会话条件列出最近的记忆处理作业。

        参数：
        - `limit`（int）：本次读取或处理的数量上限；约束：须通过方法内校验；失败条件见下方异常说明。
        - `offset`（int）：分页查询时跳过的记录数；约束：须通过方法内校验；失败条件见下方异常说明。
        - `session_id`（str | None）：目标会话的稳定标识，用于限定数据范围；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - tuple[list[MemoryJobStatus], int]：按条件分页读取的记忆作业状态列表及总数。

        异常：
        - ValueError：校验条件 `not 1 <= limit <= 100 or offset < 0` 成立时（异常说明：memory_job_pagination_invalid）。
        - 未列出的依赖异常按当前实现传播。
        """
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("memory_job_pagination_invalid")
        return await self._repository.list_recent(
            limit=limit, offset=offset, session_id=session_id
        )


__all__ = ["MemoryJobManagementService"]
