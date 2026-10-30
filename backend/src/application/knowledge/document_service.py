"""知识库文档上传、版本和删除用例。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from pathlib import PurePosixPath

from domain.files import (
    Attachment,
    AttachmentStatus,
    DocumentJobPort,
    FileAdapterRegistryPort,
    FileMetadata,
    FileStoragePort,
)
from domain.knowledge import KnowledgeBaseRepository, KnowledgeDocumentRepository

from .errors import KnowledgeBaseNotFoundError, KnowledgeDocumentNotFoundError


class KnowledgeDocumentService:
    """协调知识库文档的上传、版本和异步处理生命周期。"""

    def __init__(
        self,
        repositories: KnowledgeBaseRepository,
        documents: KnowledgeDocumentRepository,
        storage: FileStoragePort,
        adapters: FileAdapterRegistryPort,
        jobs: DocumentJobPort,
        *,
        id_factory: Callable[[], str],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `repositories`（KnowledgeBaseRepository）：读取或写入知识库及其文档的仓储依赖；约束：无额外运行时限制。
        - `documents`（KnowledgeDocumentRepository）：读取或写入知识库文档的仓储依赖；约束：无额外运行时限制。
        - `storage`（FileStoragePort）：负责保存、读取或删除文件内容的存储依赖；约束：无额外运行时限制。
        - `adapters`（FileAdapterRegistryPort）：根据文件类型选择解析能力的适配器注册表；约束：无额外运行时限制。
        - `jobs`（DocumentJobPort）：负责提交、取消或恢复后台作业的依赖；约束：无额外运行时限制。
        - `id_factory`（Callable[[], str]）：生成稳定领域标识的可注入函数；约束：无额外运行时限制。
        - `clock`（Callable[[], datetime] | None）：生成当前时间的可注入函数，可用于控制时间来源；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._repositories = repositories
        self._documents = documents
        self._storage = storage
        self._adapters = adapters
        self._jobs = jobs
        self._id_factory = id_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def upload_document(
        self,
        knowledge_base_id: str,
        *,
        filename: str,
        mime_type: str,
        chunks: AsyncIterator[bytes],
    ) -> Attachment:
        """保存知识库文档并提交异步解析作业。

        参数：
        - `knowledge_base_id`（str）：目标知识库的稳定标识，用于限定文档操作范围；约束：须通过方法内校验；失败条件见下方异常说明。
        - `filename`（str）：用户提供的文件名；存储前会按实现规则规范化；约束：无额外运行时限制。
        - `mime_type`（str）：上传内容声明的媒体类型；约束：无额外运行时限制。
        - `chunks`（AsyncIterator[bytes]）：按顺序读取的文件字节块异步迭代器；约束：无额外运行时限制。

        返回：
        - Attachment：文件元数据、存储身份和处理状态组成的领域对象。

        异常：
        - KnowledgeBaseNotFoundError：校验条件 `await self._repositories.get(knowledge_base_id) is None` 成立时。
        - Exception（重新抛出捕获的异常）：校验条件 `捕获 Exception 后` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        if await self._repositories.get(knowledge_base_id) is None:
            raise KnowledgeBaseNotFoundError(knowledge_base_id)
        safe_name = self._normalize_filename(filename)
        adapter = self._adapters.select(safe_name, mime_type)
        stored = await self._storage.save_stream(chunks)
        previous = await self._documents.latest_for_filename(
            safe_name, knowledge_base_id=knowledge_base_id
        )
        attachment_id = self._id_factory()
        logical_document_id = previous.logical_document_id if previous else self._id_factory()
        now = self._clock()
        attachment = Attachment(
            id=attachment_id,
            logical_document_id=logical_document_id,
            document_version=(previous.document_version + 1 if previous else 1),
            session_id=None,
            knowledge_base_id=knowledge_base_id,
            message_id=None,
            filename=safe_name,
            mime_type=mime_type or "application/octet-stream",
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            storage_key=stored.storage_key,
            adapter_name=adapter.name,
            adapter_version=adapter.version,
            status=AttachmentStatus.UPLOADED,
            capabilities=adapter.capabilities,
            metadata=FileMetadata(),
            error_message=None,
            created_at=now,
            updated_at=now,
        )
        try:
            saved = await self._documents.create(attachment)
            await self._jobs.enqueue(saved.id)
            return saved
        except Exception:
            await self._jobs.cancel(attachment.id)
            await self._documents.soft_delete(
                attachment.id, knowledge_base_id=knowledge_base_id
            )
            await self._storage.discard(attachment.storage_key)
            raise

    async def list_documents(self, knowledge_base_id: str) -> list[Attachment]:
        """列出文档。

        参数：
        - `knowledge_base_id`（str）：目标知识库的稳定标识，用于限定文档操作范围；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - list[Attachment]：符合条件的有序结果集合；集合成员。

        异常：
        - KnowledgeBaseNotFoundError：校验条件 `await self._repositories.get(knowledge_base_id) is None` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        if await self._repositories.get(knowledge_base_id) is None:
            raise KnowledgeBaseNotFoundError(knowledge_base_id)
        return await self._documents.list_documents(knowledge_base_id)

    async def list_versions(
        self, knowledge_base_id: str, attachment_id: str
    ) -> list[Attachment]:
        """列出指定知识库文档的可用版本。

        参数：
        - `knowledge_base_id`（str）：目标知识库的稳定标识，用于限定文档操作范围；约束：须通过方法内校验；失败条件见下方异常说明。
        - `attachment_id`（str）：目标附件的稳定标识；约束：无额外运行时限制。

        返回：
        - list[Attachment]：符合条件的有序结果集合；集合成员。

        异常：
        - KnowledgeBaseNotFoundError：校验条件 `await self._repositories.get(knowledge_base_id) is None` 成立时。
        - KnowledgeDocumentNotFoundError：校验条件 `not values` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        if await self._repositories.get(knowledge_base_id) is None:
            raise KnowledgeBaseNotFoundError(knowledge_base_id)
        values = await self._documents.list_versions(
            attachment_id, knowledge_base_id=knowledge_base_id
        )
        if not values:
            raise KnowledgeDocumentNotFoundError(attachment_id)
        return values

    async def delete_document(self, knowledge_base_id: str, attachment_id: str) -> None:
        """删除文档。

        参数：
        - `knowledge_base_id`（str）：目标知识库的稳定标识，用于限定文档操作范围；约束：须通过方法内校验；失败条件见下方异常说明。
        - `attachment_id`（str）：目标附件的稳定标识；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - KnowledgeBaseNotFoundError：校验条件 `await self._repositories.get(knowledge_base_id) is None` 成立时。
        - KnowledgeDocumentNotFoundError：校验条件 `not await self._documents.soft_delete(attachment_id, knowledge_base_id=knowledge_base_id)` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        if await self._repositories.get(knowledge_base_id) is None:
            raise KnowledgeBaseNotFoundError(knowledge_base_id)
        await self._jobs.cancel(attachment_id)
        if not await self._documents.soft_delete(
            attachment_id, knowledge_base_id=knowledge_base_id
        ):
            raise KnowledgeDocumentNotFoundError(attachment_id)

    async def supported_extensions(self) -> list[str]:
        """返回知识库文档上传支持的文件扩展名。

        参数：
        - 无参数。

        返回：
        - list[str]：按字典序排列、可用于知识库文档上传的扩展名。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return self._adapters.supported_extensions()

    @staticmethod
    def _normalize_filename(filename: str) -> str:
        """清理并校验上传文档的文件名。

        参数：
        - `filename`（str）：用户提供的文件名；存储前会按实现规则规范化；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - str：去除路径信息并符合扩展名及长度限制的文档名。

        异常：
        - ValueError：校验条件 `not value or '\\x00' in value or value in {'.', '..'}` 成立时（异常说明：invalid filename）。
        - 未列出的依赖异常按当前实现传播。
        """
        value = PurePosixPath(filename.replace("\\", "/")).name
        if not value or "\x00" in value or value in {".", ".."}:
            raise ValueError("invalid filename")
        return value


__all__ = ["KnowledgeDocumentNotFoundError", "KnowledgeDocumentService"]
