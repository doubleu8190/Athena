"""附件上传、查询和删除用例。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from pathlib import PurePosixPath

from domain.files import (
    Attachment,
    AttachmentRepository,
    AttachmentStatus,
    DocumentJobPort,
    FileAdapterRegistryPort,
    FileMetadata,
    FileStoragePort,
)
from domain.sessions import SessionRepository

from application.common.responses import AttachmentResponse


class AttachmentNotFoundError(LookupError):
    """附件不存在或不属于目标会话。"""


class SessionNotFoundError(LookupError):
    """目标会话不存在。"""


class AttachmentService:
    """协调会话附件的校验、存储、持久化和异步处理入队。"""

    def __init__(
        self,
        sessions: SessionRepository,
        attachments: AttachmentRepository,
        storage: FileStoragePort,
        adapters: FileAdapterRegistryPort,
        jobs: DocumentJobPort,
        *,
        id_factory: Callable[[], str],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `sessions`（SessionRepository）：读取会话身份和状态的仓储依赖；约束：无额外运行时限制。
        - `attachments`（AttachmentRepository）：当前请求关联或已校验的附件集合；约束：无额外运行时限制。
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
        self._sessions = sessions
        self._attachments = attachments
        self._storage = storage
        self._adapters = adapters
        self._jobs = jobs
        self._id_factory = id_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def list_for_session(self, session_id: str) -> list[AttachmentResponse]:
        """列出指定会话可访问的附件。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。

        返回：
        - list[AttachmentResponse]：符合条件的有序结果集合；集合成员。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        await self._require_session(session_id)
        return [_to_response(value) for value in await self._attachments.list_by_session(session_id)]

    async def get(self, session_id: str, attachment_id: str) -> AttachmentResponse:
        """读取指定会话中的附件并转换为响应。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。
        - `attachment_id`（str）：目标附件的稳定标识；约束：无额外运行时限制。

        返回：
        - AttachmentResponse：附件标识、文件元数据和处理状态组成的响应。

        异常：
        - AttachmentNotFoundError：校验条件 `value is None` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        await self._require_session(session_id)
        value = await self._attachments.get(attachment_id, session_id=session_id)
        if value is None:
            raise AttachmentNotFoundError(attachment_id)
        return _to_response(value)

    async def supported_extensions(self, session_id: str) -> list[str]:
        """返回当前会话允许上传的文件扩展名。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。

        返回：
        - list[str]：按字典序排列、可用于当前会话上传的文件扩展名。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        await self._require_session(session_id)
        return self._adapters.supported_extensions()

    async def upload(
        self,
        session_id: str,
        *,
        filename: str,
        mime_type: str,
        chunks: AsyncIterator[bytes],
        message_id: str | None = None,
    ) -> AttachmentResponse:
        """校验会话和文件信息，保存附件并提交后续处理作业。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。
        - `filename`（str）：用户提供的文件名；存储前会按实现规则规范化；约束：无额外运行时限制。
        - `mime_type`（str）：上传内容声明的媒体类型；约束：无额外运行时限制。
        - `chunks`（AsyncIterator[bytes]）：按顺序读取的文件字节块异步迭代器；约束：无额外运行时限制。
        - `message_id`（str | None）：关联消息的稳定标识；None 表示未绑定消息；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - AttachmentResponse：附件标识、文件元数据和处理状态组成的响应。

        异常：
        - Exception（重新抛出捕获的异常）：校验条件 `捕获 Exception 后` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        await self._require_session(session_id)
        safe_name = self._normalize_filename(filename)
        adapter = self._adapters.select(safe_name, mime_type)
        stored = await self._storage.save_stream(chunks)
        now = self._clock()
        attachment = Attachment(
            id=self._id_factory(),
            logical_document_id=self._id_factory(),
            document_version=1,
            session_id=session_id,
            knowledge_base_id=None,
            message_id=message_id,
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
            saved = await self._attachments.create(attachment)
            await self._jobs.enqueue(saved.id)
            return _to_response(saved)
        except Exception:
            await self._compensate(attachment)
            raise

    async def delete(self, session_id: str, attachment_id: str) -> None:
        """删除会话附件及其存储内容，并清理关联状态。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：须通过方法内校验；失败条件见下方异常说明。
        - `attachment_id`（str）：目标附件的稳定标识；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - AttachmentNotFoundError：校验条件 `not await self._attachments.soft_delete(attachment_id, session_id=session_id)` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        await self.get(session_id, attachment_id)
        await self._jobs.cancel(attachment_id)
        if not await self._attachments.soft_delete(attachment_id, session_id=session_id):
            raise AttachmentNotFoundError(attachment_id)

    async def _require_session(self, session_id: str) -> None:
        """确认会话存在，否则拒绝附件操作。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - SessionNotFoundError：校验条件 `await self._sessions.get(session_id) is None` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        if await self._sessions.get(session_id) is None:
            raise SessionNotFoundError(session_id)

    @staticmethod
    def _normalize_filename(filename: str) -> str:
        """清理文件名并限制其长度和可用字符。

        参数：
        - `filename`（str）：用户提供的文件名；存储前会按实现规则规范化；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - str：去除路径信息并符合扩展名及长度限制的文件名。

        异常：
        - ValueError：校验条件 `not normalized or '\\x00' in normalized or normalized in {'.', '..'}` 成立时（异常说明：invalid filename）。
        - 未列出的依赖异常按当前实现传播。
        """
        normalized = PurePosixPath(filename.replace("\\", "/")).name
        if not normalized or "\x00" in normalized or normalized in {".", ".."}:
            raise ValueError("invalid filename")
        return normalized

    async def _compensate(self, attachment: Attachment) -> None:
        """上传后续步骤失败时清理附件记录和文件。

        参数：
        - `attachment`（Attachment）：当前待处理的附件领域对象；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        try:
            await self._jobs.cancel(attachment.id)
        finally:
            try:
                await self._attachments.soft_delete(
                    attachment.id, session_id=attachment.session_id or ""
                )
            finally:
                await self._storage.discard(attachment.storage_key)


def _to_response(value: Attachment) -> AttachmentResponse:
    """将附件领域实体转换为 HTTP/SSE 可消费的应用响应。

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
        status=value.status,
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
