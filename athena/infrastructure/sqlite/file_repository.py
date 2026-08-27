"""文件智能的 SQLite 持久化。

``FileRepository`` 封装所有 File Intelligence 相关的数据库操作，
遵循 Repository 模式：查询方法返回类型化领域模型，写入方法接受类型化模型。
所有删除操作为软删除（设置 deleted_time）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Any, Iterable

from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from athena.infrastructure.sqlite.engine import get_session
from athena.infrastructure.sqlite.models import (
    AgentContinuationModel,
    AdapterRegistryModel,
    AttachmentModel,
    CodeDependencyModel,
    CodeSymbolModel,
    FileArtifactModel,
    FileChunkModel,
    MessageAttachmentModel,
    ProcessingTaskModel,
)
from athena.infrastructure.sqlite.repositories import _json_dumps, _json_loads
from athena.models.file import (
    AdapterInfo,
    Attachment,
    AttachmentStatus,
    FileChunk,
    FileTask,
    FileTaskStatus,
    FileTaskType,
)
from athena.utils.ids import generate_time_id


_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+(?:[.-][A-Za-z0-9_]+)*|[\u4e00-\u9fff]+")


def _fts_match_expression(query: str) -> str | None:
    """Build a quoted FTS5 expression without allowing query operators through."""
    tokens = _FTS_TOKEN_RE.findall(query)
    if not tokens:
        return None
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _now() -> datetime:
    """返回当前时间。"""
    return datetime.now()


def _attachment(row: AttachmentModel) -> Attachment:
    """将 ORM 模型转换为领域模型。"""
    return Attachment(
        id=row.id,
        session_id=row.session_id,
        message_id=row.message_id,
        filename=row.filename,
        mime_type=row.mime_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        storage_key=row.storage_key,
        adapter_name=row.adapter_name,
        adapter_version=row.adapter_version,
        status=AttachmentStatus(row.status),
        capabilities=_json_loads(row.capabilities_json, []),
        metadata=_json_loads(row.metadata_json, {}),
        error_message=row.error_message,
        created_at=datetime.fromisoformat(row.created_at),
        updated_at=datetime.fromisoformat(row.updated_at),
        deleted_time=(
            datetime.fromisoformat(row.deleted_time) if row.deleted_time else None
        ),
    )


def _task(row: ProcessingTaskModel) -> FileTask:
    """将 ORM 模型转换为领域模型。"""
    return FileTask(
        id=row.id,
        session_id=row.session_id,
        attachment_id=row.attachment_id,
        task_type=FileTaskType(row.task_type),
        status=FileTaskStatus(row.status),
        progress=row.progress,
        stage=row.stage,
        priority=row.priority,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        payload=_json_loads(row.payload_json, {}),
        result=_json_loads(row.result_json, {}),
        error_message=row.error_message,
        available_at=datetime.fromisoformat(row.available_at),
        created_at=datetime.fromisoformat(row.created_at),
        updated_at=datetime.fromisoformat(row.updated_at),
        started_at=datetime.fromisoformat(row.started_at) if row.started_at else None,
        completed_at=(
            datetime.fromisoformat(row.completed_at) if row.completed_at else None
        ),
    )


class FileRepository:
    """File Intelligence 持久化仓库。

    管理附件、处理任务、分块、产物、代码索引、续跑记录和适配器注册表
    的 CRUD 操作。所有写入方法使用事务保证原子性。
    """

    async def create_continuation(
        self,
        session_id: str,
        run_id: str,
        *,
        task_ids: list[str],
        request: dict[str, Any],
    ) -> str:
        """创建 Agent 续跑记录（附件处理完成后恢复执行）。

        参数：
            session_id: 会话 ID。
            run_id: 运行 ID。
            task_ids: 等待完成的任务 ID 列表。
            request: 原始请求数据（含 message_id、attachment_ids 等）。

        返回值：
            续跑记录 ID。
        """
        continuation_id = generate_time_id()
        now = _now().isoformat()
        async with get_session() as session:
            async with session.begin():
                session.add(
                    AgentContinuationModel(
                        id=continuation_id,
                        session_id=session_id,
                        run_id=run_id,
                        task_ids_json=_json_dumps(task_ids),
                        tool_calls_json=_json_dumps(request),
                        status="waiting",
                        created_at=now,
                    )
                )
        return continuation_id

    async def claim_ready_continuations(
        self, attachment_id: str
    ) -> list[dict[str, Any]]:
        """原子认领可恢复的续跑记录（附件的所有任务均已完成）。"""
        async with get_session() as session:
            async with session.begin():
                task_rows = (
                    await session.execute(
                        select(ProcessingTaskModel.session_id).where(
                            ProcessingTaskModel.attachment_id == attachment_id,
                            ProcessingTaskModel.status.in_(
                                ["queued", "running", "waiting"]
                            ),
                        )
                    )
                ).all()
                if task_rows:
                    return []
                rows = (
                    (
                        await session.execute(
                            select(AgentContinuationModel).where(
                                AgentContinuationModel.status == "waiting"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                claimed: list[dict[str, Any]] = []
                now = _now().isoformat()
                for row in rows:
                    request = _json_loads(row.tool_calls_json, {})
                    if attachment_id not in request.get("attachment_ids", []):
                        continue
                    requested_ids = request.get("attachment_ids", [])
                    terminal_rows = (
                        await session.execute(
                            select(AttachmentModel.id, AttachmentModel.status).where(
                                AttachmentModel.id.in_(requested_ids),
                                AttachmentModel.status.in_(
                                    [
                                        AttachmentStatus.READY.value,
                                        AttachmentStatus.FAILED.value,
                                        AttachmentStatus.DELETED.value,
                                    ]
                                ),
                            )
                        )
                    ).all()
                    if {item_id for item_id, _ in terminal_rows} != set(requested_ids):
                        continue
                    row.status = "resuming"
                    row.resumed_at = now
                    claimed.append(
                        {
                            "id": row.id,
                            "session_id": row.session_id,
                            "run_id": row.run_id,
                            "file_outcomes": {
                                item_id: status for item_id, status in terminal_rows
                            },
                            **request,
                        }
                    )
                return claimed

    async def finish_continuation(
        self, continuation_id: str, *, failed: bool = False
    ) -> None:
        """完成续跑记录（标记为 completed 或 failed）。"""
        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    update(AgentContinuationModel)
                    .where(
                        AgentContinuationModel.id == continuation_id,
                        AgentContinuationModel.status == "resuming",
                    )
                    .values(status="failed" if failed else "completed")
                )

    async def recover_continuations(self) -> int:
        """恢复中断的续跑记录（将 resuming 状态回退为 waiting）。"""
        async with get_session() as session:
            async with session.begin():
                result = await session.execute(
                    update(AgentContinuationModel)
                    .where(AgentContinuationModel.status == "resuming")
                    .values(status="waiting", resumed_at=None)
                )
                return int(result.rowcount or 0)

    async def waiting_continuation_attachment_ids(self) -> list[str]:
        """获取所有等待中续跑记录关联的附件 ID 列表（去重）。"""
        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        select(AgentContinuationModel.tool_calls_json).where(
                            AgentContinuationModel.status == "waiting"
                        )
                    )
                )
                .scalars()
                .all()
            )
        return list(
            dict.fromkeys(
                attachment_id
                for raw in rows
                for attachment_id in _json_loads(raw, {}).get("attachment_ids", [])
            )
        )

    async def delete_session(self, session_id: str) -> None:
        """软删除会话的所有文件记录，并取消待处理工作。"""
        now = _now().isoformat()
        async with get_session() as session:
            async with session.begin():
                attachment_ids = list(
                    (
                        await session.execute(
                            select(AttachmentModel.id).where(
                                AttachmentModel.session_id == session_id,
                                AttachmentModel.deleted_time.is_(None),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                await session.execute(
                    delete(AgentContinuationModel).where(
                        AgentContinuationModel.session_id == session_id
                    )
                )
                if not attachment_ids:
                    return
                chunk_ids = list(
                    (
                        await session.execute(
                            select(FileChunkModel.id).where(
                                FileChunkModel.attachment_id.in_(attachment_ids)
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if chunk_ids:
                    placeholders = ", ".join(f":id{i}" for i in range(len(chunk_ids)))
                    await session.execute(
                        text(
                            f"DELETE FROM file_chunk_fts WHERE chunk_id IN ({placeholders})"
                        ),
                        {f"id{i}": value for i, value in enumerate(chunk_ids)},
                    )
                await session.execute(
                    update(AttachmentModel)
                    .where(AttachmentModel.id.in_(attachment_ids))
                    .values(
                        status=AttachmentStatus.DELETED.value,
                        deleted_time=now,
                        updated_at=now,
                    )
                )
                await session.execute(
                    update(ProcessingTaskModel)
                    .where(ProcessingTaskModel.attachment_id.in_(attachment_ids))
                    .where(
                        ProcessingTaskModel.status.in_(["queued", "running", "waiting"])
                    )
                    .values(
                        status=FileTaskStatus.CANCELLED.value,
                        completed_at=now,
                        updated_at=now,
                    )
                )
                await session.execute(
                    delete(FileChunkModel).where(
                        FileChunkModel.attachment_id.in_(attachment_ids)
                    )
                )
                await session.execute(
                    delete(FileArtifactModel).where(
                        FileArtifactModel.attachment_id.in_(attachment_ids)
                    )
                )
                await session.execute(
                    delete(CodeSymbolModel).where(
                        CodeSymbolModel.attachment_id.in_(attachment_ids)
                    )
                )
                await session.execute(
                    delete(CodeDependencyModel).where(
                        CodeDependencyModel.attachment_id.in_(attachment_ids)
                    )
                )
                await session.execute(
                    delete(MessageAttachmentModel).where(
                        MessageAttachmentModel.attachment_id.in_(attachment_ids)
                    )
                )

    async def orphan_storage_keys(self) -> list[str]:
        """返回没有活动附件引用的 blob 键，供清理工作池使用。"""
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(AttachmentModel.storage_key, AttachmentModel.deleted_time)
                )
            ).all()
            live = {key for key, deleted_time in rows if deleted_time is None}
            return sorted({key for key, _ in rows} - live)

    async def live_storage_keys(self) -> set[str]:
        """执行“live storage keys”操作。

        返回值：
            set[str]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        async with get_session() as session:
            return set(
                (
                    await session.execute(
                        select(AttachmentModel.storage_key)
                        .where(AttachmentModel.deleted_time.is_(None))
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )

    async def create_attachment(
        self,
        *,
        session_id: str,
        filename: str,
        mime_type: str,
        size_bytes: int,
        sha256: str,
        storage_key: str,
    ) -> Attachment:
        """创建附件记录（上传完成后调用）。

        参数：
            session_id: 会话 ID。
            filename: 原始文件名。
            mime_type: MIME 类型。
            size_bytes: 文件字节数。
            sha256: 文件内容的 SHA-256 哈希。
            storage_key: 存储层的 blob 键。

        返回值：
            创建的 ``Attachment`` 实例。
        """
        now = _now().isoformat()
        row = AttachmentModel(
            id=generate_time_id(),
            session_id=session_id,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size_bytes,
            sha256=sha256,
            storage_key=storage_key,
            status=AttachmentStatus.UPLOADED.value,
            created_at=now,
            updated_at=now,
        )
        async with get_session() as session:
            async with session.begin():
                session.add(row)
        return _attachment(row)

    async def get_attachment(
        self,
        attachment_id: str,
        session_id: str | None = None,
        *,
        include_deleted: bool = False,
    ) -> Attachment | None:
        """执行“get attachment”操作。

        参数：
            attachment_id (str): 附件唯一标识。
            session_id (str | None): 会话唯一标识。
            include_deleted (bool): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            Attachment | None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        async with get_session() as session:
            stmt = select(AttachmentModel).where(AttachmentModel.id == attachment_id)
            if session_id is not None:
                stmt = stmt.where(AttachmentModel.session_id == session_id)
            if not include_deleted:
                stmt = stmt.where(AttachmentModel.deleted_time.is_(None))
            row = (await session.execute(stmt)).scalar_one_or_none()
            return _attachment(row) if row else None

    async def get_attachments(
        self,
        session_id: str,
        attachment_ids: Iterable[str],
        *,
        include_deleted: bool = False,
    ) -> list[Attachment]:
        """通过一次数据库往返加载多个会话附件。"""
        ids = list(dict.fromkeys(attachment_ids))
        if not ids:
            return []
        async with get_session() as session:
            stmt = select(AttachmentModel).where(
                AttachmentModel.session_id == session_id,
                AttachmentModel.id.in_(ids),
            )
            if not include_deleted:
                stmt = stmt.where(AttachmentModel.deleted_time.is_(None))
            rows = (await session.execute(stmt)).scalars().all()
        by_id = {row.id: _attachment(row) for row in rows}
        return [by_id[item] for item in ids if item in by_id]

    async def list_attachments(self, session_id: str) -> list[Attachment]:
        """执行“list attachments”操作。

        参数：
            session_id (str): 会话唯一标识。

        返回值：
            list[Attachment]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        select(AttachmentModel)
                        .where(
                            AttachmentModel.session_id == session_id,
                            AttachmentModel.deleted_time.is_(None),
                        )
                        .order_by(AttachmentModel.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )
            return [_attachment(row) for row in rows]

    async def update_attachment(
        self, attachment_id: str, **values: Any
    ) -> Attachment | None:
        """更新附件字段（支持 capabilities 和 metadata 的便捷写入）。

        参数：
            attachment_id: 附件 ID。
            **values: 要更新的字段，支持 capabilities（自动序列化为 JSON）和
                metadata（自动序列化为 JSON）的便捷参数。

        返回值：
            更新后的 ``Attachment`` 实例，不存在时返回 ``None``。
        """
        allowed = {
            "message_id",
            "mime_type",
            "adapter_name",
            "adapter_version",
            "status",
            "error_message",
            "capabilities_json",
            "metadata_json",
        }
        payload = {k: v for k, v in values.items() if k in allowed}
        if "capabilities" in values:
            payload["capabilities_json"] = _json_dumps(values["capabilities"])
        if "metadata" in values:
            payload["metadata_json"] = _json_dumps(values["metadata"])
        payload["updated_at"] = _now().isoformat()
        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    update(AttachmentModel)
                    .where(
                        AttachmentModel.id == attachment_id,
                        AttachmentModel.deleted_time.is_(None),
                    )
                    .values(**payload)
                )
                row = (
                    await session.execute(
                        select(AttachmentModel).where(
                            AttachmentModel.id == attachment_id
                        )
                    )
                ).scalar_one_or_none()
                return _attachment(row) if row else None

    async def soft_delete_attachment(self, attachment_id: str, session_id: str) -> bool:
        """执行“soft delete attachment”操作。

        参数：
            attachment_id (str): 附件唯一标识。
            session_id (str): 会话唯一标识。

        返回值：
            bool: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        now = _now().isoformat()
        async with get_session() as session:
            async with session.begin():
                result = await session.execute(
                    update(AttachmentModel)
                    .where(
                        AttachmentModel.id == attachment_id,
                        AttachmentModel.session_id == session_id,
                        AttachmentModel.deleted_time.is_(None),
                    )
                    .values(
                        status=AttachmentStatus.DELETED.value,
                        deleted_time=now,
                        updated_at=now,
                    )
                )
                if not result.rowcount:
                    return False
                await session.execute(
                    text(
                        "DELETE FROM file_chunk_fts WHERE attachment_id = :attachment_id"
                    ),
                    {"attachment_id": attachment_id},
                )
                await session.execute(
                    update(ProcessingTaskModel)
                    .where(
                        ProcessingTaskModel.attachment_id == attachment_id,
                        ProcessingTaskModel.status.in_(
                            ["queued", "running", "waiting"]
                        ),
                    )
                    .values(
                        status=FileTaskStatus.CANCELLED.value,
                        updated_at=now,
                        completed_at=now,
                    )
                )
                await session.execute(
                    delete(FileChunkModel).where(
                        FileChunkModel.attachment_id == attachment_id
                    )
                )
                await session.execute(
                    delete(FileArtifactModel).where(
                        FileArtifactModel.attachment_id == attachment_id
                    )
                )
                await session.execute(
                    delete(CodeSymbolModel).where(
                        CodeSymbolModel.attachment_id == attachment_id
                    )
                )
                await session.execute(
                    delete(CodeDependencyModel).where(
                        CodeDependencyModel.attachment_id == attachment_id
                    )
                )
                await session.execute(
                    delete(MessageAttachmentModel).where(
                        MessageAttachmentModel.attachment_id == attachment_id
                    )
                )
                return True

    async def bind_message(
        self, session_id: str, message_id: str, attachment_ids: Iterable[str]
    ) -> list[Attachment]:
        """执行“bind message”操作。

        参数：
            session_id (str): 会话唯一标识。
            message_id (str): 消息唯一标识。
            attachment_ids (Iterable[str]): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            list[Attachment]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        ids = list(dict.fromkeys(attachment_ids))
        if not ids:
            return []
        async with get_session() as session:
            async with session.begin():
                rows = (
                    (
                        await session.execute(
                            select(AttachmentModel).where(
                                AttachmentModel.id.in_(ids),
                                AttachmentModel.session_id == session_id,
                                AttachmentModel.deleted_time.is_(None),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if len(rows) != len(ids):
                    raise PermissionError("附件不存在或不属于当前会话")
                for row in rows:
                    row.message_id = message_id
                    await session.execute(
                        sqlite_insert(MessageAttachmentModel)
                        .values(message_id=message_id, attachment_id=row.id)
                        .on_conflict_do_nothing()
                    )
        return [_attachment(row) for row in rows]

    async def attachments_for_messages(
        self, message_ids: Iterable[str]
    ) -> dict[str, list[Attachment]]:
        """执行“消息对应的附件”操作。

        参数：
            message_ids (Iterable[str]): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            dict[str, list[Attachment]]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        ids = list(message_ids)
        if not ids:
            return {}
        async with get_session() as session:
            result = await session.execute(
                select(MessageAttachmentModel.message_id, AttachmentModel)
                .join(
                    AttachmentModel,
                    AttachmentModel.id == MessageAttachmentModel.attachment_id,
                )
                .where(
                    MessageAttachmentModel.message_id.in_(ids),
                    AttachmentModel.deleted_time.is_(None),
                )
            )
            out: dict[str, list[Attachment]] = {}
            for message_id, row in result.all():
                out.setdefault(message_id, []).append(_attachment(row))
            return out

    async def create_task(
        self,
        session_id: str,
        attachment_id: str,
        task_type: FileTaskType,
        *,
        payload: dict[str, Any] | None = None,
        priority: int = 0,
        max_attempts: int = 3,
    ) -> FileTask:
        """创建处理任务（幂等：同附件同类型的进行中任务不重复创建）。

        参数：
            session_id: 会话 ID。
            attachment_id: 附件 ID。
            task_type: 任务类型（解析/索引/摘要/代码分析等）。
            payload: 任务参数。
            priority: 优先级（数值越大越优先）。
            max_attempts: 最大重试次数。

        返回值：
            创建的或已存在的 ``FileTask`` 实例。
        """
        now = _now().isoformat()
        row = ProcessingTaskModel(
            id=generate_time_id(),
            session_id=session_id,
            attachment_id=attachment_id,
            task_type=task_type.value,
            status=FileTaskStatus.QUEUED.value,
            progress=0,
            stage="queued",
            priority=priority,
            attempts=0,
            max_attempts=max_attempts,
            payload_json=_json_dumps(payload or {}),
            result_json="{}",
            available_at=now,
            created_at=now,
            updated_at=now,
        )
        async with get_session() as session:
            async with session.begin():
                existing = (
                    await session.execute(
                        select(ProcessingTaskModel)
                        .where(
                            ProcessingTaskModel.attachment_id == attachment_id,
                            ProcessingTaskModel.task_type == task_type.value,
                            ProcessingTaskModel.status.in_(
                                ["queued", "running", "waiting"]
                            ),
                        )
                        .order_by(ProcessingTaskModel.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return _task(existing)
                session.add(row)
        return _task(row)

    async def get_task(
        self, task_id: str, session_id: str | None = None
    ) -> FileTask | None:
        """执行“get task”操作。

        参数：
            task_id (str): 任务唯一标识。
            session_id (str | None): 会话唯一标识。

        返回值：
            FileTask | None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        async with get_session() as session:
            stmt = select(ProcessingTaskModel).where(ProcessingTaskModel.id == task_id)
            if session_id is not None:
                stmt = stmt.where(ProcessingTaskModel.session_id == session_id)
            row = (await session.execute(stmt)).scalar_one_or_none()
            return _task(row) if row else None

    async def list_tasks(
        self, session_id: str, attachment_id: str | None = None
    ) -> list[FileTask]:
        """执行“list tasks”操作。

        参数：
            session_id (str): 会话唯一标识。
            attachment_id (str | None): 附件唯一标识。

        返回值：
            list[FileTask]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        async with get_session() as session:
            stmt = select(ProcessingTaskModel).where(
                ProcessingTaskModel.session_id == session_id
            )
            if attachment_id:
                stmt = stmt.where(ProcessingTaskModel.attachment_id == attachment_id)
            rows = (
                (
                    await session.execute(
                        stmt.order_by(ProcessingTaskModel.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            return [_task(row) for row in rows]

    async def list_tasks_for_attachments(
        self, session_id: str, attachment_ids: Iterable[str]
    ) -> dict[str, list[FileTask]]:
        """通过一次查询加载多个附件的任务。"""
        ids = list(dict.fromkeys(attachment_ids))
        if not ids:
            return {}
        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        select(ProcessingTaskModel)
                        .where(
                            ProcessingTaskModel.session_id == session_id,
                            ProcessingTaskModel.attachment_id.in_(ids),
                        )
                        .order_by(ProcessingTaskModel.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
        result: dict[str, list[FileTask]] = {item: [] for item in ids}
        for row in rows:
            result.setdefault(row.attachment_id, []).append(_task(row))
        return result

    async def claim_next_task(self) -> FileTask | None:
        """原子认领下一个待执行任务（按优先级和创建时间排序）。

        将任务状态从 QUEUED 更新为 RUNNING，递增 attempts 计数。

        返回值：
            认领的 ``FileTask`` 实例，无可用任务时返回 ``None``。
        """
        now = _now().isoformat()
        async with get_session() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(ProcessingTaskModel)
                        .where(
                            ProcessingTaskModel.status == FileTaskStatus.QUEUED.value,
                            ProcessingTaskModel.available_at <= now,
                        )
                        .order_by(
                            ProcessingTaskModel.priority.desc(),
                            ProcessingTaskModel.created_at.asc(),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if row is None:
                    return None
                row.status = FileTaskStatus.RUNNING.value
                row.stage = "running"
                row.attempts += 1
                row.started_at = now
                row.updated_at = now
            return _task(row)

    async def update_task(self, task_id: str, **values: Any) -> None:
        """执行“update task”操作。

        参数：
            task_id (str): 任务唯一标识。
            values (Any): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        payload: dict[str, Any] = {}
        for key in (
            "status",
            "progress",
            "stage",
            "error_message",
            "available_at",
            "started_at",
            "completed_at",
        ):
            if key in values:
                value = values[key]
                payload[key] = value.value if hasattr(value, "value") else value
        if "result" in values:
            payload["result_json"] = _json_dumps(values["result"])
        payload["updated_at"] = _now().isoformat()
        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    update(ProcessingTaskModel)
                    .where(ProcessingTaskModel.id == task_id)
                    .values(**payload)
                )

    async def retry_or_fail_task(self, task: FileTask, error: str) -> None:
        """重试或标记任务失败（指数退避策略）。

        未超过最大重试次数时，按 ``2^attempts`` 秒的退避时间重新入队；
        超过时标记为 FAILED。

        参数：
            task: 当前任务实例。
            error: 错误信息。
        """
        now = _now()
        if task.attempts < task.max_attempts:
            await self.update_task(
                task.id,
                status=FileTaskStatus.QUEUED,
                stage="retrying",
                error_message=error,
                available_at=(now + timedelta(seconds=2**task.attempts)).isoformat(),
            )
        else:
            await self.update_task(
                task.id,
                status=FileTaskStatus.FAILED,
                stage="failed",
                progress=1.0,
                error_message=error,
                completed_at=now.isoformat(),
            )

    async def recover_running_tasks(self) -> int:
        """执行“recover running tasks”操作。

        返回值：
            int: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        now = _now().isoformat()
        async with get_session() as session:
            async with session.begin():
                result = await session.execute(
                    update(ProcessingTaskModel)
                    .where(ProcessingTaskModel.status.in_(["running", "waiting"]))
                    .values(
                        status="queued",
                        stage="recovered",
                        available_at=now,
                        updated_at=now,
                    )
                )
                return int(result.rowcount or 0)

    async def replace_chunks(self, attachment_id: str, chunks: list[FileChunk]) -> None:
        """执行“replace chunks”操作。

        参数：
            attachment_id (str): 附件唯一标识。
            chunks (list[FileChunk]): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        async with get_session() as session:
            async with session.begin():
                existing_ids = (
                    (
                        await session.execute(
                            select(FileChunkModel.id).where(
                                FileChunkModel.attachment_id == attachment_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if existing_ids:
    # FTS5 没有 SQLAlchemy 表模型；为每个 ID 安全绑定参数。
                    placeholders = ", ".join(
                        f":id{i}" for i in range(len(existing_ids))
                    )
                    await session.execute(
                        text(
                            f"DELETE FROM file_chunk_fts WHERE chunk_id IN ({placeholders})"
                        ),
                        {f"id{i}": value for i, value in enumerate(existing_ids)},
                    )
                await session.execute(
                    delete(FileChunkModel).where(
                        FileChunkModel.attachment_id == attachment_id
                    )
                )
                for chunk in chunks:
                    session.add(
                        FileChunkModel(
                            id=chunk.id,
                            attachment_id=attachment_id,
                            ordinal=chunk.ordinal,
                            content=chunk.content,
                            token_count=chunk.token_count,
                            locator_json=_json_dumps(chunk.locator),
                            metadata_json=_json_dumps(chunk.metadata),
                        )
                    )
                    await session.execute(
                        text(
                            "INSERT INTO file_chunk_fts(content, chunk_id, attachment_id) VALUES (:content, :chunk_id, :attachment_id)"
                        ),
                        {
                            "content": chunk.content,
                            "chunk_id": chunk.id,
                            "attachment_id": attachment_id,
                        },
                    )

    async def get_chunks(
        self, attachment_id: str, *, offset: int = 0, limit: int = 50
    ) -> list[FileChunk]:
        """执行“get chunks”操作。

        参数：
            attachment_id (str): 附件唯一标识。
            offset (int): 分页偏移量；应为非负整数。
            limit (int): 最大返回数量；应为非负整数。

        返回值：
            list[FileChunk]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        select(FileChunkModel)
                        .where(FileChunkModel.attachment_id == attachment_id)
                        .order_by(FileChunkModel.ordinal)
                        .offset(offset)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [
                FileChunk(
                    id=r.id,
                    attachment_id=r.attachment_id,
                    ordinal=r.ordinal,
                    content=r.content,
                    token_count=r.token_count,
                    locator=_json_loads(r.locator_json, {}),
                    metadata=_json_loads(r.metadata_json, {}),
                )
                for r in rows
            ]

    async def search_chunks(
        self, attachment_id: str, query: str, limit: int = 10
    ) -> list[FileChunk]:
        """执行“search chunks”操作。

        参数：
            attachment_id (str): 附件唯一标识。
            query (str): 检索或搜索文本；应为非空字符串。
            limit (int): 最大返回数量；应为非负整数。

        返回值：
            list[FileChunk]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        async with get_session() as session:
            rows = []
            ranks: dict[str, float] = {}
            fts_succeeded = False
            match_expr = _fts_match_expression(query)
            try:
                if match_expr is None:
                    raise ValueError("query has no FTS tokens")
                fts_rows = list(
                    (
                        await session.execute(
                            text(
                                "SELECT chunk_id, bm25(file_chunk_fts) AS rank "
                                "FROM file_chunk_fts "
                                "WHERE attachment_id = :attachment_id "
                                "AND file_chunk_fts MATCH :match_expr "
                                "ORDER BY rank LIMIT :limit"
                            ),
                            {
                                "attachment_id": attachment_id,
                                "match_expr": match_expr,
                                "limit": limit,
                            },
                        )
                    )
                    .all()
                )
                fts_succeeded = True
                chunk_ids = [row.chunk_id for row in fts_rows]
                ranks = {row.chunk_id: float(row.rank) for row in fts_rows}
                if chunk_ids:
                    fetched = (
                        (
                            await session.execute(
                                select(FileChunkModel).where(
                                    FileChunkModel.id.in_(chunk_ids)
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    by_id = {row.id: row for row in fetched}
                    rows = [by_id[item] for item in chunk_ids if item in by_id]
            except (SQLAlchemyError, ValueError):
                rows = []
            if not fts_succeeded:
                rows = (
                    (
                        await session.execute(
                            select(FileChunkModel)
                            .where(
                                FileChunkModel.attachment_id == attachment_id,
                                FileChunkModel.content.contains(query),
                            )
                            .order_by(FileChunkModel.ordinal)
                            .limit(limit)
                        )
                    )
                    .scalars()
                    .all()
                )
            return [
                FileChunk(
                    id=r.id,
                    attachment_id=r.attachment_id,
                    ordinal=r.ordinal,
                    content=r.content,
                    token_count=r.token_count,
                    locator=_json_loads(r.locator_json, {}),
                    metadata=_json_loads(r.metadata_json, {}),
                    native_score=ranks.get(r.id) if fts_succeeded else None,
                )
                for r in rows
            ]

    async def get_artifact(self, cache_key: str) -> dict[str, Any] | None:
        """执行“get artifact”操作。

        参数：
            cache_key (str): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            dict[str, Any] | None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        async with get_session() as session:
            row = (
                await session.execute(
                    select(FileArtifactModel).where(
                        FileArtifactModel.cache_key == cache_key
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "id": row.id,
                "kind": row.kind,
                "content": row.content,
                "storage_key": row.storage_key,
                "metadata": _json_loads(row.metadata_json, {}),
            }

    async def put_artifact(
        self,
        attachment_id: str,
        kind: str,
        cache_key: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """执行“put artifact”操作。

        参数：
            attachment_id (str): 附件唯一标识。
            kind (str): 输入参数；其类型和取值约束由方法签名及实现定义。
            cache_key (str): 输入参数；其类型和取值约束由方法签名及实现定义。
            content (str): 待保存或处理的内容。
            metadata (dict[str, Any] | None): 附加元数据字典。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    sqlite_insert(FileArtifactModel)
                    .values(
                        id=generate_time_id(),
                        attachment_id=attachment_id,
                        kind=kind,
                        cache_key=cache_key,
                        content=content,
                        metadata_json=_json_dumps(metadata or {}),
                        created_at=_now().isoformat(),
                    )
                    .on_conflict_do_update(
                        index_elements=[FileArtifactModel.cache_key],
                        set_={
                            "content": content,
                            "metadata_json": _json_dumps(metadata or {}),
                            "created_at": _now().isoformat(),
                        },
                    )
                )

    async def sync_adapters(self, adapters: Iterable[AdapterInfo]) -> None:
        """执行“sync adapters”操作。

        参数：
            adapters (Iterable[AdapterInfo]): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        items = list(adapters)
        names = [item.name for item in items]
        now = _now().isoformat()
        async with get_session() as session:
            async with session.begin():
                if names:
                    await session.execute(
                        delete(AdapterRegistryModel).where(
                            AdapterRegistryModel.name.not_in(names)
                        )
                    )
                else:
                    await session.execute(delete(AdapterRegistryModel))
                for item in items:
                    await session.execute(
                        sqlite_insert(AdapterRegistryModel)
                        .values(
                            name=item.name,
                            version=item.version,
                            mime_types_json=_json_dumps(item.mime_types),
                            extensions_json=_json_dumps(item.extensions),
                            capabilities_json=_json_dumps(item.capabilities),
                            enabled=int(item.enabled),
                            updated_at=now,
                        )
                        .on_conflict_do_update(
                            index_elements=[AdapterRegistryModel.name],
                            set_={
                                "version": item.version,
                                "mime_types_json": _json_dumps(item.mime_types),
                                "extensions_json": _json_dumps(item.extensions),
                                "capabilities_json": _json_dumps(item.capabilities),
                                "enabled": int(item.enabled),
                                "updated_at": now,
                            },
                        )
                    )

    async def replace_code_index(
        self,
        attachment_id: str,
        symbols: list[dict[str, Any]],
        dependencies: list[dict[str, Any]],
    ) -> None:
        """替换附件的代码索引（先删后增，同一事务内完成）。"""
        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    delete(CodeSymbolModel).where(
                        CodeSymbolModel.attachment_id == attachment_id
                    )
                )
                await session.execute(
                    delete(CodeDependencyModel).where(
                        CodeDependencyModel.attachment_id == attachment_id
                    )
                )
                for symbol in symbols:
                    session.add(
                        CodeSymbolModel(
                            id=generate_time_id(), attachment_id=attachment_id, **symbol
                        )
                    )
                for dep in dependencies:
                    values = dict(dep)
                    metadata = values.pop("metadata", {})
                    session.add(
                        CodeDependencyModel(
                            id=generate_time_id(),
                            attachment_id=attachment_id,
                            metadata_json=_json_dumps(metadata),
                            **values,
                        )
                    )

    async def find_symbols(
        self, attachment_id: str, name: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """按名称模糊搜索代码符号。

        参数：
            attachment_id: 附件 ID。
            name: 符号名称关键词。
            limit: 最大返回数。

        返回值：
            符号信息列表（含 name/qualified_name/kind/path/language 等字段）。
        """
        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        select(CodeSymbolModel)
                        .where(
                            CodeSymbolModel.attachment_id == attachment_id,
                            CodeSymbolModel.name.contains(name),
                        )
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [
                {
                    "name": r.name,
                    "qualified_name": r.qualified_name,
                    "kind": r.kind,
                    "path": r.path,
                    "language": r.language,
                    "start_line": r.start_line,
                    "end_line": r.end_line,
                    "signature": r.signature,
                }
                for r in rows
            ]

    async def find_dependencies(
        self, attachment_id: str, symbol: str, direction: str = "both", limit: int = 100
    ) -> list[dict[str, Any]]:
        """查询代码符号的依赖关系。

        参数：
            attachment_id: 附件 ID。
            symbol: 符号名称。
            direction: ``"outgoing"``（调用）、``"incoming"``（被调用）、``"both"``。
            limit: 最大返回数。

        返回值：
            依赖关系列表（含 source/target/kind/metadata 字段）。
        """
        async with get_session() as session:
            stmt = select(CodeDependencyModel).where(
                CodeDependencyModel.attachment_id == attachment_id
            )
            if direction == "outgoing":
                stmt = stmt.where(CodeDependencyModel.source.contains(symbol))
            elif direction == "incoming":
                stmt = stmt.where(CodeDependencyModel.target.contains(symbol))
            else:
                stmt = stmt.where(
                    (CodeDependencyModel.source.contains(symbol))
                    | (CodeDependencyModel.target.contains(symbol))
                )
            rows = (await session.execute(stmt.limit(limit))).scalars().all()
            return [
                {
                    "source": r.source,
                    "target": r.target,
                    "kind": r.kind,
                    "metadata": _json_loads(r.metadata_json, {}),
                }
                for r in rows
            ]
