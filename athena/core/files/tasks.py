"""SQLite-backed in-process worker pool for file processing."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Awaitable, Callable

from athena.core.files.runtime import FileIntelligenceRuntime
from athena.models.file import AttachmentStatus, FileTask, FileTaskStatus, FileTaskType
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class FileTaskWorker:
    def __init__(self, runtime: FileIntelligenceRuntime) -> None:
        self.runtime = runtime
        settings = runtime.settings
        self._poll_interval = settings.file_task_poll_interval
        self._task_timeout = settings.file_task_timeout
        self._semaphores = {
            FileTaskType.FILE_PARSE: asyncio.Semaphore(settings.file_parse_concurrency),
            FileTaskType.FILE_INDEX: asyncio.Semaphore(settings.file_embedding_concurrency),
            FileTaskType.EMBEDDING_GENERATE: asyncio.Semaphore(settings.file_embedding_concurrency),
            FileTaskType.FILE_SUMMARY: asyncio.Semaphore(settings.file_summary_concurrency),
            FileTaskType.CODE_ANALYSIS: asyncio.Semaphore(settings.file_code_concurrency),
        }
        self._max_running = (
            settings.file_parse_concurrency
            + settings.file_embedding_concurrency
            + settings.file_summary_concurrency
            + settings.file_code_concurrency
        )
        self._dispatcher: asyncio.Task[None] | None = None
        self._running: set[asyncio.Task[Any]] = set()
        self._stopping = False
        self._continuation_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    def set_continuation_callback(self, callback: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._continuation_callback = callback

    async def resume_continuations(self, attachment_id: str) -> None:
        if self._continuation_callback is None:
            return
        for continuation in await self.runtime.repository.claim_ready_continuations(attachment_id):
            await self._continuation_callback(continuation)

    async def start(self) -> None:
        await self.runtime.repository.recover_running_tasks()
        await self.runtime.repository.recover_continuations()
        self._stopping = False
        self._dispatcher = asyncio.create_task(self._run(), name="file-task-dispatcher")
        for attachment_id in await self.runtime.repository.waiting_continuation_attachment_ids():
            await self.resume_continuations(attachment_id)

    async def stop(self) -> None:
        self._stopping = True
        if self._dispatcher:
            self._dispatcher.cancel()
            try:
                await self._dispatcher
            except asyncio.CancelledError:
                pass
        if self._running:
            await asyncio.gather(*self._running, return_exceptions=True)

    async def enqueue_parse(self, session_id: str, attachment_id: str) -> FileTask:
        task = await self.enqueue_task(session_id, attachment_id, FileTaskType.FILE_PARSE)
        attachment = await self.runtime.repository.update_attachment(
            attachment_id, status=AttachmentStatus.QUEUED.value
        )
        if attachment is not None:
            await self.runtime.emit_attachment(attachment)
        return task

    async def enqueue_task(
        self,
        session_id: str,
        attachment_id: str,
        task_type: FileTaskType,
        *,
        payload: dict[str, Any] | None = None,
        priority: int = 0,
    ) -> FileTask:
        task = await self.runtime.repository.create_task(
            session_id,
            attachment_id,
            task_type,
            payload=payload,
            priority=priority,
            max_attempts=self.runtime.settings.file_task_max_attempts,
        )
        await self.runtime.emit_file_event("file_task_created", session_id, task)
        return task

    async def _run(self) -> None:
        # Keep dispatch on the application event loop: repository operations use
        # async SQLAlchemy/aiosqlite and callbacks emit on the same loop.
        # A worker thread would require a second loop and unsafe cross-thread
        # ownership of those resources without improving throughput.
        while not self._stopping:
            if len(self._running) >= self._max_running:
                await asyncio.sleep(self._poll_interval)
                continue
            task = await self.runtime.repository.claim_next_task()
            if task is None:
                await asyncio.sleep(self._poll_interval)
                continue
            running = asyncio.create_task(self._execute_with_limit(task))
            self._running.add(running)
            running.add_done_callback(self._running.discard)

    async def _execute_with_limit(self, task: FileTask) -> None:
        async with self._semaphores[task.task_type]:
            try:
                current = await self.runtime.repository.get_task(task.id)
                if current is None or current.status == FileTaskStatus.CANCELLED:
                    return
                await self._progress(task, 0.05, "starting")
                if task.task_type == FileTaskType.FILE_PARSE:
                    result = await asyncio.wait_for(
                        self.runtime.parse_attachment(task.attachment_id), timeout=self._task_timeout
                    )
                    await self._complete(task, result)
                    await self.enqueue_task(task.session_id, task.attachment_id, FileTaskType.FILE_INDEX)
                elif task.task_type == FileTaskType.FILE_INDEX:
                    result = await asyncio.wait_for(
                        self.runtime.index_attachment(task.attachment_id, mark_ready=False), timeout=self._task_timeout
                    )
                    await self._complete(task, result)
                    await self.enqueue_task(
                        task.session_id, task.attachment_id, FileTaskType.EMBEDDING_GENERATE
                    )
                elif task.task_type == FileTaskType.EMBEDDING_GENERATE:
                    result = await asyncio.wait_for(
                        self.runtime.index_attachment(task.attachment_id), timeout=self._task_timeout
                    )
                    await self._complete(task, result)
                elif task.task_type == FileTaskType.FILE_SUMMARY:
                    result = await asyncio.wait_for(
                        self.runtime.summarize_file(task.session_id, task.attachment_id, task.payload.get("summary_type", "general")),
                        timeout=self._task_timeout,
                    )
                    await self._complete(task, result)
                elif task.task_type == FileTaskType.CODE_ANALYSIS:
                    result = await asyncio.wait_for(
                        self.runtime.analyze_codebase(task.session_id, task.attachment_id),
                        timeout=self._task_timeout,
                    )
                    await self._complete(task, result)
                if self._continuation_callback is not None and task.task_type == FileTaskType.EMBEDDING_GENERATE:
                    await self.resume_continuations(task.attachment_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("file_task_failed", task_id=task.id)
                await self.runtime.repository.retry_or_fail_task(task, str(exc))
                current = await self.runtime.repository.get_task(task.id)
                if current and current.status == FileTaskStatus.FAILED:
                    await self.runtime.emit_file_event("file_task_failed", task.session_id, current)
                    if task.task_type in {
                        FileTaskType.FILE_PARSE,
                        FileTaskType.FILE_INDEX,
                        FileTaskType.EMBEDDING_GENERATE,
                    }:
                        attachment = await self.runtime.repository.update_attachment(
                            task.attachment_id,
                            status=AttachmentStatus.FAILED.value,
                            error_message=str(exc),
                        )
                        if attachment is not None:
                            await self.runtime.emit_attachment(attachment)
                    await self.resume_continuations(task.attachment_id)

    async def _progress(self, task: FileTask, progress: float, stage: str) -> None:
        await self.runtime.repository.update_task(task.id, progress=progress, stage=stage)
        current = await self.runtime.repository.get_task(task.id)
        if current:
            await self.runtime.emit_file_event("file_task_progress", task.session_id, current)

    async def _complete(self, task: FileTask, result: dict[str, Any]) -> None:
        current = await self.runtime.repository.get_task(task.id)
        if current is None or current.status == FileTaskStatus.CANCELLED:
            return
        await self.runtime.repository.update_task(task.id, status=FileTaskStatus.COMPLETED, progress=1.0,
                                                  stage="completed", result=result, completed_at=datetime.now().isoformat())
        current = await self.runtime.repository.get_task(task.id)
        if current:
            await self.runtime.emit_file_event("file_task_completed", task.session_id, current)
