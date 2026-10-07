"""Ports implemented by the LangGraph infrastructure adapter."""

from __future__ import annotations

from typing import Any

from .root import RootGraphDependencies, build_root_graph, invoke_root_graph


class LangGraphRootGraph:
    """Concrete ``RootGraphPort`` backed by a compiled Root graph."""

    def __init__(self, *, dependencies: RootGraphDependencies | None = None, checkpointer: Any = None, graph: Any = None) -> None:
        self._graph = graph or build_root_graph(dependencies=dependencies, checkpointer=checkpointer)

    @property
    def graph(self) -> Any:
        return self._graph

    async def invoke(
        self,
        *,
        session_id: str,
        run_id: str,
        user_message: str,
        message_id: str = "",
        attachment_ids: list[str] | None = None,
        resume_value: dict[str, object] | None = None,
    ) -> object:
        return await invoke_root_graph(
            self._graph,
            session_id=session_id,
            run_id=run_id,
            user_message=user_message,
            message_id=message_id,
            attachment_ids=attachment_ids,
            resume_value=resume_value,
        )


__all__ = ["LangGraphRootGraph"]
