"""Submit and stop Run use cases."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
import re

from application.common.responses import RunEnqueueResponse, RunStopResponse
from application.runs.ports import RunExecutorPort
from domain.runs import RunLifecyclePort, RunStartPayload, RunSubmissionPort
from domain.sessions import Session


class RunRejectedError(ValueError):
    """携带稳定业务错误码的 Run 请求拒绝。"""

    def __init__(self, code: str) -> None:
        """保存业务错误码。

        参数：code 是 API 可识别的拒绝原因。
        返回：无。
        异常：不主动抛出业务异常。
        """
        self.code = code
        super().__init__(code)


class RunService:
    """协调 Run 提交、幂等回执和当前执行项停止请求。"""

    def __init__(
        self,
        submissions: RunSubmissionPort,
        lifecycle: RunLifecyclePort,
        *,
        id_factory: Callable[[], str],
        executor: RunExecutorPort | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """初始化 Run 用例。

        参数：submissions 保存消息与排队 Run；lifecycle 管理执行和停止；
        id_factory 生成服务端 ID；executor 可选，用于通知本进程的 Root 中断；
        clock 提供会话创建时间。
        返回：无。
        异常：不主动抛出业务异常。
        """
        self._submissions = submissions
        self._lifecycle = lifecycle
        self._executor = executor
        self._id_factory = id_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def submit(
        self,
        session_id: str | None,
        *,
        request_id: str,
        project_id: str | None = None,
        message: str,
        attachment_ids: Sequence[str] = (),
    ) -> RunEnqueueResponse:
        """提交一条用户消息并返回后端创建的 Run 标识。

        参数：session_id 为空时创建新会话；request_id 是幂等请求 ID；
        project_id 是新会话所属项目；message 和 attachment_ids 是用户输入。
        返回：包含会话、消息、Run、会话内序号和状态的回执。
        异常：输入无效或仓储拒绝请求时抛出 ValueError。
        """
        if not request_id.strip():
            raise ValueError("idempotency_key_required")
        if not message.strip() and not attachment_ids:
            raise ValueError("message_or_attachment_required")

        session_id = session_id.strip() if session_id else None
        if session_id == "":
            session_id = None
        new_session: Session | None = None
        if session_id is None:
            if project_id is None or not project_id.strip():
                raise ValueError("project_id_required")
            now = self._clock()
            new_session = Session.create(
                session_id=self._id_factory(),
                title=_session_title(message),
                now=now,
                project_id=project_id,
            )
            session_id = new_session.id

        run_id = self._id_factory()
        message_id = self._id_factory()
        payload = RunStartPayload(
            message=message,
            message_id=message_id,
            attachment_ids=tuple(attachment_ids),
        )
        request_parameters: dict[str, object] = {
            "session_id": session_id if new_session is None else None,
            "project_id": project_id,
            "message": message,
            "attachment_ids": list(attachment_ids),
        }
        try:
            result, replayed = await self._submissions.enqueue_run(
                request_id=request_id,
                parameters=request_parameters,
                session_id=session_id,
                project_id=project_id,
                run_id=run_id,
                payload=payload,
                new_session=new_session,
            )
        except ValueError as exc:
            raise RunRejectedError(str(exc)) from exc
        return RunEnqueueResponse(
            session_id=result.session_id,
            message_id=result.message_id,
            run_id=result.run_id,
            sequence=result.sequence,
            status=str(result.status),
            replayed=replayed,
        )

    async def stop_session(
        self,
        session_id: str,
        *,
        request_id: str,
        expected_run_id: str | None,
        displayed_session_seq: int,
    ) -> RunStopResponse:
        """停止客户端看到的 active Run，并保留会话排队项。

        参数：session_id 是目标会话；request_id 是幂等停止请求 ID；
        expected_run_id 是点击时看到的执行项；displayed_session_seq 是已展示游标。
        返回：此次停止所针对 Run 的受理状态。
        异常：执行项已变化或请求参数不合法时抛出 RunRejectedError。
        """
        try:
            result, replayed = await self._lifecycle.stop_active(
                session_id,
                request_id=request_id,
                expected_run_id=expected_run_id,
                displayed_session_seq=displayed_session_seq,
            )
        except LookupError as exc:
            raise RunRejectedError(str(exc)) from exc
        except ValueError as exc:
            raise RunRejectedError(str(exc)) from exc
        if result.changed and result.run_id is not None and self._executor is not None:
            await self._executor.cancel(result.run_id)
        if result.changed and result.run_id is not None:
            # Safe finalization is guarded by the execution lease. It completes
            # checkpointed/no-plan Runs immediately while active Root calls are
            # finalized by their consumer after releasing that lease.
            await self._lifecycle.finalize_stop(result.run_id)
        return RunStopResponse(
            run_id=result.run_id,
            status=result.status.value,
            no_op=result.no_op,
            replayed=replayed,
        )


def _session_title(message: str) -> str:
    """从首条用户消息生成简短会话标题。"""
    content = re.sub(r"\s+", " ", message).strip()
    if content.lower() in {
        "please process the attached files.",
        "please process the attached files",
        "请处理附件。",
        "请处理附件",
    }:
        return "附件分析"
    content = re.sub(
        r"^(请帮我|请帮忙|请|帮我|麻烦你|麻烦|please help me|please|help me)[，,：:\s]*",
        "",
        content,
        flags=re.IGNORECASE,
    )
    first_sentence = re.split(r"[。！？!?；;]", content, maxsplit=1)[0].strip()
    return (first_sentence or content)[:48] or "新聊天"


__all__ = ["RunRejectedError", "RunService"]
