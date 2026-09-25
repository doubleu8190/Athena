"""文件智能的 PostgreSQL 持久化。

``FileRepository`` 封装所有 File Intelligence 相关的数据库操作，
遵循 Repository 模式：查询方法返回类型化领域模型，写入方法接受类型化模型。
所有删除操作为软删除（设置 deleted_time）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, cast

from sqlalchemy import delete, exists, func, or_, select, text, update
from sqlalchemy.orm import aliased
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.engine import CursorResult

from athena.infrastructure.postgre.engine import get_session
from athena.infrastructure.postgre.models import (
    AdapterRegistryModel,
    AttachmentModel,
    CodeDependencyModel,
    CodeSymbolModel,
    FileArtifactModel,
    FileChunkModel,
)
from athena.utils import logging
from .repository_utils import (
    _json_dumps,
    _json_loads,
    _json_loads_model,
    _strip_nul,
)
from .model_converters import _row_to_attachment
from athena.models.file import (
    AdapterInfo,
    Attachment,
    AttachmentStatus,
    FileChunk,
)
from athena.models.json_models import (
    FileArtifact,
    FileLocator,
    FileMetadata,
)
from athena.utils.id_generation import generate_time_id

logger = logging.get_logger(__name__)


def _now() -> datetime:
    """返回当前时间。"""
    return datetime.now()


class FileRepository:
    """File Intelligence 持久化仓库。

    管理附件、分块、产物、代码索引和适配器注册表的 CRUD 操作。
    所有写入方法使用事务保证原子性。
    """

    async def soft_delete_session_attachments(self, session_id: str) -> None:
        """软删除会话的全部附件，并清理其派生索引和产物。"""
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
                    update(FileChunkModel)
                    .where(FileChunkModel.attachment_id.in_(attachment_ids))
                    .values(deleted_time=now)
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

    async def list_live_storage_keys(self) -> set[str]:
        """

        返回值：
            set[str]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
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
        session_id: str | None = None,
        knowledge_base_id: str | None = None,
        message_id: str | None = None,
        filename: str,
        mime_type: str,
        size_bytes: int,
        sha256: str,
        storage_key: str,
        logical_document_id: str | None = None,
    ) -> Attachment:
        """创建会话附件或知识库文档记录（上传完成后调用）。

        参数：
            session_id: 会话 ID；创建知识库文档时为空。
            knowledge_base_id: 知识库 ID；创建会话附件时为空。
            message_id: 关联的用户消息 ID；multipart 消息提交时由 Gateway 预先分配。
            filename: 原始文件名。
            mime_type: MIME 类型。
            size_bytes: 文件字节数。
            sha256: 文件内容的 SHA-256 哈希。
            storage_key: 存储层的 blob 键。

        返回值：
            创建的 ``Attachment`` 实例。
        """
        if (session_id is None) == (knowledge_base_id is None):
            raise ValueError("附件必须且只能属于一个会话或知识库")
        now = _now().isoformat()
        attachment_id = generate_time_id()
        document_version = 1
        if logical_document_id is not None and knowledge_base_id is not None:
            async with get_session() as session:
                latest_version = (
                    await session.execute(
                        select(func.max(AttachmentModel.document_version)).where(
                            AttachmentModel.logical_document_id == logical_document_id,
                            AttachmentModel.knowledge_base_id == knowledge_base_id,
                        )
                    )
                ).scalar_one()
            document_version = int(latest_version or 0) + 1
            logger.info("知识库中 %s 新版本为: %s", filename, document_version)

        row = AttachmentModel(
            id=attachment_id,
            logical_document_id=logical_document_id or attachment_id,
            document_version=document_version,
            session_id=session_id,
            knowledge_base_id=knowledge_base_id,
            message_id=message_id,
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
                if document_version > 1:
                    await session.execute(
                        update(AttachmentModel)
                        .where(
                            AttachmentModel.logical_document_id == logical_document_id,
                            AttachmentModel.knowledge_base_id == knowledge_base_id,
                            AttachmentModel.deleted_time.is_(None),
                        )
                        .values(deleted_time=now)
                    )
                    await session.execute(
                        update(FileChunkModel)
                        .where(
                            FileChunkModel.attachment_id.in_(
                                select(AttachmentModel.id).where(
                                    AttachmentModel.logical_document_id
                                    == logical_document_id,
                                    AttachmentModel.knowledge_base_id
                                    == knowledge_base_id,
                                    AttachmentModel.deleted_time.is_not(None),
                                )
                            )
                        )
                        .values(deleted_time=now)
                    )
                    logger.info(
                        "%s 旧版本已删除",
                        filename,
                    )

                session.add(row)
                logger.info(
                    "%s 创建新附件 %s，逻辑文档 %s，版本 %s",
                    filename,
                    attachment_id,
                    logical_document_id,
                    document_version,
                )
        return _row_to_attachment(row)

    async def get_attachment(
        self,
        attachment_id: str,
        session_id: str | None = None,
        *,
        include_deleted: bool = False,
    ) -> Attachment | None:
        """

        参数：
            attachment_id (str): 附件唯一标识。
            session_id (str | None): 会话唯一标识。
            include_deleted (bool): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            Attachment | None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        async with get_session() as session:
            stmt = select(AttachmentModel).where(AttachmentModel.id == attachment_id)
            if session_id is not None:
                stmt = stmt.where(AttachmentModel.session_id == session_id)
            if not include_deleted:
                stmt = stmt.where(AttachmentModel.deleted_time.is_(None))
            row = (await session.execute(stmt)).scalar_one_or_none()
            return _row_to_attachment(row) if row else None

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
        # IN 查询不保证返回顺序；当前调用方会自行按 ID 查找或重排，因此无需构建索引。
        return [_row_to_attachment(row) for row in rows]

    async def list_session_attachments(self, session_id: str) -> list[Attachment]:
        """列出直接上传到当前会话的附件。

        参数：
            session_id (str): 当前会话唯一标识。

        返回值：
        list[Attachment]: 当前会话附件，按创建时间升序排列。

        异常：
            数据库读取失败时传播底层异常。
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
        return [_row_to_attachment(row) for row in rows]

    async def list_global_knowledge_documents(self) -> list[Attachment]:
        """列出所有全局知识库文档。

        返回值：
            list[Attachment]: 所有未删除的知识库文档，按创建时间升序排列。

        异常：
            数据库读取失败时传播底层异常。
        """
        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        select(AttachmentModel)
                        .where(
                            AttachmentModel.knowledge_base_id.is_not(None),
                            AttachmentModel.deleted_time.is_(None),
                        )
                        .order_by(
                            AttachmentModel.created_at.desc(),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [_row_to_attachment(row) for row in rows]

    async def list_global_knowledge_ready_documents(self) -> list[Attachment]:
        """列出所有全局知识库文档。

        返回值：
            list[Attachment]: 所有未删除的知识库文档，按创建时间升序排列。

        异常：
            数据库读取失败时传播底层异常。
        """
        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        select(AttachmentModel)
                        .where(
                            AttachmentModel.knowledge_base_id.is_not(None),
                            AttachmentModel.status == AttachmentStatus.READY.value,
                            AttachmentModel.deleted_time.is_(None),
                        )
                        .order_by(
                            AttachmentModel.created_at.desc(),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [_row_to_attachment(row) for row in rows]

    async def list_knowledge_ready_documents(
        self, knowledge_base_ids: list[str]
    ) -> list[Attachment]:
        """列出所有全局知识库文档。

        返回值：
            list[Attachment]: 所有未删除的知识库文档，按创建时间升序排列。

        异常：
            数据库读取失败时传播底层异常。
        """
        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        select(AttachmentModel).where(
                            AttachmentModel.knowledge_base_id.in_(knowledge_base_ids),
                            AttachmentModel.status == AttachmentStatus.READY.value,
                            AttachmentModel.deleted_time.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [_row_to_attachment(row) for row in rows]

    async def find_latest_knowledge_document(
        self, knowledge_base_id: str, filename: str
    ) -> Attachment | None:
        """按知识库和文件名找到当前逻辑文档版本。

        参数：
            knowledge_base_id: 知识库唯一标识。
            filename: 规范化后的文件名。

        返回值：
            当前未删除文档；不存在时返回 ``None``。

        异常：
            数据库读取失败时传播底层异常。
        """
        async with get_session() as session:
            row = (
                await session.execute(
                    select(AttachmentModel).where(
                        AttachmentModel.knowledge_base_id == knowledge_base_id,
                        AttachmentModel.filename == filename,
                        AttachmentModel.deleted_time.is_(None),
                    )
                )
            ).scalar_one_or_none()
        return _row_to_attachment(row) if row is not None else None

    async def list_document_versions(
        self, attachment_id: str, knowledge_base_id: str
    ) -> list[Attachment]:
        """读取同一逻辑文档的全部上传版本。

        参数：
            attachment_id: 任意一个文档版本的附件 ID。
            knowledge_base_id: 文档所属知识库 ID。

        返回值：
            按文档版本序号升序排列的附件版本，包含历史和已删除版本。

        异常：
            数据库读取失败时传播底层异常。
        """
        async with get_session() as session:
            target = (
                await session.execute(
                    select(AttachmentModel).where(
                        AttachmentModel.id == attachment_id,
                        AttachmentModel.knowledge_base_id == knowledge_base_id,
                    )
                )
            ).scalar_one_or_none()
            if target is None:
                return []
            rows = (
                (
                    await session.execute(
                        select(AttachmentModel)
                        .where(
                            AttachmentModel.logical_document_id
                            == target.logical_document_id,
                            AttachmentModel.knowledge_base_id == knowledge_base_id,
                        )
                        .order_by(AttachmentModel.document_version.asc())
                    )
                )
                .scalars()
                .all()
            )
        return [_row_to_attachment(row) for row in rows]

    async def get_accessible_attachment(
        self, session_id: str, attachment_id: str
    ) -> Attachment | None:
        """读取会话直接拥有或全局知识库中的文档。

        参数：
            session_id (str): 当前会话唯一标识。
            attachment_id (str): 目标附件唯一标识。

        返回值：
            Attachment | None: 无访问权限、已删除或不存在时返回 ``None``。

        异常：
            数据库读取失败时传播底层异常。
        """
        async with get_session() as session:
            row = (
                await session.execute(
                    select(AttachmentModel).where(
                        AttachmentModel.id == attachment_id,
                        or_(
                            AttachmentModel.session_id == session_id,
                            AttachmentModel.knowledge_base_id.is_not(None),
                        ),
                        AttachmentModel.deleted_time.is_(None),
                    )
                )
            ).scalar_one_or_none()
        return _row_to_attachment(row) if row is not None else None

    async def list_knowledge_base_attachments(
        self, knowledge_base_id: str
    ) -> list[Attachment]:
        """列出知识库中每个逻辑文档的当前版本。

        参数：
            knowledge_base_id (str): 知识库唯一标识。

        返回值：
            list[Attachment]: 按创建时间升序排列的知识库文档。

        异常：
            数据库读取失败时传播底层异常。
        """
        documents = await self.list_global_knowledge_documents()
        return [
            document
            for document in documents
            if document.knowledge_base_id == knowledge_base_id
        ]

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
            "parsed_metadata_json",
        }
        payload = {k: v for k, v in values.items() if k in allowed}
        if "capabilities" in values:
            payload["capabilities_json"] = _json_dumps(values["capabilities"])
        if "metadata" in values:
            payload["parsed_metadata_json"] = _json_dumps(values["metadata"])
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
                return _row_to_attachment(row) if row else None

    async def soft_delete_attachment(self, attachment_id: str, session_id: str) -> bool:
        """

        参数：
            attachment_id (str): 附件唯一标识。
            session_id (str): 会话唯一标识。

        返回值：
            bool: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
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
                if not cast(CursorResult[Any], result).rowcount:
                    return False
                await session.execute(
                    update(FileChunkModel)
                    .where(FileChunkModel.attachment_id == attachment_id)
                    .values(deleted_time=now)
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
                return True

    async def soft_delete_knowledge_base_attachment(
        self, attachment_id: str, knowledge_base_id: str
    ) -> bool:
        """软删除知识库中的指定文档及其派生索引。

        参数：
            attachment_id (str): 文档附件唯一标识。
            knowledge_base_id (str): 文档必须归属的知识库标识。

        返回值：
            bool: 找到并删除文档时为 ``True``。

        异常：
            数据库写入失败时传播底层异常。
        """
        return await self._soft_delete_owned_attachment(
            attachment_id,
            knowledge_base_id=knowledge_base_id,
        )

    async def _soft_delete_owned_attachment(
        self,
        attachment_id: str,
        *,
        knowledge_base_id: str,
    ) -> bool:
        """按知识库所有权软删除附件并清理 PostgreSQL 派生数据。

        参数：
            attachment_id (str): 文档附件唯一标识。
            knowledge_base_id (str): 文档所属知识库标识。

        返回值：
            bool: 发生删除时为 ``True``。

        异常：
            数据库写入失败时传播底层异常。
        """
        now = _now().isoformat()
        async with get_session() as session:
            async with session.begin():
                result = await session.execute(
                    update(AttachmentModel)
                    .where(
                        AttachmentModel.id == attachment_id,
                        AttachmentModel.knowledge_base_id == knowledge_base_id,
                        AttachmentModel.deleted_time.is_(None),
                    )
                    .values(
                        status=AttachmentStatus.DELETED.value,
                        deleted_time=now,
                        updated_at=now,
                    )
                )
                if not cast(CursorResult[Any], result).rowcount:
                    return False
                await session.execute(
                    update(FileChunkModel)
                    .where(FileChunkModel.attachment_id == attachment_id)
                    .values(deleted_time=now)
                )
                for model in (FileArtifactModel, CodeSymbolModel, CodeDependencyModel):
                    await session.execute(
                        delete(model).where(model.attachment_id == attachment_id)
                    )
                return True

    async def bind_message(
        self, session_id: str, message_id: str, attachment_ids: Iterable[str]
    ) -> list[Attachment]:
        """

        参数：
            session_id (str): 会话唯一标识。
            message_id (str): 消息唯一标识。
            attachment_ids (Iterable[str]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            list[Attachment]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
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
                    if row.message_id not in (None, message_id):
                        raise ValueError("附件已关联其他消息")
                    row.message_id = message_id
        return [_row_to_attachment(row) for row in rows]

    async def attachments_for_messages(
        self, message_ids: Iterable[str]
    ) -> dict[str, list[Attachment]]:
        """

        参数：
            message_ids (Iterable[str]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            dict[str, list[Attachment]]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        ids = list(message_ids)
        if not ids:
            return {}
        async with get_session() as session:
            result = await session.execute(
                select(AttachmentModel).where(
                    AttachmentModel.message_id.in_(ids),
                    AttachmentModel.deleted_time.is_(None),
                )
            )
            out: dict[str, list[Attachment]] = {}
            for row in result.scalars().all():
                if row.message_id is not None:
                    out.setdefault(row.message_id, []).append(_row_to_attachment(row))
            return out

    async def replace_chunks(self, attachment_id: str, chunks: list[FileChunk]) -> None:
        """

        参数：
            attachment_id (str): 附件唯一标识。
            chunks (list[FileChunk]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            None: 分块版本由所属附件的 ``document_version`` 表示。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
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
                attachment = await session.get(AttachmentModel, attachment_id)
                if attachment is None:
                    raise FileNotFoundError("附件不存在")
                if existing_ids:
                    await session.execute(
                        update(FileChunkModel)
                        .where(FileChunkModel.id.in_(existing_ids))
                        .values(deleted_time=_now().isoformat())
                    )
                attachment.updated_at = _now().isoformat()
                for chunk in chunks:
                    # PDF/Office 解析器可能把源文件中的 NUL 控制字节带入文本。
                    # PostgreSQL TEXT 和 psycopg 都拒绝该字符，因此主表与 FTS
                    # 索引必须使用同一份清理后的内容，避免两边出现分歧。
                    content = _strip_nul(chunk.content)
                    session.add(
                        FileChunkModel(
                            id=chunk.id,
                            attachment_id=attachment_id,
                            ordinal=chunk.ordinal,
                            content=content,
                            token_count=chunk.token_count,
                            locator_json=_json_dumps(chunk.locator),
                            metadata_json=_json_dumps(chunk.metadata),
                            deleted_time=None,
                        )
                    )
        return None

    async def get_chunks(
        self, attachment_id: str, *, offset: int = 0, limit: int = 50
    ) -> list[FileChunk]:
        """

        参数：
            attachment_id (str): 附件唯一标识。
            offset (int): 分页偏移量；应为非负整数。
            limit (int): 最大返回数量；应为非负整数。

        返回值：
            list[FileChunk]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        select(FileChunkModel)
                        .join(
                            AttachmentModel,
                            AttachmentModel.id == FileChunkModel.attachment_id,
                        )
                        .where(
                            FileChunkModel.attachment_id == attachment_id,
                            FileChunkModel.deleted_time.is_(None),
                            AttachmentModel.deleted_time.is_(None),
                        )
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
                    locator=_json_loads_model(
                        r.locator_json, FileLocator, FileLocator()
                    ),
                    metadata=_json_loads_model(
                        r.metadata_json, FileMetadata, FileMetadata()
                    ),
                )
                for r in rows
            ]

    async def search_chunks(
        self, attachment_id: str, query: str, limit: int = 10
    ) -> list[FileChunk]:
        """

        参数：
            attachment_id (str): 附件唯一标识。
            query (str): 检索或搜索文本；应为非空字符串。
            limit (int): 最大返回数量；应为非负整数。

        返回值：
            list[FileChunk]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        if not query.strip():
            return []
        async with get_session() as session:
            rows = (await session.execute(
                select(
                    FileChunkModel,
                    func.ts_rank_cd(
                        FileChunkModel.content_fts,
                        func.websearch_to_tsquery("chinese", query),
                    ).label("fts_score"),
                )
                .where(
                    FileChunkModel.attachment_id == attachment_id,
                    FileChunkModel.deleted_time.is_(None),
                    FileChunkModel.content_fts.op("@@")(
                        func.websearch_to_tsquery("chinese", query)
                    ),
                )
                .order_by(text("fts_score DESC"))
                .limit(limit)
            )).all()
            return [
                FileChunk(
                    id=r.id,
                    attachment_id=r.attachment_id,
                    ordinal=r.ordinal,
                    content=r.content,
                    token_count=r.token_count,
                    locator=_json_loads_model(
                        r.locator_json, FileLocator, FileLocator()
                    ),
                    metadata=_json_loads_model(
                        r.metadata_json, FileMetadata, FileMetadata()
                    ),
                    native_score=float(fts_score),
                )
                for r, fts_score in rows
            ]

    async def search_knowledge_chunks(
        self,
        query: str,
        document_ids: set[str],
        limit: int = 10,
    ) -> list[FileChunk]:
        """跨全部未删除知识库文档执行 PostgreSQL 原生 FTS 检索。

        参数：
            query：全文检索查询文本。
            limit：最大结果数。

        返回：
            按 PostgreSQL FTS 相关度排序的文件分块。

        异常：
            数据库读取失败时向上抛出异常；无有效 FTS token 时返回空列表。
        """
        if not query.strip():
            return []
        async with get_session() as session:
            rows = (await session.execute(
                select(
                    FileChunkModel,
                    func.ts_rank_cd(
                        FileChunkModel.content_fts,
                        func.websearch_to_tsquery("chinese", query),
                    ).label("fts_score"),
                )
                .join(
                    AttachmentModel,
                    AttachmentModel.id == FileChunkModel.attachment_id,
                )
                .where(
                    AttachmentModel.id.in_(document_ids),
                    FileChunkModel.deleted_time.is_(None),
                    FileChunkModel.content_fts.op("@@")(
                        func.websearch_to_tsquery("chinese", query)
                    ),
                )
                .order_by(text("fts_score DESC"))
                .limit(limit)
            )).all()
        return [
            FileChunk(
                id=chunk.id,
                attachment_id=chunk.attachment_id,
                ordinal=chunk.ordinal,
                content=chunk.content,
                token_count=chunk.token_count,
                locator=_json_loads_model(
                    chunk.locator_json, FileLocator, FileLocator()
                ),
                metadata=_json_loads_model(
                    chunk.metadata_json, FileMetadata, FileMetadata()
                ),
                native_score=float(fts_score),
            )
            for chunk, fts_score in rows
        ]

    async def get_artifact(self, cache_key: str) -> FileArtifact | None:
        """

        参数：
            cache_key (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            FileArtifact | None: 返回文件产物模型；不存在时返回 ``None``。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
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
            return FileArtifact(
                id=row.id,
                kind=row.kind,
                content=row.content,
                storage_key=row.storage_key,
                metadata=_json_loads_model(
                    row.metadata_json, FileMetadata, FileMetadata()
                ),
            )

    async def put_artifact(
        self,
        attachment_id: str,
        kind: str,
        cache_key: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """

        参数：
            attachment_id (str): 附件唯一标识。
            kind (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            cache_key (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            content (str): 待保存或处理的内容。
            metadata (dict[str, Any] | None): 附加元数据字典。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    postgres_insert(FileArtifactModel)
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
        """

        参数：
            adapters (Iterable[AdapterInfo]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
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
                        postgres_insert(AdapterRegistryModel)
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
