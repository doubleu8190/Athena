"""知识库文档解析任务的 SQLite 队列。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult

from athena.infrastructure.sqlite.engine import get_core_session
from athena.utils.id_generation import generate_time_id


class KnowledgeDocumentJobRepository:
    """按附件 ID 去重的、至少一次投递知识库索引队列。"""

    async def enqueue_job(self, attachment_id: str) -> bool:
        """创建或重新投递一个知识库文档任务。

        参数：
            attachment_id：待解析并建立向量索引的知识库文档 ID。

        返回：
            新建或重新排队时返回 ``True``；已在排队或运行时返回 ``False``。

        异常：
            SQLite 写入失败时向上抛出异常。
        """
        now = datetime.now().isoformat()
        async with get_core_session() as session:
            async with session.begin():
                result = await session.execute(
                    text(
                        """INSERT INTO knowledge_document_jobs
                        (job_id, attachment_id, status, attempt, available_at,
                         created_at, updated_at)
                        VALUES (:job_id, :attachment_id, 'queued', 0, :now, :now, :now)
                        ON CONFLICT(attachment_id) DO UPDATE SET
                            status = 'queued', attempt = 0, available_at = :now,
                            result_json = NULL, error_json = NULL, updated_at = :now
                        WHERE knowledge_document_jobs.status IN ('failed', 'cancelled')"""
                    ),
                    {
                        "job_id": generate_time_id(),
                        "attachment_id": attachment_id,
                        "now": now,
                    },
                )
        return cast(CursorResult[Any], result).rowcount > 0

    async def claim_next_job(self, *, max_attempts: int = 5) -> dict[str, Any] | None:
        """原子领取一条到期的知识库文档任务。

        参数：
            max_attempts：单个任务允许的最大处理次数。

        返回：
            领取成功时返回含 ``attachment_id`` 和本次 ``attempt`` 的任务；无任务时返回 ``None``。

        异常：
            SQLite 读取或更新失败时向上抛出异常。
        """
        now = datetime.now().isoformat()
        async with get_core_session() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        text("""SELECT job_id, attachment_id, attempt
                            FROM knowledge_document_jobs
                            WHERE status IN ('queued', 'retry')
                              AND available_at <= :now
                              AND attempt < :max_attempts
                            ORDER BY available_at, created_at
                            LIMIT 1"""),
                        {"now": now, "max_attempts": max_attempts},
                    )
                ).first()
                if row is None:
                    return None
                updated = cast(
                    CursorResult[Any],
                    await session.execute(
                        text(
                            """UPDATE knowledge_document_jobs
                            SET status = 'running', attempt = attempt + 1, updated_at = :now
                            WHERE job_id = :job_id AND status IN ('queued', 'retry')"""
                        ),
                        {"job_id": row.job_id, "now": now},
                    ),
                )
                if updated.rowcount != 1:
                    return None
        return {
            "job_id": str(row.job_id),
            "attachment_id": str(row.attachment_id),
            "attempt": int(row.attempt) + 1,
        }

    async def mark_succeeded(self, job_id: str, result: dict[str, Any]) -> None:
        """将已完成任务标记为成功并持久化处理结果。

        参数：
            job_id：已领取的任务 ID。
            result：解析和索引的摘要信息。

        返回：
            None。

        异常：
            SQLite 更新失败时向上抛出异常。
        """
        await self._mark(job_id, "succeeded", result=result, only_running=True)

    async def mark_failed(self, job_id: str, error: str, *, retry: bool) -> None:
        """记录任务失败，并根据重试策略更新状态。

        参数：
            job_id：失败任务 ID。
            error：用于诊断的错误文本。
            retry：为 ``True`` 时延迟重新排队，否则终止为 ``failed``。

        返回：
            None。

        异常：
            SQLite 更新失败时向上抛出异常。
        """
        available_at = (
            (datetime.now() + timedelta(seconds=30)).isoformat() if retry else None
        )
        await self._mark(
            job_id,
            "retry" if retry else "failed",
            error=error,
            available_at=available_at,
            only_running=True,
        )

    async def cancel_attachment_job(self, attachment_id: str) -> None:
        """取消尚未开始处理的附件任务。

        参数：
            attachment_id：被删除知识库文档的附件 ID。

        返回：
            None。

        异常：
            SQLite 更新失败时向上抛出异常。
        """
        async with get_core_session() as session:
            async with session.begin():
                await session.execute(
                    text("""UPDATE knowledge_document_jobs
                        SET status = 'cancelled', updated_at = :now
                        WHERE attachment_id = :attachment_id
                          AND status IN ('queued', 'retry', 'running')"""),
                    {"attachment_id": attachment_id, "now": datetime.now().isoformat()},
                )

    async def mark_cancelled(self, job_id: str, reason: str) -> None:
        """将已取消或已删除附件对应的任务固定为取消态。

        参数：
            job_id：任务唯一标识。
            reason：取消原因，供任务诊断页面显示。

        返回：
            None。

        异常：
            SQLite 更新失败时向上抛出异常。
        """
        await self._mark(job_id, "cancelled", error=reason)

    async def recover_interrupted_jobs(self) -> int:
        """将进程退出时处于运行态的任务恢复为可重试状态。

        参数：
            无。

        返回：
            恢复的任务数量。

        异常：
            SQLite 更新失败时向上抛出异常。
        """
        now = datetime.now().isoformat()
        async with get_core_session() as session:
            async with session.begin():
                result = await session.execute(
                    text("""UPDATE knowledge_document_jobs
                        SET status = 'retry', available_at = :now,
                            error_json = 'process_interrupted', updated_at = :now
                        WHERE status = 'running'"""),
                    {"now": now},
                )
        return cast(CursorResult[Any], result).rowcount

    async def _mark(
        self,
        job_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        available_at: str | None = None,
        only_running: bool = False,
    ) -> None:
        """写入任务终态或重试态。

        参数：
            job_id：待更新任务 ID。
            status：目标状态。
            result：成功时的处理摘要。
            error：失败时的错误文本。
            available_at：重试任务下次可领取时间。

        返回：
            None。

        异常：
            SQLite 更新失败时向上抛出异常。
        """
        async with get_core_session() as session:
            async with session.begin():
                await session.execute(
                    text("""UPDATE knowledge_document_jobs
                        SET status = :status, result_json = :result_json,
                            error_json = :error_json,
                            available_at = COALESCE(:available_at, available_at),
                            updated_at = :updated_at
                        WHERE job_id = :job_id
                        AND (:only_running = 0 OR status = 'running')"""),
                    {
                        "job_id": job_id,
                        "status": status,
                        "result_json": (
                            json.dumps(result, ensure_ascii=False) if result else None
                        ),
                        "error_json": error,
                        "available_at": available_at,
                        "updated_at": datetime.now().isoformat(),
                        "only_running": 1 if only_running else 0,
                    },
                )
