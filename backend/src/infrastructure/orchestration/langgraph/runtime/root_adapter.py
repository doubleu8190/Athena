"""Ports implemented by the LangGraph infrastructure adapter."""

from __future__ import annotations

from typing import Any

from domain.orchestration.ports import RootGraphPort

from application.orchestration.protocols import RootWorkflowDependencies, RootWorkflowPort

from ..graphs.root import build_root_graph, invoke_root_graph


class LangGraphRootGraph(RootGraphPort):
    """Concrete ``RootGraphPort`` backed by a compiled Root graph."""

    def __init__(
        self,
        *,
        workflow: RootWorkflowPort | None = None,
        dependencies: RootWorkflowDependencies | None = None,
        event_publisher: Any | None = None,
        checkpointer: Any = None,
        graph: Any = None,
    ) -> None:
        self._workflow = workflow
        self._dependencies = dependencies
        self._event_publisher = event_publisher
        self._checkpointer = checkpointer
        self._graph = graph

    def _saver(self) -> Any:
        if self._checkpointer is None:
            return None
        getter = getattr(self._checkpointer, "get_saver", None)
        return getter() if getter is not None else self._checkpointer

    async def start(self) -> None:
        if self._graph is None:
            self._graph = build_root_graph(
                workflow=self._workflow,
                dependencies=self._dependencies,
                event_publisher=self._event_publisher,
                checkpointer=self._saver(),
            )

    async def stop(self) -> None:
        return None

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
        stop_signal: object | None = None,
    ) -> object:
        if self._graph is None:
            await self.start()
        return await invoke_root_graph(
            self._graph,
            session_id=session_id,
            run_id=run_id,
            user_message=user_message,
            message_id=message_id,
            attachment_ids=attachment_ids,
            resume_value=resume_value,
            stop_signal=stop_signal,
        )


__all__ = ["LangGraphRootGraph"]
