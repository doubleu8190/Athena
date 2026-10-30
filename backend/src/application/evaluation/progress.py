"""在进程内向订阅方分发实时评估进度事件。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from application.evaluation.ports import EvaluationEventSink


class EvaluationProgressHub(EvaluationEventSink):
    """在进程内按评估运行分发实时进度事件。
    """
    def __init__(self) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - 无参数。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._queues: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._history: dict[str, list[dict[str, Any]]] = defaultdict(list)

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        """将评估进度事件发送给当前运行的订阅方。

        参数：
        - `event_type`（str）：要发布的事件类型名称；约束：无额外运行时限制。
        - `payload`（dict[str, Any]）：随事件或消息传递的结构化内容；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
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
        """订阅指定评估运行的进度事件。

        参数：
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。

        返回：
        - AsyncIterator[dict[str, Any]]：按顺序异步或同步产出的结果流。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
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
