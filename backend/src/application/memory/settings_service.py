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
        """保存会话目录和设置仓储端口。"""
        self._sessions = sessions
        self._settings = settings

    async def get(self, session_id: str) -> SessionMemorySettings:
        """获取指定会话的设置；首次读取使用默认值。"""
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
        """保存记忆读取设置。"""
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
