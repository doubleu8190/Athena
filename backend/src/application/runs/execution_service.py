"""执行已领取的 Run，并收敛其生命周期状态。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from time import perf_counter
from domain.leases import LeaseGrant

from application.runs.ports import RunExecutorPort
from domain.events import ApplicationEvent, EventDurability, EventPort, EventType
from domain.memory import MemoryJobPort
from domain.runs import (
    CommandType,
    RunCommand,
    RunLifecyclePort,
    RunQueryPort,
    RunResumePayload,
    RunStartPayload,
    RunStatus,
    RootWaitCheckpoint,
)


class RunExecutionService:
    """执行 Root 的启动或恢复调用，并通过 Run 生命周期端口协调状态。
    """

    def __init__(
        self,
        lifecycle: RunLifecyclePort,
        executor: RunExecutorPort,
        events: EventPort,
        memory_jobs: MemoryJobPort | None = None,
        run_query: RunQueryPort | None = None,
    ) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `lifecycle`（RunLifecyclePort）：启动、停止和查询 Run 生命周期的应用端口；约束：无额外运行时限制。
        - `executor`（RunExecutorPort）：执行已提交 Run 的运行时端口；约束：无额外运行时限制。
        - `events`（EventPort）：用于读取、写入或推送的事件存储依赖；约束：无额外运行时限制。
        - `memory_jobs`（MemoryJobPort | None）：提交记忆处理后台作业的依赖；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `run_query`（RunQueryPort | None）：读取 Run 当前状态和请求信息的查询依赖；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._lifecycle = lifecycle
        self._executor = executor
        self._events = events
        self._memory_jobs = memory_jobs
        self._run_query = run_query
        self._run_started: dict[str, tuple[datetime, float]] = {}
        self._user_message_ids_by_run_id: dict[str, str] = {}

    async def execute(self, invocation: RunCommand) -> dict[str, object] | None:
        """执行 Run 命令并返回 Root 运行结果。

        参数：
        - `invocation`（RunCommand）：当前 Run 命令及其执行身份、输入和恢复信息；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - dict[str, object] | None：Run 执行结果字段；Run 已被拒绝或无需启动时返回 None。

        异常：
        - TypeError：校验条件 `invocation.command_type not in {CommandType.RUN_START, CommandType.RUN_RESUME}` 成立时（异常说明：Run execution accepts only start or resume invocations）。
        - 未列出的依赖异常按当前实现传播。
        """
        if not invocation.run_id:
            return None
        if invocation.command_type not in {CommandType.RUN_START, CommandType.RUN_RESUME}:
            raise TypeError("Run execution accepts only start or resume invocations")
        return await self._execute_run(invocation, invocation.run_id)

    async def interrupt(self, run_id: str) -> None:
        """通知活动 Root 调用停止，并等待其完成取消。

        参数：
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        await self._executor.cancel(run_id)

    async def _execute_run(
        self, invocation: RunCommand, run_id: str
    ) -> dict[str, object] | None:
        """执行运行。

        参数：
        - `invocation`（RunCommand）：当前 Run 命令及其执行身份、输入和恢复信息；约束：须通过方法内校验；失败条件见下方异常说明。
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。

        返回：
        - dict[str, object] | None：Root 执行结果字段；Run 已被取消或终态阻止执行时返回 None。

        异常：
        - RuntimeError：校验条件 `invocation.lease is None` 成立时（异常说明：root_execution_lease_missing）。
        - asyncio.CancelledError（重新抛出捕获的异常）：校验条件 `捕获 asyncio.CancelledError 后` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        if invocation.lease is None:
            raise RuntimeError("root_execution_lease_missing")
        # 先用租约栅栏推进生命周期；状态迁移失败时不得启动 Root 图。
        if not await self._lifecycle.update_status(
            run_id, RunStatus.RUNNING, lease=invocation.lease
        ):
            return None
        if run_id not in self._run_started:
            self._run_started[run_id] = (datetime.now(timezone.utc), perf_counter())
        if isinstance(invocation.payload, RunStartPayload):
            self._user_message_ids_by_run_id[run_id] = invocation.payload.message_id

        event_type = (
            EventType.RUN_RESUMED
            if invocation.command_type == CommandType.RUN_RESUME
            else EventType.RUN_STARTED
        )
        await self._publish(
            invocation,
            event_type,
            payload=self._run_payload(
                invocation, "running", started_at=self._run_started[run_id][0]
            ),
        )

        try:
            outcome = await self._executor.execute(invocation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.fail(
                invocation,
                {"code": "run_failed", "message": str(exc) or type(exc).__name__},
            )
            return None

        # Root 等待 Worker 时保留检查点并发布唤醒事件，不把等待误判为完成。
        if outcome.root_wait is not None:
            payload = self._run_payload(invocation, "waiting")
            await self._arm_root_wait(run_id, outcome.root_wait, invocation.lease, payload)
            if self._lifecycle.writes_root_wait_event:
                # 非空序号只用于唤醒订阅者；订阅者从数据库按顺序补读。
                await self._events.publish_realtime(ApplicationEvent(
                    event_type=EventType.RUN_ROOT_SUSPENDED,
                    durability=EventDurability.DURABLE,
                    session_id=invocation.session_id,
                    session_seq=0,
                    run_id=run_id,
                    payload=payload,
                ))
            else:
                await self._publish(invocation, EventType.RUN_ROOT_SUSPENDED, payload=payload)
            return None
        if outcome.error is not None:
            await self.fail(invocation, outcome.error)
            return None

        result = outcome.result or {}
        if invocation.lease is None:
            raise RuntimeError("root_execution_lease_missing")
        # 终态迁移以租约身份为准；并发停止已抢先完成时，放弃本次迟到结果。
        if not await self._lifecycle.complete(run_id, result, lease=invocation.lease):
            current = await self._run_query.get(run_id) if self._run_query else None
            status = current.status if current is not None else "unknown"
            if status == RunStatus.STOPPING.value:
                return None
            self._clear_local_state(run_id)
            return {"status": str(getattr(status, "value", status))}

        if not self._lifecycle.writes_terminal_artifacts_to_outbox:
            await self._publish_result_message(invocation, result)
            await self._enqueue_memory(invocation, result)
            await self._publish(
                invocation,
                EventType.RUN_COMPLETED,
                payload=self._run_payload(
                    invocation, "completed", summary="运行已完成"
                ),
            )
        self._clear_local_state(run_id)
        return result

    async def _arm_root_wait(
        self, run_id: str, root_wait: RootWaitCheckpoint, lease: LeaseGrant,
        wait_payload: dict[str, object],
    ) -> None:
        """持久化 Root 等待身份，并将 Run 转入等待轮次状态。

        参数：
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：须通过方法内校验；失败条件见下方异常说明。
        - `root_wait`（RootWaitCheckpoint）：Root 等待 Worker 轮次完成时保存的恢复信息；约束：须通过方法内校验；失败条件见下方异常说明。
        - `lease`（LeaseGrant）：当前后台作业持有的租约授权及执行代次；约束：须通过方法内校验；失败条件见下方异常说明。
        - `wait_payload`（dict[str, object]）：用于重建 Root 等待身份的持久化内容；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - ValueError：校验条件 `root_wait.run_id != run_id` 成立时（异常说明：root_wait_run_identity_mismatch）。
        - RuntimeError：校验条件 `not await self._lifecycle.arm_root_wait(run_id, plan_id=root_wait.plan_id, round_id=root_wait.round_id, plan_version=root_wait.plan_version, interrupt_id=root_wait.interrupt_id, checkpoint_id=root_wait.checkpoint_id, lease=lease, wait_payload=wait_payload)` 成立时（异常说明：root_wait_checkpoint_not_armed）。
        - 未列出的依赖异常按当前实现传播。
        """
        if root_wait.run_id != run_id:
            raise ValueError("root_wait_run_identity_mismatch")
        if not await self._lifecycle.arm_root_wait(
            run_id,
            plan_id=root_wait.plan_id,
            round_id=root_wait.round_id,
            plan_version=root_wait.plan_version,
            interrupt_id=root_wait.interrupt_id,
            checkpoint_id=root_wait.checkpoint_id,
            lease=lease,
            wait_payload=wait_payload,
        ):
            raise RuntimeError("root_wait_checkpoint_not_armed")

    async def fail(self, invocation: RunCommand, error: dict[str, object]) -> None:
        """将 Run 标记为失败并发布关联状态事件。

        参数：
        - `invocation`（RunCommand）：当前 Run 命令及其执行身份、输入和恢复信息；约束：须通过方法内校验；失败条件见下方异常说明。
        - `error`（dict[str, object]）：操作失败时捕获的异常或错误说明；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - RuntimeError：校验条件 `invocation.lease is None` 成立时（异常说明：root_execution_lease_missing）。
        - 未列出的依赖异常按当前实现传播。
        """
        run_id = invocation.run_id
        if run_id is None:
            return
        if invocation.lease is None:
            raise RuntimeError("root_execution_lease_missing")
        changed = await self._lifecycle.fail(
            run_id, error, lease=invocation.lease
        )
        if changed and not self._lifecycle.writes_terminal_artifacts_to_outbox:
            await self._publish(
                invocation,
                EventType.RUN_FAILED,
                payload=self._run_payload(invocation, "failed", error=error),
            )
        self._clear_local_state(run_id)

    def _clear_local_state(self, run_id: str) -> None:
        """清除指定 Run 在当前进程中的活动调用和租约状态。

        参数：
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._run_started.pop(run_id, None)
        self._user_message_ids_by_run_id.pop(run_id, None)

    async def _publish(
        self,
        invocation: RunCommand,
        event_type: EventType,
        *,
        payload: dict[str, object] | None = None,
    ) -> None:
        """发布绑定当前 Run 的事件。

        参数：
        - `invocation`（RunCommand）：当前 Run 命令及其执行身份、输入和恢复信息；约束：无额外运行时限制。
        - `event_type`（EventType）：要发布的事件类型名称；约束：无额外运行时限制。
        - `payload`（dict[str, object] | None）：随事件或消息传递的结构化内容；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        await self._events.publish(
            ApplicationEvent(
                event_type=event_type,
                durability=EventDurability.DURABLE,
                session_id=invocation.session_id,
                run_id=invocation.run_id,
                payload=payload or {},
            )
        )

    def _run_payload(
        self,
        invocation: RunCommand,
        status: str,
        *,
        started_at: datetime | None = None,
        summary: str | None = None,
        error: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """构造包含状态、耗时、摘要和错误信息的 Run 事件载荷。

        参数：
        - `invocation`（RunCommand）：当前 Run 命令及其执行身份、输入和恢复信息；约束：无额外运行时限制。
        - `status`（str）：当前操作或记录的状态值；约束：无额外运行时限制。
        - `started_at`（datetime | None）：操作开始时间，用于记录运行时间信息；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `summary`（str | None）：面向用户或后续流程的简要结果说明；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `error`（dict[str, object] | None）：操作失败时捕获的异常或错误说明；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - dict[str, object]：包含 Run 状态、耗时、摘要和错误信息的事件字段映射。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        run_id = invocation.run_id or ""
        payload: dict[str, object] = {
            "activity_id": f"run:{run_id}",
            "activity_type": "run",
            "name": "run",
            "status": status,
        }
        state = self._run_started.get(run_id)
        start_time = started_at or (state[0] if state else None)
        if start_time is not None:
            payload["started_at"] = start_time.isoformat()
        if status in {"completed", "failed", "cancelled"} and state:
            payload["duration_ms"] = int((perf_counter() - state[1]) * 1000)
            payload["completed_at"] = datetime.now(timezone.utc).isoformat()
        if summary:
            payload["summary"] = summary
        if error:
            safe_error = {
                key: value for key, value in error.items()
                if key in {"code", "stage", "retryable"}
            }
            safe_error["message"] = str(error.get("code", "run_failed"))
            payload["error"] = safe_error
        return payload

    async def _enqueue_memory(
        self, invocation: RunCommand, result: dict[str, object]
    ) -> None:
        """将 Run 结果转换为记忆处理作业并提交。

        参数：
        - `invocation`（RunCommand）：当前 Run 命令及其执行身份、输入和恢复信息；约束：无额外运行时限制。
        - `result`（dict[str, object]）：当前阶段生成的领域结果或操作回执；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        if self._memory_jobs is None or not invocation.run_id:
            return
        if not isinstance(invocation.payload, RunStartPayload):
            return
        user_text = invocation.payload.message.strip()
        if not user_text:
            return
        content = _result_content(result)
        try:
            await self._memory_jobs.enqueue(
                turn_id=invocation.run_id,
                session_id=invocation.session_id,
                user_text=user_text,
                assistant_text=content,
            )
        except Exception:
            return

    async def _publish_result_message(
        self, invocation: RunCommand, result: dict[str, object]
    ) -> None:
        """将 Root 最终回答持久化为会话消息。

        参数：
        - `invocation`（RunCommand）：当前 Run 命令及其执行身份、输入和恢复信息；约束：无额外运行时限制。
        - `result`（dict[str, object]）：当前阶段生成的领域结果或操作回执；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - ValueError：校验条件 `not isinstance(answer_stream, dict)` 成立时（异常说明：answer_stream_metadata_missing）。
        - 未列出的依赖异常按当前实现传播。
        """
        if not invocation.run_id:
            return
        if isinstance(invocation.payload, RunStartPayload):
            reply_to_message_id = invocation.payload.message_id
        elif invocation.command_type == CommandType.RUN_RESUME:
            reply_to_message_id = self._user_message_ids_by_run_id.get(invocation.run_id)
        else:
            return
        if not reply_to_message_id:
            return
        content = _result_content(result)
        if not content:
            return
        answer_stream = result.get("answer_stream")
        if not isinstance(answer_stream, dict):
            nested = result.get("result")
            answer_stream = nested.get("answer_stream") if isinstance(nested, dict) else None
        if not isinstance(answer_stream, dict):
            raise ValueError("answer_stream_metadata_missing")
        stream_id = str(answer_stream["stream_id"])
        # 以 Run 派生助手消息 ID，使恢复或重复完成事件写入同一条会话消息。
        assistant_message_id = f"assistant:{invocation.run_id}"
        await self._events.publish(
            ApplicationEvent(
                event_type=EventType.MESSAGE_COMPLETED,
                durability=EventDurability.DURABLE,
                session_id=invocation.session_id,
                run_id=invocation.run_id,
                message_id=assistant_message_id,
                stream_id=stream_id,
                stream_type="answer",
                chunk_id=int(answer_stream["last_chunk_id"]),
                is_complete=True,
                transition_id=f"answer:{stream_id}:completed",
                payload={
                    "content": content,
                    "last_chunk_id": int(answer_stream["last_chunk_id"]),
                    "reply_to_message_id": reply_to_message_id,
                    "kind": result.get("kind", "final"),
                    "started_at": answer_stream["started_at"],
                    "status": "completed",
                },
            )
        )


def _result_content(result: dict[str, object]) -> str:
    """从执行结果映射中提取可持久化的文本正文。

    参数：
    - `result`（dict[str, object]）：当前阶段生成的领域结果或操作回执；约束：无额外运行时限制。

    返回：
    - str：从执行结果中提取并规范化的可持久化正文。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    value = result.get("content")
    if isinstance(value, str):
        return value
    nested = result.get("result")
    if isinstance(nested, dict) and isinstance(nested.get("content"), str):
        return str(nested["content"])
    return ""


__all__ = ["RunExecutionService"]
