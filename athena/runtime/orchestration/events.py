"""中心编排的持久化事件发布器。"""

from __future__ import annotations

from athena.contracts.events import ApplicationEvent, EventDurability, EventType
from athena.contracts.ports import EventPublisherPort


class OrchestrationEventPublisher:
    """为计划、任务和汇总生命周期发布 Durable 事件。"""

    def __init__(self, publisher: EventPublisherPort) -> None:
        """绑定底层事件发布器。

        参数：
            publisher: Runtime 事件发布端口。

        返回值：
            None。

        异常：
            不主动抛出业务异常。
        """
        self._publisher = publisher

    async def publish_plan(
        self,
        event_type: EventType,
        *,
        session_id: str,
        plan_id: str,
        run_id: str,
        payload: dict[str, object],
    ) -> None:
        """发布计划生命周期事件。

        参数：
            event_type: plan.* 事件类型。
            session_id: 所属会话。
            plan_id: 执行计划标识。
            run_id: Root Run 标识。
            payload: 计划事件内容。

        返回值：
            None。

        异常：
            底层事件发布失败时传播对应异常。
        """
        await self._publisher.publish(
            ApplicationEvent(
                event_type=event_type,
                durability=EventDurability.DURABLE,
                session_id=session_id,
                run_id=run_id,
                transition_id=f"plan:{plan_id}:{event_type.value}",
                payload={"plan_id": plan_id, **payload},
            )
        )

    async def publish_task(
        self,
        event_type: EventType,
        *,
        session_id: str,
        plan_id: str,
        task_id: str,
        run_id: str,
        worker_run_id: str | None = None,
        attempt: int | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        """发布任务生命周期事件。

        参数：
            event_type: task.* 事件类型。
            session_id: 所属会话。
            plan_id: 执行计划标识。
            task_id: 稳定任务标识。
            run_id: Root Run 标识。
            worker_run_id: 当前 Worker 尝试标识。
            attempt: 当前尝试序号。
            payload: 附加事件内容。

        返回值：
            None。

        异常：
            底层事件发布失败时传播对应异常。
        """
        event_payload: dict[str, object] = {
            "plan_id": plan_id,
            "task_id": task_id,
        }
        if worker_run_id is not None:
            event_payload["worker_run_id"] = worker_run_id
        if attempt is not None:
            event_payload["attempt"] = attempt
        if payload:
            event_payload.update(payload)
        if "title" not in event_payload:
            event_payload["title"] = task_id
        await self._publisher.publish(
            ApplicationEvent(
                event_type=event_type,
                durability=EventDurability.DURABLE,
                session_id=session_id,
                run_id=run_id,
                parent_run_id=run_id,
                transition_id=f"task:{task_id}:{event_type.value}:{attempt or 0}",
                payload=event_payload,
            )
        )

    async def publish_synthesis(
        self,
        event_type: EventType,
        *,
        session_id: str,
        plan_id: str,
        run_id: str,
        payload: dict[str, object],
    ) -> None:
        """发布汇总生命周期事件。

        参数：
            event_type: synthesis.* 事件类型。
            session_id: 所属会话。
            plan_id: 执行计划标识。
            run_id: Root Run 标识。
            payload: 汇总事件内容。

        返回值：
            None。

        异常：
            底层事件发布失败时传播对应异常。
        """
        await self._publisher.publish(
            ApplicationEvent(
                event_type=event_type,
                durability=EventDurability.DURABLE,
                session_id=session_id,
                run_id=run_id,
                transition_id=f"synthesis:{plan_id}:{event_type.value}",
                payload={"plan_id": plan_id, **payload},
            )
        )
