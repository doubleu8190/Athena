"""项目用例。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from domain.projects import Project, ProjectRepositoryPort


class ProjectNotFoundError(LookupError):
    """目标项目不存在或已经删除。"""


class ProjectService:
    """创建和管理聊天项目。"""

    def __init__(
        self,
        repository: ProjectRepositoryPort,
        *,
        id_factory: Callable[[], str],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """保存项目仓储及可替换的 ID、时钟来源。

        参数：
        - `repository`（ProjectRepositoryPort）：负责读取或持久化领域数据的仓储依赖；约束：无额外运行时限制。
        - `id_factory`（Callable[[], str]）：生成稳定领域标识的可注入函数；约束：无额外运行时限制。
        - `clock`（Callable[[], datetime] | None）：生成当前时间的可注入函数，可用于控制时间来源；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._repository = repository
        self._id_factory = id_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def list(self) -> list[Project]:
        """返回未删除项目，顺序由仓储按最近聊天活动决定。

        参数：
        - 无参数。

        返回：
        - list[Project]：符合条件的有序结果集合；集合成员。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return await self._repository.list()

    async def get(self, project_id: str) -> Project:
        """按 ID 读取项目；找不到时抛出 ProjectNotFoundError。

        参数：
        - `project_id`（str）：项目空间的稳定标识；None 表示未绑定项目；约束：无额外运行时限制。

        返回：
        - Project：项目标识、名称和项目级配置组成的领域对象。

        异常：
        - ProjectNotFoundError：校验条件 `project is None` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        project = await self._repository.get(project_id)
        if project is None:
            raise ProjectNotFoundError(project_id)
        return project

    async def create(self, name: str) -> Project:
        """创建项目并分配稳定 ID；名称允许与现有项目相同。

        参数：
        - `name`（str）：目标名称，用于标识相应配置、工具或对象；约束：无额外运行时限制。

        返回：
        - Project：项目标识、名称和项目级配置组成的领域对象。

        异常：
        - ValueError：校验条件 `not normalized or len(normalized) > 120` 成立时（异常说明：project_name_invalid）。
        - 未列出的依赖异常按当前实现传播。
        """
        normalized = name.strip()
        if not normalized or len(normalized) > 120:
            raise ValueError("project_name_invalid")
        now = self._clock().astimezone(timezone.utc).isoformat()
        return await self._repository.create(
            Project(
                id=self._id_factory(),
                name=normalized,
                created_at=now,
                updated_at=now,
            )
        )

    async def rename(self, project_id: str, name: str) -> Project:
        """更新项目名称；ID 与会话、记忆归属保持不变。

        参数：
        - `project_id`（str）：项目空间的稳定标识；None 表示未绑定项目；约束：无额外运行时限制。
        - `name`（str）：目标名称，用于标识相应配置、工具或对象；约束：无额外运行时限制。

        返回：
        - Project：项目标识、名称和项目级配置组成的领域对象。

        异常：
        - ValueError：校验条件 `not normalized or len(normalized) > 120` 成立时（异常说明：project_name_invalid）。
        - ProjectNotFoundError：校验条件 `project is None` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        normalized = name.strip()
        if not normalized or len(normalized) > 120:
            raise ValueError("project_name_invalid")
        project = await self._repository.rename(project_id, normalized)
        if project is None:
            raise ProjectNotFoundError(project_id)
        return project

    async def delete(self, project_id: str) -> None:
        """删除项目和项目级数据；活动 Run 会由仓储拒绝删除。

        参数：
        - `project_id`（str）：项目空间的稳定标识；None 表示未绑定项目；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - ProjectNotFoundError：校验条件 `not await self._repository.delete(project_id)` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        if not await self._repository.delete(project_id):
            raise ProjectNotFoundError(project_id)


__all__ = ["ProjectNotFoundError", "ProjectService"]
