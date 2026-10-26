"""会话级记忆设置用例。"""

from __future__ import annotations

from domain.memory import SessionMemorySettings, SessionMemorySettingsPort
from domain.sessions import SessionRepository


class SessionMemorySettingsService:
    """验证会话存在后读取或保存两项独立的记忆开关。"""

    def __init__(
        self,
        sessions: SessionRepository,
        settings: SessionMemorySettingsPort,
    ) -> None:
        """保存会话目录和设置仓储端口。"""
        self._sessions = sessions
        self._settings = settings

    async def get(self, session_id: str) -> SessionMemorySettings:
        """获取指定会话的设置；首次读取使用默认值。"""
        if await self._sessions.get(session_id) is None:
            raise LookupError("session_not_found")
        return await self._settings.get(session_id)

    async def update(
        self,
        session_id: str,
        *,
        use_memories: bool,
        generate_memories: bool,
        project_id: str | None = None,
    ) -> SessionMemorySettings:
        """完整替换读取和生成开关。"""
        if await self._sessions.get(session_id) is None:
            raise LookupError("session_not_found")
        return await self._settings.save(
            SessionMemorySettings(
                session_id=session_id,
                use_memories=use_memories,
                generate_memories=generate_memories,
                project_id=project_id,
            )
        )


__all__ = ["SessionMemorySettingsService"]
