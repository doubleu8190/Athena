"""通过订阅式唤醒将持久化命令交给 LangGraph 执行的消费者。"""

from __future__ import annotations

import asyncio
from typing import Any

from athena.contracts.commands import CommandType
from athena.contracts.events import ApplicationEvent, EventDurability, EventType
from athena.contracts.ports import AgentCommandRecord, AgentStorePort
from athena.contracts.statuses import (
    AgentApprovalDecision,
    AgentCommandStatus,
    AgentRunStatus,
    StreamSnapshotStatus,
)
from athena.models.json_models import CommandPayload
from athena.core.memory.memory import MemoryManager
from .command_notifications import CommandNotifier
from .langgraph_graph import invoke_graph
from .cancellation import CancellationRegistry
from langgraph.graph.state import CompiledStateGraph


class CommandConsumer:
    """消费持久化命令并驱动运行、审批和记忆。"""

    def __init__(
        self,
        store: AgentStorePort,
        *,
        poll_interval: float = 0.25,
        graph: CompiledStateGraph,
        notifier: CommandNotifier | None = None,
        cancellation: CancellationRegistry | None = None,
        memory_manager: MemoryManager,
    ) -> None:
        """创建命令消费者。

        参数:
            store (AgentStorePort): 命令、事件和运行状态存储。
            poll_interval (float): 无命令时的轮询间隔，单位为秒，必须为非负数。
            graph (CompiledStateGraph): 已编译的 LangGraph 图；处理消息命令时必须可调用。
            notifier (CommandNotifier | None): Command 提交后的进程内唤醒通知器。
            cancellation (CancellationRegistry | None): 可选取消注册表。
            memory_manager (MemoryManager): 处理主动保存记忆命令的服务。
        返回值:
            None: 消费循环尚未启动。
        异常:
            不抛出业务异常；依赖异常在消费循环中转换为命令失败状态。
        """
        self.store = store
        self.poll_interval = poll_interval
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.graph = graph
        self.notifier = notifier
        self.cancellation = cancellation or CancellationRegistry()
        self.memory_manager = memory_manager

    async def start(self) -> None:
        """启动后台命令消费任务。

        参数:
            无。
        返回值:
            None: 后台任务已创建。
        异常:
            存储读取失败时传播底层异常。
        """
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="athena-command-consumer")

    async def stop(self) -> None:
        """请求停止消费并等待后台任务退出。

        参数:
            无。
        返回值:
            None: 消费任务已停止；重复调用保持幂等。
        异常:
            后台任务抛出的未处理异常会原样传播。
        """
        self._stop.set()
        if self.notifier is not None:
            await self.notifier.notify()
        if self._task:
            await self._task
            self._task = None

    async def run(self) -> None:
        """持续领取并执行命令，直到收到停止信号。

        参数:
            无。
        返回值:
            None: 停止事件设置后退出循环。
        异常:
            ``asyncio.CancelledError`` 会继续向上传播；普通命令异常会记录为失败状态。
        """
        while not self._stop.is_set():
            command = await self.store.claim_pending()
            if command is None:
                await self._wait_for_command()
                continue
            try:
                await self._process_command(command)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._handle_command_error(command, exc)

    async def _wait_for_command(self) -> None:
        """等待新命令或停止信号。"""
        if self.notifier is not None:
            await self.notifier.wait()
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
        except asyncio.TimeoutError:
            pass

    async def _process_command(self, command: AgentCommandRecord) -> None:
        """校验命令并分派到对应的命令处理器。"""
        if command.schema_version != 1:
            await self.store.complete(
                command.command_id,
                status=AgentCommandStatus.REJECTED,
                error={
                    "code": "unsupported_schema_version",
                    "schema_version": command.schema_version,
                },
            )
            return

        payload = CommandPayload.model_validate_json(command.payload_json)
        handlers = {
            CommandType.RUN_PAUSE: self._handle_run_pause,
            CommandType.RUN_RESUME: self._handle_run_resume,
            CommandType.RUN_CANCEL: self._handle_run_cancel,
            CommandType.APPROVAL_RESOLVE: self._handle_approval,
            CommandType.APPROVAL_CANCEL: self._handle_approval,
            CommandType.MEMORY_CREATE: self._handle_memory_create,
            CommandType.MESSAGE_SUBMIT: self._handle_message_submit,
        }
        handler = handlers.get(command.command_type)
        if handler is None:
            await self.store.complete(
                command.command_id,
                status=AgentCommandStatus.REJECTED,
                error={
                    "code": "unsupported_command",
                    "command_type": command.command_type,
                },
            )
            return
        await handler(command, payload)

    async def _handle_run_pause(
        self, command: AgentCommandRecord, payload: CommandPayload
    ) -> None:
        await self._handle_run_state_change(
            command,
            status=AgentRunStatus.PAUSED,
            event_type=EventType.RUN_PAUSED,
            pause=True,
        )

    async def _handle_run_resume(
        self, command: AgentCommandRecord, payload: CommandPayload
    ) -> None:
        await self._handle_run_state_change(
            command,
            status=AgentRunStatus.RUNNING,
            event_type=EventType.RUN_RESUMED,
            clear_pause=True,
        )

    async def _handle_run_state_change(
        self,
        command: AgentCommandRecord,
        *,
        status: AgentRunStatus,
        event_type: EventType,
        pause: bool = False,
        clear_pause: bool = False,
    ) -> None:
        if command.run_id:
            await self.store.update_run_control(
                command.run_id,
                pause=pause,
                clear_pause=clear_pause,
                status=status,
            )
        await self._publish_run_event(command, event_type)
        await self.store.complete(
            command.command_id,
            status=AgentCommandStatus.SUCCEEDED,
            result={"status": status.value},
        )

    async def _handle_run_cancel(
        self, command: AgentCommandRecord, payload: CommandPayload
    ) -> None:
        if command.run_id:
            await self.cancellation.request(command.run_id)
            await self.store.update_run_control(
                command.run_id,
                cancel=True,
                status=AgentRunStatus.CANCEL_REQUESTED,
            )
        await self.store.complete(
            command.command_id,
            status=AgentCommandStatus.SUCCEEDED,
            result={"status": AgentRunStatus.CANCEL_REQUESTED.value},
        )

    async def _handle_approval(
        self, command: AgentCommandRecord, payload: CommandPayload
    ) -> None:
        approval_id = payload.approval_id or ""
        expected_worker_run_id = payload.worker_run_id or None
        expected_task_id = payload.task_id or None
        expected_plan_id = payload.plan_id or None
        expected_run_id = command.run_id
        decision = (
            AgentApprovalDecision.CANCELLED
            if command.command_type == CommandType.APPROVAL_CANCEL
            else AgentApprovalDecision(str(payload.decision))
        )
        resolved = await self.store.resolve_approval_for_attempt(
            approval_id,
            decision,
            expected_run_id=expected_run_id,
            expected_worker_run_id=expected_worker_run_id,
            expected_task_id=expected_task_id,
            expected_plan_id=expected_plan_id,
        )
        if self.graph is not None and command.run_id and resolved:
            # 审批结果写入存储后，从 LangGraph 的持久化中断检查点继续执行。
            await self.graph.ainvoke(
                {"approval_id": approval_id, "decision": decision.value},
                config={"configurable": {"thread_id": command.run_id}},
            )
        await self.store.complete(
            command.command_id,
            status=(
                AgentCommandStatus.SUCCEEDED
                if resolved
                else AgentCommandStatus.REJECTED
            ),
            result={"resolved": True} if resolved else None,
            error=None if resolved else {"code": "approval_already_resolved"},
        )

    async def _handle_memory_create(
        self, command: AgentCommandRecord, payload: CommandPayload
    ) -> None:
        if self.memory_manager is None:
            raise RuntimeError("memory service is not configured")
        memory_id = await self.memory_manager.add_memory(
            payload.content or "",
            payload.metadata,
            bool(payload.pinned),
        )
        await self.store.complete(
            command.command_id,
            status=AgentCommandStatus.SUCCEEDED,
            result={"memory_id": memory_id},
        )

    async def _handle_message_submit(
        self, command: AgentCommandRecord, payload: CommandPayload
    ) -> None:
        # Gateway 已在提交命令时原子分配运行；消费者只执行该 run_id，不重新创建或替换运行。
        run_id = command.run_id
        if not run_id:
            raise RuntimeError("message.submit is missing run_id")
        cancel_event = await self.cancellation.register(run_id)
        if cancel_event.is_set():
            await self._complete_cancelled_message(command, run_id)
            await self.cancellation.release(run_id)
            return

        await self.store.update_run_status(run_id, AgentRunStatus.RUNNING)
        await self._publish_run_event(command, EventType.RUN_STARTED)
        result = await invoke_graph(
            self.graph,
            session_id=command.session_id,
            run_id=run_id,
            user_message=payload.message or "",
            message_id=payload.message_id or "",
            attachment_ids=payload.attachment_ids,
            stop_signal=cancel_event,
        )
        if cancel_event.is_set():
            await self._complete_cancelled_message(command, run_id)
            await self.cancellation.release(run_id)
            return
        await self._complete_message(command, run_id, result)
        await self.cancellation.release(run_id)

    async def _complete_cancelled_message(
        self, command: AgentCommandRecord, run_id: str
    ) -> None:
        await self.store.update_run_status(run_id, AgentRunStatus.CANCELLED)
        await self._publish_run_event(command, EventType.RUN_CANCELLED)
        await self.store.complete(
            command.command_id,
            status=AgentCommandStatus.SUCCEEDED,
            result={"status": AgentRunStatus.CANCELLED.value},
        )

    async def _complete_message(
        self, command: AgentCommandRecord, run_id: str, result: Any
    ) -> None:
        await self.store.update_run_status(run_id, AgentRunStatus.COMPLETED)
        content = self._result_content(result)
        stream_id = f"answer-{run_id}"
        await self.store.upsert_snapshot(
            command.session_id,
            stream_id,
            1,
            content,
            run_id=run_id,
            status=StreamSnapshotStatus.COMPLETED,
        )
        await self.store.publish(
            ApplicationEvent(
                event_type=EventType.STREAM_SNAPSHOT,
                durability=EventDurability.SNAPSHOT,
                session_id=command.session_id,
                run_id=run_id,
                message_id=self._command_message_id(command),
                stream_id=stream_id,
                stream_type="answer",
                transition_id=f"stream:{stream_id}:snapshot:1",
                payload={
                    "session_id": command.session_id,
                    "run_id": run_id,
                    "message_id": self._command_message_id(command),
                    "stream_id": stream_id,
                    "stream_type": "answer",
                    "version": 1,
                    "last_chunk_id": 0,
                    "content": content,
                    "content_length": len(content.encode("utf-8")),
                    "status": StreamSnapshotStatus.COMPLETED.value,
                },
            )
        )
        await self._publish_run_event(command, EventType.RUN_COMPLETED)
        await self.store.complete(
            command.command_id,
            status=AgentCommandStatus.SUCCEEDED,
            result=result if isinstance(result, dict) else {"result": result},
        )

    @staticmethod
    def _result_content(result: Any) -> str:
        if isinstance(result, dict):
            return str(
                result.get("content")
                or result.get("response")
                or result.get("message")
                or ""
            )
        return str(result or "")

    async def _publish_run_event(
        self, command: AgentCommandRecord, event_type: EventType
    ) -> None:
        await self.store.publish(
            ApplicationEvent(
                event_type=event_type,
                durability=EventDurability.DURABLE,
                session_id=command.session_id,
                run_id=command.run_id,
                message_id=self._command_message_id(command),
                transition_id=(
                    f"run:{command.run_id}:{event_type.value}"
                    if command.run_id
                    else None
                ),
                payload={
                    "command_id": command.command_id,
                    "message_id": self._command_message_id(command),
                },
            )
        )

    @staticmethod
    def _command_message_id(command: AgentCommandRecord) -> str | None:
        if command.command_type != CommandType.MESSAGE_SUBMIT:
            return None
        try:
            return CommandPayload.model_validate_json(command.payload_json).message_id
        except ValueError:
            return None

    async def _handle_command_error(
        self, command: AgentCommandRecord, exc: Exception
    ) -> None:
        run_id = (
            command.run_id
            if command.command_type == CommandType.MESSAGE_SUBMIT
            else None
        )
        if run_id is not None:
            await self.store.update_run_status(run_id, AgentRunStatus.FAILED, str(exc))
            await self.store.publish(
                ApplicationEvent(
                    event_type=EventType.RUN_FAILED,
                    durability=EventDurability.DURABLE,
                    session_id=command.session_id,
                    run_id=run_id,
                    message_id=self._command_message_id(command),
                    transition_id=f"run:{run_id}:failed",
                    payload={
                        "command_id": command.command_id,
                        "message_id": self._command_message_id(command),
                        "error": str(exc),
                    },
                )
            )
        await self.store.complete(
            command.command_id,
            status=AgentCommandStatus.FAILED,
            error={"code": "command_failed", "message": str(exc)},
        )
