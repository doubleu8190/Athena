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
            project_id: 新会话所属的未删除项目。
            creation_request_id: 稳定请求 ID，用于网络重试幂等。
            title: 初始会话标题；正常聊天使用默认占位标题。
        返回值：
            Session: 新创建的会话。
        异常：
            ValueError: 标题为空时抛出。
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
        return _to_response(
            await self._repository.create_idempotent(
                session, creation_request_id=creation_request_id
            )
        )

    async def list_sessions(
        self, *, project_id: str | None = None
    ) -> list[SessionResponse]:
        """返回所有未删除会话。

        参数：无。
        返回值：
            list[Session]: 按 repository 约定排序的会话列表。
        异常：
            不主动抛出业务异常。
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
            session_id: 会话唯一标识。
        返回值：
            Session: 找到的会话。
        异常：
            SessionNotFoundError: 会话不存在时抛出。
        """
        session = await self._repository.get(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)
        return _to_response(session)

    async def rename(self, session_id: str, title: str) -> SessionResponse:
        """修改会话标题并保存。

        参数：
            session_id: 会话唯一标识。
            title: 新标题；空白标题由领域实体拒绝。
        返回值：
            Session: 保存后的会话。
        异常：
            SessionNotFoundError: 会话不存在时抛出。
            ValueError: 标题为空时抛出。
        """
        current = await self._repository.get(session_id)
        if current is None:
            raise SessionNotFoundError(session_id)
        updated = current.rename(title, now=self._clock())
        return _to_response(await self._repository.rename(updated))

    async def delete(self, session_id: str) -> None:
        """删除指定会话。

        参数：
            session_id: 会话唯一标识。
        返回值：
            None: 删除成功。
        异常：
            SessionNotFoundError: 会话不存在时抛出。
        """
        if not await self._repository.delete(session_id):
            raise SessionNotFoundError(session_id)


def _to_response(value: Session) -> SessionResponse:
    """将领域会话转换为应用层只读返回值。"""
    return SessionResponse(
        id=value.id,
        title=value.title,
        status=value.status.value,
        run_id=value.run_id,
        created_at=value.created_at,
        updated_at=value.updated_at,
        project_id=value.project_id,
        last_activity_at=value.last_activity_at,
    )
