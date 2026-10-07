"""Root 编排 facade，隔离 LangGraph graph 组装。"""

from __future__ import annotations

from domain.orchestration import RootGraphPort


class RootExecutionService:
    """Root 只通过 graph port 启动或恢复一次执行。"""

    def __init__(self, graph: RootGraphPort) -> None:
        self._graph = graph

    async def execute(
        self,
        *,
        session_id: str,
        run_id: str,
        user_message: str,
        message_id: str = "",
        attachment_ids: list[str] | None = None,
    ) -> object:
        return await self._graph.invoke(
            session_id=session_id,
            run_id=run_id,
            user_message=user_message,
            message_id=message_id,
            attachment_ids=attachment_ids,
        )

    async def resume(
        self,
        *,
        session_id: str,
        run_id: str,
        user_message: str,
        resume_value: dict[str, object],
    ) -> object:
        return await self._graph.invoke(
            session_id=session_id,
            run_id=run_id,
            user_message=user_message,
            resume_value=resume_value,
        )


__all__ = ["RootExecutionService"]
