"""Process-local fanout for live evaluation progress events."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from application.evaluation.ports import EvaluationEventSink


class EvaluationProgressHub(EvaluationEventSink):
    def __init__(self) -> None:
        self._queues: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._history: dict[str, list[dict[str, Any]]] = defaultdict(list)

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        run_id = str(payload.get("evaluation_run_id", ""))
        if not run_id:
            return
        event = {"event_type": event_type, "payload": dict(payload)}
        history = self._history[run_id]
        history.append(event)
        del history[:-256]
        for queue in tuple(self._queues.get(run_id, ())):
            queue.put_nowait(event)

    async def subscribe(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._queues[run_id].add(queue)
        for event in self._history.get(run_id, ()):
            queue.put_nowait(event)
        try:
            while True:
                yield await queue.get()
        finally:
            listeners = self._queues.get(run_id)
            if listeners is not None:
                listeners.discard(queue)
                if not listeners:
                    self._queues.pop(run_id, None)


__all__ = ["EvaluationProgressHub"]
