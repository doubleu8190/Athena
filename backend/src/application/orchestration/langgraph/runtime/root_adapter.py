"""LangGraph-backed implementation of the Root graph port."""

from __future__ import annotations

from collections.abc import Mapping
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
        self._checkpointer = checkpointer
        self._graph: Any | None = None

    def _compiled_graph(self) -> Any:
        """Compile lazily so lifecycle-managed PostgreSQL savers can start first."""
        if self._graph is None:
            saver = self._checkpointer
            getter = getattr(saver, "get_saver", None)
            if callable(getter):
                saver = getter()
            self._graph = build_root_graph(
                workflow=self._workflow,
                dependencies=self._dependencies,
                checkpointer=saver,
            )
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
        return await invoke_root_graph(
            self._compiled_graph(),
            session_id=session_id,
            run_id=run_id,
            user_message=user_message,
            message_id=message_id,
            attachment_ids=attachment_ids,
            resume_value=resume_value,
            stop_signal=stop_signal,
        )

    async def has_pending_round_wait(
        self,
        *,
        run_id: str,
        plan_id: str,
        round_id: str,
        plan_version: int,
        interrupt_id: str,
        checkpoint_id: str,
    ) -> bool:
        """检查当前 checkpoint 是否正等待指定版本和轮次的中断。"""
        snapshot = await self._compiled_graph().aget_state(
            {"configurable": {"thread_id": f"run:{run_id}"}}
        )
        snapshot_config = getattr(snapshot, "config", {})
        configurable = (
            snapshot_config.get("configurable", {})
            if isinstance(snapshot_config, Mapping)
            else {}
        )
        if (
            not isinstance(configurable, Mapping)
            or configurable.get("checkpoint_id") != checkpoint_id
        ):
            return False
        for task in getattr(snapshot, "tasks", ()):
            for pending in getattr(task, "interrupts", ()):
                value = getattr(pending, "value", None)
                if (
                    getattr(pending, "id", None) == interrupt_id
                    and isinstance(value, Mapping)
                    and value.get("type") == "round_wait"
                    and value.get("run_id") == run_id
                    and value.get("plan_id") == plan_id
                    and value.get("round_id") == round_id
                    and value.get("plan_version") == plan_version
                ):
                    return True
        return False


__all__ = ["LangGraphRootGraph"]
