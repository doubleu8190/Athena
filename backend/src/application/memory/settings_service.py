"""会话级记忆读取设置用例。"""

from __future__ import annotations

from domain.memory import SessionMemorySettings, SessionMemorySettingsPort
from domain.sessions import SessionRepository


class SessionMemorySettingsService:
    """验证会话存在后读取或保存记忆读取设置。"""

    def __init__(
        self,
        sessions: SessionRepository,
        settings: SessionMemorySettingsPort,
    ) -> None:
        """保存会话目录和设置仓储端口。

        参数：
        - `sessions`（SessionRepository）：读取会话身份和状态的仓储依赖；约束：无额外运行时限制。
        - `settings`（SessionMemorySettingsPort）：当前会话的记忆读取设置；None 表示未提供会话级覆盖；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._sessions = sessions
        self._settings = settings

    async def get(self, session_id: str) -> SessionMemorySettings:
        """获取指定会话的设置；首次读取使用默认值。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。

        返回：
        - SessionMemorySettings：指定会话当前的记忆读取设置及所属项目标识。

        异常：
        - LookupError：校验条件 `session is None` 成立时（异常说明：session_not_found）。
        - 未列出的依赖异常按当前实现传播。
        """
        session = await self._sessions.get(session_id)
        if session is None:
            raise LookupError("session_not_found")
        value = await self._settings.get(session_id)
        return SessionMemorySettings(
            session_id=value.session_id,
            use_memories=value.use_memories,
            project_id=session.project_id,
        )

    async def update(
        self,
        session_id: str,
        *,
        use_memories: bool,
    ) -> SessionMemorySettings:
        """保存记忆读取设置。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。
        - `use_memories`（bool）：控制当前会话是否允许读取记忆；约束：无额外运行时限制。

        返回：
        - SessionMemorySettings：保存后且包含会话项目归属的记忆读取设置。

        异常：
        - LookupError：校验条件 `session is None` 成立时（异常说明：session_not_found）。
        - 未列出的依赖异常按当前实现传播。
        """
        session = await self._sessions.get(session_id)
        if session is None:
            raise LookupError("session_not_found")
        saved = await self._settings.save(
            SessionMemorySettings(
                session_id=session_id,
                use_memories=use_memories,
            )
        )
        return SessionMemorySettings(
            session_id=saved.session_id,
            use_memories=saved.use_memories,
            project_id=session.project_id,
        )


__all__ = ["SessionMemorySettingsService"]
