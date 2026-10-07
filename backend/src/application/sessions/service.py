"""会话用例服务。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from domain.sessions import Session, SessionRepository


class SessionNotFoundError(LookupError):
    """请求的会话不存在。"""


class SessionService:
    """协调会话用例，不感知 HTTP 或具体数据库。"""

    def __init__(
        self,
        repository: SessionRepository,
        *,
        id_factory: Callable[[], str],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._id_factory = id_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def create(self, title: str = "New Session") -> Session:
        """创建会话并持久化。

        参数：
            title: 会话标题；空白标题由领域实体拒绝。
        返回值：
            Session: 新创建的会话。
        异常：
            ValueError: 标题为空时抛出。
        """
        session = Session.create(
            session_id=self._id_factory(),
            title=title,
            now=self._clock(),
        )
        return await self._repository.create(session)

    async def list(self) -> list[Session]:
        """返回所有未删除会话。

        参数：无。
        返回值：
            list[Session]: 按 repository 约定排序的会话列表。
        异常：
            不主动抛出业务异常。
        """
        return await self._repository.list_all()

    async def get(self, session_id: str) -> Session:
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
        return session

    async def rename(self, session_id: str, title: str) -> Session:
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
        current = await self.get(session_id)
        updated = current.rename(title, now=self._clock())
        return await self._repository.save(updated)

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
