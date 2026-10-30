"""知识库和文档写入用例。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from pathlib import PurePosixPath

from domain.files import (
    AdapterInfo,
    Attachment,
    AttachmentStatus,
    DocumentJobPort,
    FileAdapterRegistryPort,
    FileMetadata,
    FileStoragePort,
)
from domain.knowledge import KnowledgeBase, KnowledgeBaseRepository, KnowledgeDocumentRepository

from .document_service import KnowledgeDocumentService
from .errors import KnowledgeBaseNotFoundError, KnowledgeDocumentNotFoundError
from application.common.responses import AttachmentResponse, KnowledgeBaseResponse


class KnowledgeBaseService:
    """协调知识库元数据和文档上传生命周期。"""

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
        self._document_service = KnowledgeDocumentService(
            repositories,
            documents,
            storage,
            adapters,
            jobs,
            id_factory=id_factory,
            clock=self._clock,
        )

    async def list_knowledge_bases(self) -> list[KnowledgeBaseResponse]:
        """列出可访问的知识库并转换为响应对象。

        参数：
        - 无参数。

        返回：
        - list[KnowledgeBaseResponse]：符合条件的有序结果集合；集合成员。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return [_kb_response(value) for value in await self._repositories.list_knowledge_bases()]

    async def get(self, knowledge_base_id: str) -> KnowledgeBaseResponse:
        """读取指定知识库；不存在时抛出未找到异常。

        参数：
        - `knowledge_base_id`（str）：目标知识库的稳定标识，用于限定文档操作范围；约束：无额外运行时限制。

        返回：
        - KnowledgeBaseResponse：知识库标识、名称、说明和文档统计组成的响应。

        异常：
        - KnowledgeBaseNotFoundError：校验条件 `value is None` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        value = await self._repositories.get(knowledge_base_id)
        if value is None:
            raise KnowledgeBaseNotFoundError(knowledge_base_id)
        return _kb_response(value)

    async def create_knowledge_base(self, name: str, description: str = "") -> KnowledgeBaseResponse:
        """校验名称并创建知识库。

        参数：
        - `name`（str）：目标名称，用于标识相应配置、工具或对象；约束：须通过方法内校验；失败条件见下方异常说明。
        - `description`（str）：知识库的说明文本；约束：可省略，默认值为 `''`。

        返回：
        - KnowledgeBaseResponse：知识库标识、名称、说明和文档统计组成的响应。

        异常：
        - ValueError：校验条件 `not normalized` 成立时（异常说明：knowledge base name must not be empty）。
        - 未列出的依赖异常按当前实现传播。
        """
        normalized = name.strip()
        if not normalized:
            raise ValueError("knowledge base name must not be empty")
        now = self._clock()
        return _kb_response(await self._repositories.create(
            KnowledgeBase(
                id=self._id_factory(),
                name=normalized,
                description=description.strip(),
                document_count=0,
                ready_document_count=0,
                total_size_bytes=0,
                created_at=now,
                updated_at=now,
            )
        ))

    async def update_knowledge_base(self, knowledge_base_id: str, name: str, description: str = "") -> KnowledgeBase:
        """更新指定知识库的名称和说明。

        参数：
        - `knowledge_base_id`（str）：目标知识库的稳定标识，用于限定文档操作范围；约束：无额外运行时限制。
        - `name`（str）：目标名称，用于标识相应配置、工具或对象；约束：须通过方法内校验；失败条件见下方异常说明。
        - `description`（str）：知识库的说明文本；约束：可省略，默认值为 `''`。

        返回：
        - KnowledgeBase：知识库标识、名称和说明组成的领域对象。

        异常：
        - KnowledgeBaseNotFoundError：校验条件 `current is None` 成立时。
        - ValueError：校验条件 `not normalized` 成立时（异常说明：knowledge base name must not be empty）。
        - 未列出的依赖异常按当前实现传播。
        """
        current = await self._repositories.get(knowledge_base_id)
        if current is None:
            raise KnowledgeBaseNotFoundError(knowledge_base_id)
        normalized = name.strip()
        if not normalized:
            raise ValueError("knowledge base name must not be empty")
        updated = KnowledgeBase(
            id=current.id,
            name=normalized,
            description=description.strip(),
            document_count=current.document_count,
            ready_document_count=current.ready_document_count,
            total_size_bytes=current.total_size_bytes,
            created_at=current.created_at,
            updated_at=self._clock(),
        )
        return _kb_response(await self._repositories.save(updated))

    async def delete_knowledge_base(self, knowledge_base_id: str) -> None:
        """删除指定知识库及其关联文档。

        参数：
        - `knowledge_base_id`（str）：目标知识库的稳定标识，用于限定文档操作范围；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - KnowledgeBaseNotFoundError：校验条件 `not await self._repositories.delete(knowledge_base_id)` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        await self.get(knowledge_base_id)
        for document in await self._documents.list_documents(knowledge_base_id):
            await self._jobs.cancel(document.id)
            await self._documents.soft_delete(document.id, knowledge_base_id=knowledge_base_id)
        if not await self._repositories.delete(knowledge_base_id):
            raise KnowledgeBaseNotFoundError(knowledge_base_id)

    async def upload_document(
        self,
        knowledge_base_id: str,
        *,
        filename: str,
        mime_type: str,
        chunks: AsyncIterator[bytes],
    ) -> AttachmentResponse:
        """校验知识库及文件信息，保存文档并提交解析作业。

        参数：
        - `knowledge_base_id`（str）：目标知识库的稳定标识，用于限定文档操作范围；约束：无额外运行时限制。
        - `filename`（str）：用户提供的文件名；存储前会按实现规则规范化；约束：无额外运行时限制。
        - `mime_type`（str）：上传内容声明的媒体类型；约束：无额外运行时限制。
        - `chunks`（AsyncIterator[bytes]）：按顺序读取的文件字节块异步迭代器；约束：无额外运行时限制。

        返回：
        - AttachmentResponse：附件标识、文件元数据和处理状态组成的响应。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return _attachment_response(await self._document_service.upload_document(
            knowledge_base_id,
            filename=filename,
            mime_type=mime_type,
            chunks=chunks,
        ))

    async def list_documents(self, knowledge_base_id: str) -> list[AttachmentResponse]:
        """列出文档。

        参数：
        - `knowledge_base_id`（str）：目标知识库的稳定标识，用于限定文档操作范围；约束：无额外运行时限制。

        返回：
        - list[AttachmentResponse]：符合条件的有序结果集合；集合成员。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return [_attachment_response(value) for value in await self._document_service.list_documents(knowledge_base_id)]

    async def list_versions(self, knowledge_base_id: str, attachment_id: str) -> list[AttachmentResponse]:
        """列出指定知识库文档的可用版本。

        参数：
        - `knowledge_base_id`（str）：目标知识库的稳定标识，用于限定文档操作范围；约束：无额外运行时限制。
        - `attachment_id`（str）：目标附件的稳定标识；约束：无额外运行时限制。

        返回：
        - list[AttachmentResponse]：符合条件的有序结果集合；集合成员。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return [_attachment_response(value) for value in await self._document_service.list_versions(knowledge_base_id, attachment_id)]

    async def delete_document(self, knowledge_base_id: str, attachment_id: str) -> None:
        """删除文档。

        参数：
        - `knowledge_base_id`（str）：目标知识库的稳定标识，用于限定文档操作范围；约束：无额外运行时限制。
        - `attachment_id`（str）：目标附件的稳定标识；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        await self._document_service.delete_document(knowledge_base_id, attachment_id)

    async def supported_extensions(self) -> list[str]:
        """返回知识库文档上传支持的文件扩展名。

        参数：
        - 无参数。

        返回：
        - list[str]：按字典序排列、可用于知识库文档上传的扩展名。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return await self._document_service.supported_extensions()

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


def _kb_response(value: KnowledgeBase) -> KnowledgeBaseResponse:
    """将知识库实体转换为只读应用响应。

    参数：
    - `value`（KnowledgeBase）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - KnowledgeBaseResponse：知识库标识、名称、说明和文档统计组成的响应。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return KnowledgeBaseResponse(
        id=value.id,
        name=value.name,
        description=value.description,
        document_count=value.document_count,
        ready_document_count=value.ready_document_count,
        total_size_bytes=value.total_size_bytes,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def _attachment_response(value: Attachment) -> AttachmentResponse:
    """将知识库附件实体转换为只读应用响应。

    参数：
    - `value`（Attachment）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - AttachmentResponse：附件标识、文件元数据和处理状态组成的响应。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return AttachmentResponse(
        id=value.id,
        filename=value.filename,
        mime_type=value.mime_type,
        size_bytes=value.size_bytes,
        sha256=value.sha256,
        storage_key=value.storage_key,
        status=value.status.value,
        session_id=value.session_id,
        knowledge_base_id=value.knowledge_base_id,
        message_id=value.message_id,
        adapter_name=value.adapter_name,
        adapter_version=value.adapter_version,
        capabilities=tuple(value.capabilities),
        metadata=dict(value.metadata.values),
        error_message=value.error_message,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )
