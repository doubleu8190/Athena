"""由 Root LangGraph 驱动的 Run 执行适配器。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

from langgraph.types import Interrupt

from application.orchestration.protocols import RootGraphPort
from application.runs.ports import RunExecutorPort
from domain.runs import (
    CommandExecutionResult,
    RootWaitCheckpoint,
    RunCommand,
    RunResumePayload,
    RunStartPayload,
)
from shared import get_logger

logger = get_logger(__name__)

@dataclass(slots=True)
class _ActiveRun:
    """记录活动 Root 调用任务及其取消信号。
    """
    task: asyncio.Task[CommandExecutionResult]
    stop_signal: asyncio.Event


class LangGraphRunExecutor(RunExecutorPort):
    """将 Root 图适配为应用 RunExecutorPort。
    """

    def __init__(self, graph: RootGraphPort) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `graph`（RootGraphPort）：已编译或待执行的工作流图；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - ValueError：校验条件 `graph is None` 成立时（异常说明：graph is required）。
        - 未列出的依赖异常按当前实现传播。
        """
        if graph is None:
            raise ValueError("graph is required")
        self._graph = graph
        self._active: dict[str, _ActiveRun] = {}
        self._lock = asyncio.Lock()

    async def execute(self, command: RunCommand) -> CommandExecutionResult:
        """将 Run 命令转换为 Root 图调用并生成执行回执。

        参数：
        - `command`（RunCommand）：领域命令，携带幂等身份和目标对象；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - CommandExecutionResult：封装本次操作输出或状态的 `CommandExecutionResult` 结果对象。

        异常：
        - RuntimeError：校验条件 `command.lease is None` 成立时（异常说明：root_execution_lease_missing）。
        - TypeError：校验条件 `not (isinstance(command.payload, RunStartPayload))；not (isinstance(command.payload, RunResumePayload))` 成立时（异常说明：LangGraph execution accepts start or resume commands）。
        - RuntimeError：校验条件 `current is None` 成立时（异常说明：run execution requires an asyncio task）。
        - 未列出的依赖异常按当前实现传播。
        """
        if not command.run_id:
            return CommandExecutionResult(error={"code": "run_id_required"})
        if command.lease is None:
            raise RuntimeError("root_execution_lease_missing")
        if isinstance(command.payload, RunStartPayload):
            user_message = command.payload.message
            message_id = command.payload.message_id
            run_sequence = command.payload.sequence
            attachment_ids = list(command.payload.attachment_ids)
            resume_value = None
        elif isinstance(command.payload, RunResumePayload):
            # 恢复身份来自 checkpoint；这些启动参数不会参与恢复状态。
            user_message = ""
            message_id = ""
            run_sequence = 1
            attachment_ids = []
            resume_value = command.payload.resume_value
        else:
            raise TypeError("LangGraph execution accepts start or resume commands")

        stop_signal = asyncio.Event()
        current = asyncio.current_task()
        if current is None:  # pragma: no cover - asyncio 正常运行时始终提供当前任务。
            raise RuntimeError("run execution requires an asyncio task")
        async with self._lock:
            self._active[command.run_id] = _ActiveRun(current, stop_signal)
        try:
            value = await self._graph.invoke(
                session_id=command.session_id,
                run_id=command.run_id,
                lease=command.lease,
                user_message=user_message,
                message_id=message_id,
                run_sequence=run_sequence,
                attachment_ids=attachment_ids,
                resume_value=resume_value,
                stop_signal=stop_signal,
            )
        finally:
            async with self._lock:
                active = self._active.get(command.run_id)
                if active is not None and active.task is current:
                    self._active.pop(command.run_id, None)
        if isinstance(value, Mapping):
            # 在 LangGraph 边界把框架中断对象转换为领域身份；应用服务不解析框架返回值。
            if "__interrupt__" in value:
                root_wait = _root_wait_checkpoint(value, command.run_id)
                return CommandExecutionResult(
                    root_wait=root_wait,
                    result=dict(value),
                )
            if value.get("error") or value.get("status") == "failed":
                error = value.get("error") or value
                if not isinstance(error, Mapping):
                    return CommandExecutionResult(
                        error={"message": str(error)}
                    )
                return CommandExecutionResult(error=dict(error))
            result = value.get("result", value)
            if not isinstance(result, Mapping):
                return CommandExecutionResult(result={"result": result})
            return CommandExecutionResult(result=dict(result))
        return CommandExecutionResult(result={"result": value})

    async def cancel(self, run_id: str | None) -> None:
        """向指定 Run 的活动 Root 图调用发送取消信号。

        参数：
        - `run_id`（str | None）：目标运行的稳定标识，用于关联执行及其记录；约束：允许为 None，按未提供值的分支处理。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        if not run_id:
            return
        async with self._lock:
            active = self._active.get(run_id)
        if active is None:
            logger.info("run is not active", run_id=run_id)
            return
        active.stop_signal.set()
        current = asyncio.current_task()
        if active.task is not current and not active.task.done():
            active.task.cancel()


def _root_wait_checkpoint(
    result: Mapping[str, object], expected_run_id: str
) -> RootWaitCheckpoint:
    """校验 LangGraph 中断内容，并转换为 Root 等待领域值。

    参数：
    - `result`（Mapping[str, object]）：当前阶段生成的领域结果或操作回执；约束：无额外运行时限制。
    - `expected_run_id`（str）：必须与恢复身份中的 Run 标识一致的预期值；约束：须通过方法内校验；失败条件见下方异常说明。

    返回：
    - RootWaitCheckpoint：绑定 Run、计划轮次、版本和中断标识的持久化等待检查点。

    异常：
    - RuntimeError：校验条件 `not isinstance(interrupts, (list, tuple)) or len(interrupts) != 1` 成立时（异常说明：root_round_interrupt_count_invalid）。
    - RuntimeError：校验条件 `not isinstance(interrupt, Interrupt)` 成立时（异常说明：root_round_interrupt_invalid）。
    - RuntimeError：校验条件 `not isinstance(value, Mapping) or value.get('type') != 'round_wait'` 成立时（异常说明：root_round_interrupt_payload_invalid）。
    - RuntimeError：校验条件 `not isinstance(run_id, str) or run_id != expected_run_id or (not isinstance(plan_id, str)) or (not isinstance(round_id, str)) or (not isinstance(plan_version, int)) or isinstance(plan_version, bool) or (not isinstance(interrupt_id, str)) or (not isinstance(checkpoint_id, str))` 成立时（异常说明：root_round_interrupt_identity_invalid）。
    - 未列出的依赖异常按当前实现传播。
    """
    interrupts = result["__interrupt__"]
    if not isinstance(interrupts, (list, tuple)) or len(interrupts) != 1:
        raise RuntimeError("root_round_interrupt_count_invalid")
    interrupt = interrupts[0]
    if not isinstance(interrupt, Interrupt):
        raise RuntimeError("root_round_interrupt_invalid")
    value = interrupt.value
    interrupt_id = interrupt.id
    checkpoint_id = result.get("__root_checkpoint_id__")
    if not isinstance(value, Mapping) or value.get("type") != "round_wait":
        raise RuntimeError("root_round_interrupt_payload_invalid")
    run_id = value.get("run_id")
    plan_id = value.get("plan_id")
    round_id = value.get("round_id")
    plan_version = value.get("plan_version")
    if (
        not isinstance(run_id, str)
        or run_id != expected_run_id
        or not isinstance(plan_id, str)
        or not isinstance(round_id, str)
        or not isinstance(plan_version, int)
        or isinstance(plan_version, bool)
        or not isinstance(interrupt_id, str)
        or not isinstance(checkpoint_id, str)
    ):
        raise RuntimeError("root_round_interrupt_identity_invalid")
    return RootWaitCheckpoint(
        run_id=run_id,
        plan_id=plan_id,
        round_id=round_id,
        plan_version=plan_version,
        interrupt_id=interrupt_id,
        checkpoint_id=checkpoint_id,
    )


__all__ = ["LangGraphRunExecutor"]
