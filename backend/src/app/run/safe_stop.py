"""Run 上下文安全停止的系统提示构造与一次性终态提交协作者。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime

from domain.message import (
    ConversationMessage,
    ConversationMessageDraft,
    ConversationRole,
    ExecutionMessage,
    MessageMetadata,
    MessageStatus,
    TextPart,
    ToolResultPart,
)
from domain.run.model import (
    ContextStopReason,
    FailRunCommand,
    Run,
    RunUsage,
)
from domain.run.ports import RunRepository
from domain.tool import AuthorizedRunContext

CONTEXT_SAFETY_STOP_ERROR_CODE = "run_context_safety_stop"

CONTEXT_STOP_REASON_TEXT: dict[ContextStopReason, str] = {
    ContextStopReason.NO_COMPRESSIBLE_HISTORY: "没有可继续压缩的较早历史",
    ContextStopReason.COMPRESSION_NOT_CONFIGURED: "当前未配置上下文压缩策略",
    ContextStopReason.COMPRESSION_MODEL_UNAVAILABLE: "上下文压缩模型不可用",
    ContextStopReason.COMPRESSION_BUDGET_EXHAUSTED: "上下文压缩调用额度已用尽",
    ContextStopReason.COMPRESSION_INPUT_TOO_LARGE: "待压缩历史超出压缩模型窗口",
    ContextStopReason.COMPRESSION_FAILED: "上下文摘要压缩失败",
    ContextStopReason.COMPRESSION_INSUFFICIENT: "压缩后输入仍超过安全上限",
    ContextStopReason.HARD_WINDOW_EXCEEDED: "输入与输出预留超出模型窗口上限",
}


def _artifact_filenames(
    messages: tuple[ConversationMessage | ExecutionMessage, ...],
) -> tuple[str, ...]:
    """仅提取已持久化工具结果中的交付文件名，严禁读取消息正文、工具输出或图片 base64。"""
    names: list[str] = []
    seen: set[str] = set()
    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolResultPart):
                for artifact in part.artifacts:
                    if artifact.filename and artifact.filename not in seen:
                        seen.add(artifact.filename)
                        names.append(artifact.filename)
    return tuple(names)


def system_context_report(
    reason: ContextStopReason,
    artifact_filenames: Sequence[str] = (),
) -> str:
    """生成简洁可读的上下文安全停止说明，明确任务未完成且不泄露正文或工具输出。"""
    reason_text = CONTEXT_STOP_REASON_TEXT.get(reason, "上下文达到安全上限")
    lines = [
        f"上下文超出安全上限，任务未标记为完成（原因：{reason_text}）。",
        "请开启新会话或精简上下文后重试。",
    ]
    if artifact_filenames:
        lines.append("已持久化交付文件：" + "、".join(artifact_filenames))
    return "\n".join(lines)


class SafeStopReporter:
    """上下文安全停止时的轻量一次性终态提交协作者。"""

    def __init__(
        self,
        repository: RunRepository,
        *,
        id_factory: Callable[[str], str],
        now: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._id_factory = id_factory
        self._now = now

    async def stop_for_context_safety(
        self,
        run: Run,
        messages: tuple[ConversationMessage | ExecutionMessage, ...],
        reason: ContextStopReason,
        *,
        authorize: Callable[[Run], Awaitable[AuthorizedRunContext | None]] | None,
        publish_events: Callable[[], Awaitable[None]],
        llm_request_count: int | None = None,
    ) -> None:
        """单次校验租约与鉴权后，原子提交系统安全停止消息与 FAILED 终态。"""
        worker_id = run.worker_id
        if worker_id is None:
            return
        # 取消与失租高于安全报告写入；租约已失效时直接退出，避免越权覆盖终态。
        if not await self._repository.is_live_execution(run.id, run.execution_epoch):
            return
        if authorize is not None and await authorize(run) is None:
            # 鉴权失效时仅记录 run_auth_session_invalid，绝不附带安全停止报告消息。
            await self._repository.fail(
                FailRunCommand(
                    run_id=run.id,
                    worker_id=worker_id,
                    execution_epoch=run.execution_epoch,
                    safe_error_code="run_auth_session_invalid",
                    failed_at=self._now(),
                    usage=RunUsage(),
                    llm_request_count=llm_request_count,
                )
            )
            await publish_events()
            return

        finished_at = self._now()
        report_text = system_context_report(reason, _artifact_filenames(messages))
        report_message = ConversationMessageDraft(
            id=self._id_factory("msg"),
            session_id=run.session_id,
            owner_id=run.owner_id,
            role=ConversationRole.ASSISTANT,
            status=MessageStatus.COMPLETED,
            parts=(TextPart(report_text),),
            reply_to_message_id=run.trigger_message_id,
            run_id=run.id,
            client_message_id=None,
            created_at=finished_at,
            completed_at=finished_at,
            metadata=MessageMetadata(provider="system_context_safety", model=None),
            context_turn_id=run.trigger_message_id,
        )
        await self._repository.fail(
            FailRunCommand(
                run_id=run.id,
                worker_id=worker_id,
                execution_epoch=run.execution_epoch,
                safe_error_code=CONTEXT_SAFETY_STOP_ERROR_CODE,
                failed_at=finished_at,
                report_message=report_message,
                usage=RunUsage(),
                context_stop_reason=reason,
                llm_request_count=llm_request_count,
            )
        )
        await publish_events()


__all__ = [
    "CONTEXT_SAFETY_STOP_ERROR_CODE",
    "CONTEXT_STOP_REASON_TEXT",
    "SafeStopReporter",
    "system_context_report",
]
