"""运行命令提交和状态查询用例。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
import hashlib
from typing import Any

from application.sessions import SessionNotFoundError
from domain.runs import (
    CommandEnqueueResult,
    CommandStatus,
    CommandStatusRecord,
    CommandStorePort,
    CommandType,
    RunCommand,
    RunQueryPort,
    RunStatus,
    RunSummary,
)
from domain.sessions import SessionRepository

from application.common.responses import CommandEnqueueResponse, CommandStatusResponse
from application.runs.responses import RunCancellationResponse


class CommandRejectedError(ValueError):
    """基础设施拒绝命令时携带稳定业务错误码。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CommandNotFoundError(LookupError):
    """请求的命令不存在。"""


class RunNotFoundError(LookupError):
    """取消请求指向的运行不存在。"""


class RunCommandService:
    """协调会话校验、命令生成和命令存储。"""

    def __init__(
        self,
        sessions: SessionRepository,
        commands: CommandStorePort,
        runs: RunQueryPort,
        *,
        id_factory: Callable[[], str],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = sessions
        self._commands = commands
        self._runs = runs
        self._id_factory = id_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def submit(
        self,
        session_id: str,
        *,
        command_id: str,
        message: str,
        attachment_ids: Sequence[str] = (),
        message_id: str | None = None,
    ) -> CommandEnqueueResponse:
        """提交一次消息运行；命令端口负责并发安全和幂等落库。"""
        if await self._sessions.get(session_id) is None:
            raise SessionNotFoundError(session_id)
        if not command_id.strip():
            raise ValueError("command_id is required")

        actual_message_id = message_id or self._id_factory()
        command = RunCommand(
            command_id=command_id,
            command_type=CommandType.RUN_START,
            session_id=session_id,
            run_id=self._id_factory(),
            payload={
                "message": message,
                "message_id": actual_message_id,
                "attachment_ids": list(attachment_ids),
            },
            issued_at=self._clock(),
        )
        try:
            result = await self._commands.enqueue_command(command)
        except CommandRejectedError:
            raise
        except ValueError as exc:
            code = str(exc)
            if code:
                raise CommandRejectedError(code) from exc
            raise

        return _enqueue_response(result)

    async def cancel_run(
        self, run_id: str, *, idempotency_key: str | None = None
    ) -> RunCancellationResponse:
        """按运行 ID 受理可安全重试的异步取消命令。"""
        run = await self._runs.get(run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        key = _cancel_idempotency_key(idempotency_key, f"run:{run_id}")
        command_id = _cancel_command_id(run_id, key)
        status = _run_status(run.status)
        if status in _TERMINAL_RUN_STATUSES:
            return RunCancellationResponse(
                command_id=await self._existing_cancel_command_id(command_id),
                run_id=run_id,
                status=status,
                already_terminal=True,
            )
        if status == RunStatus.CANCELLING.value:
            return RunCancellationResponse(
                command_id=await self._existing_cancel_command_id(command_id),
                run_id=run_id,
                status=status,
                already_terminal=False,
            )
        return await self._enqueue_cancellation(
            command_id=command_id,
            session_id=run.session_id,
            run_id=run_id,
        )

    async def cancel_session(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> RunCancellationResponse:
        """按会话取消当前运行；显式 run_id 优先。"""
        if await self._sessions.get(session_id) is None:
            raise SessionNotFoundError(session_id)
        if run_id is not None:
            target_run = await self._runs.get(run_id)
            if target_run is None or target_run.session_id != session_id:
                raise RunNotFoundError(run_id)
        else:
            target_run = await self._runs.get_active_for_session(session_id)
        if target_run is None:
            _cancel_idempotency_key(idempotency_key, f"session:{session_id}")
            return RunCancellationResponse(
                command_id=None,
                run_id=None,
                status=RunStatus.CANCELLED.value,
                already_terminal=True,
            )
        target_run_id = target_run.run_id
        scope = f"run:{target_run_id}"
        key = _cancel_idempotency_key(idempotency_key, scope)
        command_id = _cancel_command_id(scope, key)
        status = _run_status(target_run.status)
        if status in _TERMINAL_RUN_STATUSES:
            return RunCancellationResponse(
                command_id=await self._existing_cancel_command_id(command_id),
                run_id=target_run_id,
                status=status,
                already_terminal=True,
            )
        if status == RunStatus.CANCELLING.value:
            return RunCancellationResponse(
                command_id=await self._existing_cancel_command_id(command_id),
                run_id=target_run_id,
                status=status,
                already_terminal=False,
            )
        return await self._enqueue_cancellation(
            command_id=command_id,
            session_id=session_id,
            run_id=target_run_id,
        )

    async def _enqueue_cancellation(
        self, *, command_id: str, session_id: str, run_id: str
    ) -> RunCancellationResponse:
        """提交停止命令，并返回 API 对运行状态的受理视图。"""
        command = RunCommand(
            command_id=command_id,
            command_type=CommandType.RUN_CANCEL,
            session_id=session_id,
            run_id=run_id,
            payload={"reason": "USER_REQUEST"},
            issued_at=self._clock(),
        )
        await self._commands.enqueue_command(command)
        return RunCancellationResponse(
            command_id=command_id,
            run_id=run_id,
            status=RunStatus.CANCELLING.value,
            already_terminal=False,
        )

    async def _existing_cancel_command_id(self, command_id: str) -> str | None:
        """重复请求已完成终态转换时保留原命令身份。"""
        return command_id if await self._commands.get_command(command_id) is not None else None

    async def get_command(self, command_id: str) -> CommandStatusResponse:
        value = await self._commands.get_command(command_id)
        if value is None:
            raise CommandNotFoundError(command_id)
        return CommandStatusResponse(
            command_id=value.command_id,
            session_id=value.session_id,
            run_id=value.run_id,
            command_type=value.command_type.value if isinstance(value.command_type, CommandType) else str(value.command_type),
            status=value.status.value if isinstance(value.status, CommandStatus) else str(value.status),
            result=value.result,
            error=value.error,
        )


def _enqueue_response(value: CommandEnqueueResult) -> CommandEnqueueResponse:
    """将命令入队领域结果转换为只读应用响应。"""
    return CommandEnqueueResponse(
        command_id=value.command_id,
        run_id=value.run_id,
        message_id=value.message_id,
        attachment_ids=tuple(value.attachment_ids),
        status=value.status,
        deduplicated=value.deduplicated,
    )


def _cancel_idempotency_key(value: str | None, default_scope: str) -> str:
    """规范取消幂等键；未提供时使用作用域固定键支持网络重试。"""
    key = value.strip() if value is not None else ""
    if value is not None and not key:
        raise ValueError("Idempotency-Key must not be empty")
    return key or f"cancel:{default_scope}"


def _cancel_command_id(scope: str, idempotency_key: str) -> str:
    """生成不暴露调用方幂等键的稳定命令 ID。"""
    digest = hashlib.sha256(f"{scope}:{idempotency_key}".encode("utf-8")).hexdigest()
    return f"cmd_cancel_{digest[:32]}"


_TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED.value,
    RunStatus.FAILED.value,
    RunStatus.CANCELLED.value,
}


def _run_status(value: RunStatus | str) -> str:
    """统一处理领域枚举和持久化字符串状态。"""
    return value.value if isinstance(value, RunStatus) else str(value)

__all__ = [
    "CommandNotFoundError",
    "CommandRejectedError",
    "RunCommandService",
    "RunNotFoundError",
]
