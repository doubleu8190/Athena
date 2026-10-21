"""LangGraph-backed implementation of the Root graph port."""

from __future__ import annotations

from typing import Any

from domain.events import EventPort
from domain.orchestration.ports import RootGraphPort

from application.orchestration.protocols import RootWorkflowDependencies, RootWorkflowPort

from ..graphs.root import build_root_graph, invoke_root_graph


class LangGraphRootGraph(RootGraphPort):
    """Concrete ``RootGraphPort`` backed by a compiled Root graph."""

    def __init__(
        self,
        *,
        workflow: RootWorkflowPort,
        dependencies: RootWorkflowDependencies,
        checkpointer: Any = None,
    ) -> None:
        self._workflow = workflow
        self._dependencies = dependencies
        self._graph = build_root_graph(
            workflow=self._workflow,
            dependencies=self._dependencies,
            checkpointer=checkpointer,
        )

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
