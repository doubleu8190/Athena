"""知识库文档解析和索引的后台 worker。"""

from __future__ import annotations

import asyncio

from athena.core.files.runtime import FileIntelligenceRuntime
from athena.infrastructure.sqlite.repositories.knowledge_document_job_repository import (
    KnowledgeDocumentJobRepository,
)
from athena.models.file import AttachmentStatus
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class KnowledgeDocumentWorker:
    """消费持久知识库文档任务，并管理失败重试。"""

    def __init__(
        self,
        repository: KnowledgeDocumentJobRepository,
        file_runtime: FileIntelligenceRuntime,
        *,
        poll_interval: float = 1.0,
        max_attempts: int = 5,
    ) -> None:
        """初始化知识库文档后台 worker。

        参数：
            repository：任务队列仓储。
            file_runtime：文件解析、索引和删除运行时。
            poll_interval：空队列时的轮询间隔，单位秒。
            max_attempts：单个任务失败后的最大尝试次数。

        返回：
            None。

        异常：
            不主动抛出业务异常。
        """
        self._repository = repository
        self._file_runtime = file_runtime
        self._poll_interval = poll_interval
        self._max_attempts = max_attempts
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """恢复中断任务并启动 worker 循环。

        参数：
            无。

        返回：
            None。

        异常：
            队列恢复失败时向上抛出异常。
        """
        if self._task is None:
            await self._repository.recover_interrupted_jobs()
            self._stop.clear()
            self._task = asyncio.create_task(
                self.run(), name="athena-knowledge-document-worker"
            )

    async def stop(self) -> None:
        """请求 worker 停止并等待当前循环退出。

        参数：
            无。

        返回：
            None。

        异常：
            运行中的任务取消或退出失败时向上抛出异常。
        """
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def run(self) -> None:
        """持续领取任务，完成索引或记录可恢复失败。

        参数：
            无。

        返回：
            None。

        异常：
            单个任务异常会被记录，不中断 worker 循环。
        """
        while not self._stop.is_set():
            job = await self._repository.claim_next_job(max_attempts=self._max_attempts)
            if job is None:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._poll_interval
                    )
                except asyncio.TimeoutError:
                    continue
                continue
            await self._process_job(job)

    async def _process_job(self, job: dict[str, object]) -> None:
        """执行单个任务并将结果写回状态机。

        参数：
            job：已领取任务，必须包含 ``job_id``、``attachment_id`` 和 ``attempt``。

        返回：
            None。

        异常：
            处理异常被转化为重试或失败状态，不向 worker 主循环传播。
        """
        job_id = str(job["job_id"])
        attachment_id = str(job["attachment_id"])
        attempt = int(job["attempt"])
        attachment = await self._file_runtime.repository.get_attachment(attachment_id)
        if attachment is None:
            await self._repository.mark_succeeded(job_id, {"status": "deleted"})
            return
        try:
            result = await self._file_runtime.process_knowledge_document(attachment_id)
            await self._repository.mark_succeeded(job_id, result)
        except Exception as exc:
            retry = attempt < self._max_attempts
            await self._repository.mark_failed(job_id, str(exc), retry=retry)
            if not retry:
                await self._file_runtime.repository.update_attachment(
                    attachment_id,
                    status=AttachmentStatus.FAILED.value,
                    error_message=str(exc),
                )
            logger.warning(
                "knowledge_document_job_failed",
                attachment_id=attachment_id,
                attempt=attempt,
                retry=retry,
                error=str(exc),
            )
