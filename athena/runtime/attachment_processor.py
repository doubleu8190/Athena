"""Lifecycle adapter for one attachment processing operation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from athena.contracts.events import ApplicationEvent, EventDurability, EventType
from athena.models.file import AttachmentStatus

from .processor_lifecycle import (
    ProcessAction,
    ProcessDecision,
    ProcessOutcome,
    Processor,
)
from .state import FileProcessResult

if TYPE_CHECKING:
    from .langgraph_runtime import LangGraphRuntime


@dataclass(frozen=True, slots=True)
class AttachmentProcessContext:
    attachment_id: str
    message_id: str
    session_id: str
    run_id: str


class AttachmentProcessor(
    Processor[AttachmentProcessContext, FileProcessResult]
):
    """Own one attachment's lifecycle while preserving progress events."""

    def __init__(self, runtime: LangGraphRuntime) -> None:
        self._runtime = runtime

    async def inspect(
        self, context: AttachmentProcessContext
    ) -> ProcessDecision[FileProcessResult]:
        attachment = await self._runtime._db.files.get_attachment(
            context.attachment_id, context.session_id
        )
        if attachment is None:
            return ProcessDecision(
                ProcessAction.BLOCK,
                reason="附件不存在或不属于当前会话",
            )
        if attachment.status is AttachmentStatus.DELETED:
            return ProcessDecision(ProcessAction.BLOCK, reason="附件已删除")
        if attachment.status is AttachmentStatus.READY:
            metadata = attachment.metadata.model_dump(mode="json")
            return ProcessDecision(
                ProcessAction.SKIP,
                result={
                    "message_id": context.message_id,
                    "attachment_id": context.attachment_id,
                    "status": "ready",
                    "error": None,
                    "chunk_count": int(metadata.get("chunk_count", 0)),
                },
                reason="附件已经完成解析和索引",
            )
        if attachment.status is AttachmentStatus.PROCESSING:
            # Parsing and indexing replace their durable projections, so a
            # checkpoint replay can safely resume the in-progress operation.
            return ProcessDecision(ProcessAction.RESUME, resume_from="processing")
        return ProcessDecision(ProcessAction.EXECUTE)

    async def pre_process(
        self,
        context: AttachmentProcessContext,
        decision: ProcessDecision[FileProcessResult],
    ) -> None:
        await self._runtime._db.files.update_attachment(
                context.attachment_id,
                status=AttachmentStatus.PROCESSING.value,
                error_message=None,
            )
        await self._runtime._events.publish(
            ApplicationEvent(
                event_type=EventType.FILE_PROCESSING_STARTED,
                durability=EventDurability.DURABLE,
                session_id=context.session_id,
                run_id=context.run_id,
                message_id=context.message_id,
                attachment_id=context.attachment_id,
                transition_id=f"attachment:{context.attachment_id}:started",
                payload={
                    "message_id": context.message_id,
                    "attachment_id": context.attachment_id,
                },
            )
        )

    async def process(
        self,
        context: AttachmentProcessContext,
        decision: ProcessDecision[FileProcessResult],
    ) -> FileProcessResult:
        try:
            return await self._runtime._process_attachment_once(
                context.attachment_id,
                context.message_id,
                context.session_id,
                context.run_id,
            )
        except Exception as exc:
            # Processing failures are durable business results; cancellation
            # remains a control-flow exception and is intentionally propagated.
            return {
                "message_id": context.message_id,
                "attachment_id": context.attachment_id,
                "status": "failed",
                "error": str(exc),
                "chunk_count": None,
            }

    async def post_process(
        self,
        context: AttachmentProcessContext,
        outcome: ProcessOutcome[FileProcessResult],
    ) -> None:
        result = outcome.result
        if outcome.status == "cancelled":
            # Cancellation is recoverable control flow, not a processing
            # failure. Keep the attachment in PROCESSING/UPLOADED so the next
            # graph attempt can inspect and resume it.
            return
        if outcome.status == "completed" and result is not None and result.get("status") != "failed":
            await self._runtime._file_runtime.emit(
                EventType.FILE_PROCESSING_COMPLETED,
                context.session_id,
                {
                    "message_id": context.message_id,
                    "attachment_id": context.attachment_id,
                    "run_id": context.run_id,
                    "chunk_count": result.get("chunk_count"),
                    "transition_id": f"attachment:{context.attachment_id}:completed",
                },
            )
            return

        error = str(outcome.error) if outcome.error is not None else (
            result.get("error") if result is not None else "附件处理失败"
        )
        attachment = await self._runtime._db.files.update_attachment(
            context.attachment_id,
            status=AttachmentStatus.FAILED.value,
            error_message=error,
        )
        if attachment is not None:
            await self._runtime._file_runtime.emit_attachment(
                attachment, run_id=context.run_id
            )
        await self._runtime._file_runtime.emit(
            EventType.FILE_PROCESSING_FAILED,
            context.session_id,
            {
                "message_id": context.message_id,
                "attachment_id": context.attachment_id,
                "run_id": context.run_id,
                "error": error,
                "transition_id": f"attachment:{context.attachment_id}:failed",
            },
        )


__all__ = ["AttachmentProcessContext", "AttachmentProcessor"]
