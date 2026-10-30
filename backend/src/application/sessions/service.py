"""会话用例服务。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from domain.sessions import Session, SessionRepository
from domain.projects import ProjectRepositoryPort

from application.common.responses import SessionResponse


class SessionNotFoundError(LookupError):
    """请求的会话不存在。"""


class SessionService:
    """协调会话用例，不感知 HTTP 或具体数据库。"""

    def __init__(
        self,
        repository: SessionRepository,
        *,
        id_factory: Callable[[], str],
        projects: ProjectRepositoryPort | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `repository`（SessionRepository）：负责读取或持久化领域数据的仓储依赖；约束：无额外运行时限制。
        - `id_factory`（Callable[[], str]）：生成稳定领域标识的可注入函数；约束：无额外运行时限制。
        - `projects`（ProjectRepositoryPort | None）：查询或校验项目归属的可选仓储端口；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `clock`（Callable[[], datetime] | None）：生成当前时间的可注入函数，可用于控制时间来源；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._repository = repository
        self._id_factory = id_factory
        self._projects = projects
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def create(
        self,
        *,
        project_id: str,
        creation_request_id: str,
        title: str = "新聊天",
    ) -> SessionResponse:
        """创建会话并持久化。

        参数：
        - `project_id`（str）：新会话所属的未删除项目；约束：须通过方法内校验；失败条件见下方异常说明。
        - `creation_request_id`（str）：稳定请求 ID，用于网络重试幂等；约束：须通过方法内校验；失败条件见下方异常说明。
        - `title`（str）：初始会话标题；正常聊天使用默认占位标题；约束：可省略，默认值为 `'新聊天'`。

        返回：
        - SessionResponse：会话标识、项目归属和会话元数据组成的响应。

        异常：
        - ValueError：校验条件 `not project_id.strip() or not creation_request_id.strip()` 成立时（异常说明：session_creation_request_invalid）。
        - LookupError：校验条件 `self._projects is not None and await self._projects.get(project_id) is None` 成立时（异常说明：project_not_found）。
        - 未列出的依赖异常按当前实现传播。
        """
        if not project_id.strip() or not creation_request_id.strip():
            raise ValueError("session_creation_request_invalid")
        if self._projects is not None and await self._projects.get(project_id) is None:
            raise LookupError("project_not_found")
        session = Session.create(
            session_id=self._id_factory(),
            title=title,
            now=self._clock(),
            project_id=project_id,
        )
        persisted, replayed = await self._repository.create_idempotent(
            session,
            request_id=creation_request_id,
            parameters={"project_id": project_id, "title": title},
        )
        return _to_response(persisted, idempotency_replayed=replayed)

    async def list_sessions(
        self, *, project_id: str | None = None
    ) -> list[SessionResponse]:
        """返回所有未删除会话。

        参数：
        - `project_id`（str | None）：项目空间的稳定标识；None 表示未绑定项目；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - list[SessionResponse]：符合条件的有序结果集合；集合成员。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        values = (
            await self._repository.list_sessions()
            if project_id is None
            else await self._repository.list_sessions(project_id=project_id)
        )
        return [_to_response(value) for value in values]

    async def get(self, session_id: str) -> SessionResponse:
        """按 ID 获取会话。

        参数：
        - `session_id`（str）：会话唯一标识；约束：无额外运行时限制。

        返回：
        - SessionResponse：会话标识、项目归属和会话元数据组成的响应。

        异常：
        - SessionNotFoundError：校验条件 `session is None` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        session = await self._repository.get(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)
        return _to_response(session)

    async def rename(self, session_id: str, title: str) -> SessionResponse:
        """修改会话标题并保存。

        参数：
        - `session_id`（str）：会话唯一标识；约束：无额外运行时限制。
        - `title`（str）：新标题；空白标题由领域实体拒绝；约束：无额外运行时限制。

        返回：
        - SessionResponse：会话标识、项目归属和会话元数据组成的响应。

        异常：
        - SessionNotFoundError：校验条件 `current is None` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        current = await self._repository.get(session_id)
        if current is None:
            raise SessionNotFoundError(session_id)
        updated = current.rename(title, now=self._clock())
        return _to_response(await self._repository.rename(updated))

    async def delete(self, session_id: str) -> None:
        """删除指定会话。

        参数：
        - `session_id`（str）：会话唯一标识；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - SessionNotFoundError：校验条件 `not await self._repository.delete(session_id)` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        if not await self._repository.delete(session_id):
            raise SessionNotFoundError(session_id)


def _to_response(
    value: Session, *, idempotency_replayed: bool = False
) -> SessionResponse:
    """将领域会话转换为应用层只读返回值。

    参数：
    - `value`（Session）：待解析、校验或转换的输入值；约束：无额外运行时限制。
    - `idempotency_replayed`（bool）：指示响应是否来自已保存的幂等结果；约束：可省略，默认值为 `False`。

    返回：
    - SessionResponse：会话标识、项目归属和会话元数据组成的响应。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return SessionResponse(
        id=value.id,
        title=value.title,
        active_run_id=value.active_run_id,
        created_at=value.created_at,
        updated_at=value.updated_at,
        project_id=value.project_id,
        last_activity_at=value.last_activity_at,
        idempotency_replayed=idempotency_replayed,
    )
