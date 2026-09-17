"""文件智能的 SQLite 持久化。

``FileRepository`` 封装所有 File Intelligence 相关的数据库操作，
遵循 Repository 模式：查询方法返回类型化领域模型，写入方法接受类型化模型。
所有删除操作为软删除（设置 deleted_time）。
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Iterable, cast

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError

from athena.infrastructure.sqlite.engine import get_session
from athena.infrastructure.sqlite.models import (
    AdapterRegistryModel,
    AttachmentModel,
    CodeDependencyModel,
    CodeSymbolModel,
    FileArtifactModel,
    FileChunkModel,
)
from athena.infrastructure.sqlite.repositories import (
    _json_dumps,
    _json_loads,
    _json_loads_model,
)
from athena.infrastructure.sqlite.repositories.converters import _row_to_attachment
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
from athena.utils.ids import generate_time_id

_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+(?:[.-][A-Za-z0-9_]+)*|[\u4e00-\u9fff]+")


def _fts_match_expression(query: str) -> str | None:
    """构造带引号的 FTS5 表达式，阻止查询运算符直接进入全文检索语法。

    参数:
        query (str): 用户输入的检索词。
    返回值:
        str | None: 由安全词元组成的 OR 表达式；没有有效词元时返回 ``None``。
    异常:
        不抛出业务异常。
    """
    tokens = _FTS_TOKEN_RE.findall(query)
    if not tokens:
        return None
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _now() -> datetime:
    """返回当前时间。"""
    return datetime.now()


class FileRepository:
    """File Intelligence 持久化仓库。

    管理附件、分块、产物、代码索引和适配器注册表的 CRUD 操作。
    所有写入方法使用事务保证原子性。
    """

    async def delete_session(self, session_id: str) -> None:
        """软删除会话的所有文件记录。"""
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
    async def live_storage_keys(self) -> set[str]:
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
        row = AttachmentModel(
            id=generate_time_id(),
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
                session.add(row)
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

    async def list_attachments(self, session_id: str) -> list[Attachment]:
        """

        参数：
            session_id (str): 会话唯一标识。

        返回值：
            list[Attachment]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
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
                await session.execute(
                    select(AttachmentModel)
                    .where(
                        AttachmentModel.session_id == session_id,
                        AttachmentModel.deleted_time.is_(None),
                    )
                    .order_by(AttachmentModel.created_at.asc())
                )
            ).scalars().all()
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
                await session.execute(
                    select(AttachmentModel)
                    .where(
                        AttachmentModel.knowledge_base_id.is_not(None),
                        AttachmentModel.deleted_time.is_(None),
                    )
                    .order_by(AttachmentModel.created_at.asc())
                )
            ).scalars().all()
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
        """列出知识库中的全部未删除文档。

        参数：
            knowledge_base_id (str): 知识库唯一标识。

        返回值：
            list[Attachment]: 按创建时间升序排列的知识库文档。

        异常：
            数据库读取失败时传播底层异常。
        """
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(AttachmentModel)
                    .where(
                        AttachmentModel.knowledge_base_id == knowledge_base_id,
                        AttachmentModel.deleted_time.is_(None),
                    )
                    .order_by(AttachmentModel.created_at.asc())
                )
            ).scalars().all()
        return [_row_to_attachment(row) for row in rows]

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
                    text(
                        "DELETE FROM file_chunk_fts WHERE attachment_id = :attachment_id"
                    ),
                    {"attachment_id": attachment_id},
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
        """按知识库所有权软删除附件并清理 SQLite 派生数据。

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
                    text(
                        "DELETE FROM file_chunk_fts WHERE attachment_id = :attachment_id"
                    ),
                    {"attachment_id": attachment_id},
                )
                for model in (
                    FileChunkModel,
                    FileArtifactModel,
                    CodeSymbolModel,
                    CodeDependencyModel,
                ):
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
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

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
                    ).all()
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
                    locator=_json_loads_model(
                        r.locator_json, FileLocator, FileLocator()
                    ),
                    metadata=_json_loads_model(
                        r.metadata_json, FileMetadata, FileMetadata()
                    ),
                    native_score=ranks.get(r.id) if fts_succeeded else None,
                )
                for r in rows
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
