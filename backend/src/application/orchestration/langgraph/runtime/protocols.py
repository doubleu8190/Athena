"""Runtime adapter contracts for lifecycle-managed LangGraph resources."""

from __future__ import annotations

from typing import Any, Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver


class CheckpointerProvider(Protocol):
    """Provide a started saver without exposing its infrastructure owner."""

    def get_saver(self) -> BaseCheckpointSaver[Any]: ...


__all__ = ["CheckpointerProvider"]
