"""文档解析、分块和索引业务用例。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4
import asyncio

from domain.files import (
    Attachment,
    AttachmentRepository,
    AttachmentStatus,
    DocumentParserPort,
    FileChunk,
    FileChunkRepository,
    GraphIndexerPort,
    ParsedDocument,
    VectorIndexerPort,
)
from domain.leases import LeaseCenterPort, LeaseGrant, LeaseKey


class IngestionService:
    """将已上传附件推进到解析、分块、向量和图索引状态。"""

    def __init__(
        self,
        attachments: AttachmentRepository,
        chunks: FileChunkRepository,
        parser: DocumentParserPort,
        vector_indexer: VectorIndexerPort,
        graph_indexer: GraphIndexerPort | None = None,
        *,
        chunker: Callable[[str], list[FileChunk]],
        clock: Callable[[], datetime] | None = None,
        lease_center: LeaseCenterPort | None = None,
    ) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `attachments`（AttachmentRepository）：当前请求关联或已校验的附件集合；约束：无额外运行时限制。
        - `chunks`（FileChunkRepository）：按顺序读取的文件字节块异步迭代器；约束：无额外运行时限制。
        - `parser`（DocumentParserPort）：解析上传文件内容的适配器；约束：无额外运行时限制。
        - `vector_indexer`（VectorIndexerPort）：执行向量检索或索引操作的可选依赖；约束：无额外运行时限制。
        - `graph_indexer`（GraphIndexerPort | None）：写入或删除知识图谱索引的依赖；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `chunker`（Callable[[str], list[FileChunk]]）：将解析文本拆分为检索文本块的依赖；约束：无额外运行时限制。
        - `clock`（Callable[[], datetime] | None）：生成当前时间的可注入函数，可用于控制时间来源；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `lease_center`（LeaseCenterPort | None）：协调后台作业租约的可选应用端口；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._attachments = attachments
        self._chunks = chunks
        self._parser = parser
        self._vector_indexer = vector_indexer
        self._graph_indexer = graph_indexer
        self._chunker = chunker
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._leases = lease_center or getattr(chunks, "_leases")

    async def process_attachment(
        self, attachment_id: str, *, lease: LeaseGrant
    ) -> dict[str, object]:
        """解析附件、创建文本块并更新向量及图谱索引。

        参数：
        - `attachment_id`（str）：目标附件的稳定标识；约束：须通过方法内校验；失败条件见下方异常说明。
        - `lease`（LeaseGrant）：当前后台作业持有的租约授权及执行代次；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - dict[str, object]：附件处理状态及已创建文本块、索引和错误信息的结果映射。

        异常：
        - ValueError：校验条件 `lease.key != LeaseKey('document_ingestion', attachment_id)` 成立时（异常说明：document_ingestion_lease_mismatch）。
        - LookupError：校验条件 `attachment is None` 成立时。
        - LookupError：校验条件 `attachment.status is AttachmentStatus.DELETED` 成立时（异常说明：attachment_deleted）。
        - 未列出的依赖异常按当前实现传播。
        """
        if lease.key != LeaseKey("document_ingestion", attachment_id):
            raise ValueError("document_ingestion_lease_mismatch")
        await self._leases.assert_current(lease)
        attachment = await self._attachments.get(attachment_id)
        if attachment is None:
            raise LookupError(attachment_id)
        if attachment.status is AttachmentStatus.DELETED:
            raise LookupError("attachment_deleted")
        parsed = await self._parser.parse(attachment)
        revision_id = str(uuid4())
        parsed_chunks = await asyncio.to_thread(self._chunker, parsed.text)
        from dataclasses import replace

        values = [
            replace(
                value,
                id=str(uuid4()),
                attachment_id=attachment_id,
                index_revision_id=revision_id,
            )
            for value in parsed_chunks
        ]
        await self._chunks.replace_for_attachment(attachment_id, values, lease=lease)
        await self._vector_indexer.index_attachment(attachment, values, lease=lease)
        if self._graph_indexer is not None:
            await self._leases.assert_current(lease)
            await self._graph_indexer.index_attachment(attachment, values, lease=lease)
        return {
            "attachment_id": attachment_id,
            "chunks": len(values),
            "status": AttachmentStatus.READY.value,
            "index_revision_id": revision_id,
            "_parsed_metadata": parsed.metadata.values,
            "_adapter_name": parsed.adapter_name or attachment.adapter_name,
            "_adapter_version": parsed.adapter_version or attachment.adapter_version,
            "_capabilities": list(parsed.capabilities or attachment.capabilities),
        }

    async def delete_indexes(self, attachment_id: str) -> None:
        """删除附件对应的向量索引和图谱索引。

        参数：
        - `attachment_id`（str）：目标附件的稳定标识；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        await self._vector_indexer.delete_attachment(attachment_id)
        if self._graph_indexer is not None:
            await self._graph_indexer.delete_attachment(attachment_id)
