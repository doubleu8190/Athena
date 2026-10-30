"""提交、停止和查询 Run 的应用用例。
"""

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

        参数：
        - `code`（str）：用于区分错误或结果类型的代码；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
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

        参数：
        - `submissions`（RunSubmissionPort）：提交并查询 Run 启动请求的应用端口；约束：无额外运行时限制。
        - `lifecycle`（RunLifecyclePort）：启动、停止和查询 Run 生命周期的应用端口；约束：无额外运行时限制。
        - `id_factory`（Callable[[], str]）：生成稳定领域标识的可注入函数；约束：无额外运行时限制。
        - `executor`（RunExecutorPort | None）：执行已提交 Run 的运行时端口；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `clock`（Callable[[], datetime] | None）：生成当前时间的可注入函数，可用于控制时间来源；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
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

        参数：
        - `session_id`（str | None）：目标会话的稳定标识，用于限定数据范围；约束：允许为 None，按未提供值的分支处理；须通过方法内校验；失败条件见下方异常说明。
        - `request_id`（str）：本次 Run 请求的稳定标识，用于幂等处理和关联事件；约束：须通过方法内校验；失败条件见下方异常说明。
        - `project_id`（str | None）：项目空间的稳定标识；None 表示未绑定项目；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`；须通过方法内校验；失败条件见下方异常说明。
        - `message`（str）：当前待处理或要返回的消息对象；约束：须通过方法内校验；失败条件见下方异常说明。
        - `attachment_ids`（Sequence[str]）：当前请求已授权使用的附件标识集合；约束：可省略，默认值为 `()`；须通过方法内校验；失败条件见下方异常说明。

        返回：
        - RunEnqueueResponse：包含会话、消息、Run、会话内序号和状态的回执。 供调用方展示或继续处理的 `RunEnqueueResponse` 响应对象。

        异常：
        - ValueError：校验条件 `not request_id.strip()` 成立时（异常说明：idempotency_key_required）。
        - ValueError：校验条件 `not message.strip() and (not attachment_ids)` 成立时（异常说明：message_or_attachment_required）。
        - ValueError：校验条件 `session_id is None；project_id is None or not project_id.strip()` 成立时（异常说明：project_id_required）。
        - RunRejectedError：校验条件 `捕获 ValueError 后` 成立时。
        - 未列出的依赖异常按当前实现传播。
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
        """停止客户端已展示的活动 Run，并保留会话排队项。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。
        - `request_id`（str）：本次 Run 请求的稳定标识，用于幂等处理和关联事件；约束：无额外运行时限制。
        - `expected_run_id`（str | None）：必须与恢复身份中的 Run 标识一致的预期值；约束：允许为 None，按未提供值的分支处理。
        - `displayed_session_seq`（int）：客户端已经展示的最后一个会话序号；约束：无额外运行时限制。

        返回：
        - RunStopResponse：包含停止状态、Run 标识及重复请求处理结果的响应。

        异常：
        - RunRejectedError：校验条件 `捕获 LookupError 后` 成立时。
        - RunRejectedError：校验条件 `捕获 ValueError 后` 成立时。
        - 未列出的依赖异常按当前实现传播。
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
            # 最终停止收尾受执行租约保护。已写入检查点但尚无计划的 Run 会立即完成停止处理；
            # 活动 Root 调用则由其消费者在释放租约后完成收尾。
            await self._lifecycle.finalize_stop(result.run_id)
        return RunStopResponse(
            run_id=result.run_id,
            status=result.status.value,
            no_op=result.no_op,
            replayed=replayed,
        )


def _session_title(message: str) -> str:
    """从首条用户消息生成简短会话标题。

    参数：
    - `message`（str）：当前待处理或要返回的消息对象；约束：无额外运行时限制。

    返回：
    - str：从首条用户消息生成的非空会话标题。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
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
