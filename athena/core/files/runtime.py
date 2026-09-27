"""File Intelligence 运行时 — 文件解析、索引、搜索和分析的核心编排层。

``FileIntelligenceRuntime`` 是文件智能系统的中枢，协调以下能力：

- **解析**：通过适配器提取文件内容、符号和表格，分块存储到 PostgreSQL。
- **索引**：将分块写入 ChromaDB 向量索引，支持语义搜索。
- **读取**：按定位器（页码、工作表、路径）精准读取文件分块。
- **搜索**：融合 PostgreSQL FTS 关键词搜索和向量语义搜索的混合检索。
- **摘要**：多级 LLM 摘要（分块 → 章节 → 文档），支持缓存。
- **分析**：适配器级分析 + 视觉模型图片描述。

生命周期：
    ``initialize()`` 初始化适配器注册表和向量索引；
    各 ``*_file()`` 方法由工具层调用，通过 ``require_attachment()``
    校验权限后执行。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Protocol

from langchain_core.messages import HumanMessage

from athena.config.settings import Settings
from athena.core.retrieval import RetrievalCandidate, RetrievalRunRequest
from athena.core.retrieval.ports import RetrievalTraceWriter
from athena.core.files.extraction import ExtractedUnit, ExtractionContext
from athena.core.files.ports import FileVectorStore
from athena.core.files.attachment_serialization import attachment_to_payload
from athena.core.files.adapter_registry import AdapterRegistry
from athena.infrastructure.postgre.repositories.file_repository import FileRepository
from athena.core.files.storage import StorageLayer
from athena.core.llm.provider import LLMProvider
from athena.core.llm.tokens import EmbeddingTokenCounter, conservative_text_token_count
from athena.contracts.events import ApplicationEvent, EventDurability, EventType
from athena.models.file import (
    Attachment,
    AttachmentStatus,
    FileChunk,
)
from athena.models.json_models import FileLocator, FileMetadata
from athena.utils.id_generation import generate_time_id
from athena.utils.llm_response import extract_message_text
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class FileAccessError(PermissionError):
    """文件访问权限错误（文件不存在或当前会话没有直接或知识库权限）。"""


class FileEventPublisher(Protocol):
    """文件运行时所需的事件发布端口。"""

    async def publish(self, event: ApplicationEvent) -> ApplicationEvent:
        """发布文件生命周期事件。

        参数:
            event (ApplicationEvent): 要发布的文件事件。
        返回值:
            ApplicationEvent: 事件发布器确认后的事件对象。
        异常:
            事件传输失败时传播底层异常。
        """
        ...


class FileIntelligenceRuntime:
    """File Intelligence 运行时，协调文件的解析、索引、搜索和分析。

    通过依赖注入获取 Repository、LLM 和事件发布器，
    内部管理 StorageLayer、AdapterRegistry 和 ChromaDB 向量索引。

    参数：
        repository: 文件持久化仓库。
        primary_llm: 主 LLM 提供者（用于文档摘要和视觉分析）。
        secondary_llm: 次要 LLM 提供者（用于分块摘要，成本更低）。
        settings: 全局配置。
        event_publisher: 应用事件发布器。
    """

    def __init__(
        self,
        repository: FileRepository,
        primary_llm: LLMProvider,
        secondary_llm: LLMProvider,
        *,
        settings: Settings,
        event_publisher: FileEventPublisher,
        trace_writer: RetrievalTraceWriter,
        vector_store: FileVectorStore,
        file_token_counter: EmbeddingTokenCounter | None = None,
    ) -> None:
        """

        参数：
            repository (FileRepository): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            primary_llm (LLMProvider): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            secondary_llm (LLMProvider): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            settings (Settings): 全局配置对象。
            event_publisher: 应用事件发布器。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        self.repository = repository
        self.settings = settings
        self.storage = StorageLayer(
            self.settings.files_path, self.settings.file_max_upload_bytes
        )
        self.adapter_registry = AdapterRegistry()
        self.primary_llm = primary_llm
        self.secondary_llm = secondary_llm
        self._event_publisher = event_publisher
        self._trace_writer = trace_writer
        self._vector_store = vector_store
        self.file_token_counter = file_token_counter or EmbeddingTokenCounter(
            conservative_text_token_count
        )
        self._knowledge_document_locks: dict[str, asyncio.Lock] = {}

    async def _complete_retrieval_trace(
        self,
        run_id: str | None,
        started: float,
        *,
        candidate_count: int,
        selected_count: int = 0,
        status: str,
        error_message: str | None = None,
    ) -> None:
        """安全写入文件检索终态，避免轨迹故障影响搜索结果。

        参数：
            run_id: 召回运行标识；为空时跳过写入。
            started: 运行计时起点。
            candidate_count: 融合候选数量。
            selected_count: 通过 provider 筛选的候选数量。
            status: 运行终态。
            error_message: 可选的错误说明。

        返回值：
            None。

        异常：
            轨迹写入异常只记录日志，不覆盖搜索本身的结果或错误。
        """
        if run_id is None or self._trace_writer is None:
            return
        try:
            await self._trace_writer.complete_run(
                run_id,
                candidate_count=candidate_count,
                selected_count=selected_count,
                status=status,
                duration_ms=round(
                    (asyncio.get_running_loop().time() - started) * 1000, 2
                ),
                error_message=error_message,
            )
        except Exception as exc:
            logger.warning("retrieval_trace_complete_failed", error=str(exc))

    async def initialize(self) -> None:
        """初始化运行时：同步适配器注册表和文件向量索引。"""
        await self.repository.sync_adapters(
            adapter.info for adapter in self.adapter_registry.list()
        )

    async def cleanup_unreferenced_blobs(self) -> int:
        """回收无引用的内容寻址 blob（附件软删除后调用）。

        返回值：
            删除的 blob 数量。
        """
        live = await self.repository.list_live_storage_keys()
        removed = 0
        for key in self.storage.blob_keys():
            if key in live:
                continue
            if self.storage.delete_blob(key):
                removed += 1
        return removed

    async def require_attachment(self, session_id: str, file_id: str) -> Attachment:
        """获取附件并校验会话直接所有权或知识库授权。

        参数：
            session_id: 会话 ID。
            file_id: 附件 ID。

        返回值：
            ``Attachment`` 实例。

        异常：
            FileAccessError: 附件不存在或当前会话没有访问权限。
        """
        attachment = await self.repository.get_accessible_attachment(
            session_id, file_id
        )
        if attachment is None:
            raise FileAccessError("文件不存在或当前会话没有访问权限")
        return attachment

    async def parse_attachment(
        self, attachment_id: str, *, run_id: str | None = None
    ) -> dict[str, Any]:
        """解析附件：提取内容、分块、构建符号索引和表格缓存。"""
        attachment = await self.repository.get_attachment(attachment_id)
        if attachment is None:
            raise FileNotFoundError("附件不存在")
        adapter = self.adapter_registry.select(
            attachment.filename, attachment.mime_type
        )
        processing = await self.repository.update_attachment(
            attachment.id,
            status=AttachmentStatus.PROCESSING.value,
            adapter_name=adapter.info.name,
            adapter_version=adapter.info.version,
            capabilities=adapter.info.capabilities,
            error_message=None,
        )
        if processing is None:
            raise RuntimeError("无法更新附件状态")
        logger.info(
            "file_processing_started", attachment_id=attachment.id, run_id=run_id
        )
        await self.emit_attachment(processing, run_id=run_id)
        path = self.storage.resolve(attachment.storage_key)
        workspace = self.storage.create_workspace(attachment.id)
        try:
            context = ExtractionContext(
                path=path,
                workspace=workspace,
                filename=attachment.filename,
                mime_type=attachment.mime_type,
            )
            result = await adapter.extract(context, self.settings)
            # 适配器接收内容寻址的 blob 路径。持久化前，将这一实现细节替换为
            # 用户可见的文件名，并应用到所有定位器和代码索引中。
            for unit in result.units:
                if unit.locator.get("path") == path.name:
                    unit.locator["path"] = attachment.filename
            for symbol in result.symbols:
                if symbol.get("path") == path.name:
                    symbol["path"] = attachment.filename
            for dependency in result.dependencies:
                if dependency.get("source") == path.name:
                    dependency["source"] = attachment.filename
            chunks = self._chunk_units(attachment.id, result.units)
            await self.repository.replace_chunks(attachment.id, chunks)
            await self.repository.replace_code_index(
                attachment.id, result.symbols, result.dependencies
            )
            if result.tables:
                cache_key = self.cache_key(
                    attachment,
                    "tables",
                    {},
                    adapter.info.version,
                    self.settings.primary_llm.model,
                )
                await self.repository.put_artifact(
                    attachment.id,
                    "tables",
                    cache_key,
                    json.dumps(result.tables, ensure_ascii=False, default=str),
                    {"count": len(result.tables)},
                )
            metadata = {
                **result.metadata,
                "chunk_count": len(chunks),
                "symbol_count": len(result.symbols),
                "dependency_count": len(result.dependencies),
                "document_version": attachment.document_version,
            }
            await self.repository.update_attachment(attachment.id, metadata=metadata)
            return metadata
        finally:
            self.storage.cleanup_workspace(workspace)

    async def index_attachment(self, attachment_id: str) -> dict[str, Any]:
        """将附件分块写入 ChromaDB 向量索引。

        按 100 个分块一批写入；失败时抛出异常，由调用方统一更新附件状态。

        参数：
            attachment_id: 附件 ID。
        返回值：
            包含 chunks 数量和 vector_indexed 标志的字典。
        """
        attachment = await self.repository.get_attachment(attachment_id)
        if attachment is None:
            raise FileNotFoundError("附件不存在")
        chunks = await self.repository.get_chunks(attachment_id, limit=100_000)
        try:
            await self._vector_store.replace_attachment(
                attachment_id,
                [
                    {
                        "id": chunk.id,
                        "content": chunk.content,
                        "metadata": {
                            "attachment_id": attachment_id,
                            "document_version": attachment.document_version,
                            "ordinal": chunk.ordinal,
                            "locator_json": json.dumps(
                                chunk.locator.model_dump(
                                    mode="json", exclude_none=True
                                ),
                                ensure_ascii=False,
                            ),
                        },
                    }
                    for chunk in chunks
                ],
            )
        except Exception as exc:
            logger.warning(
                "file_embedding_index_failed",
                attachment_id=attachment_id,
                error=str(exc),
            )
            raise
        return {"chunks": len(chunks)}

    async def process_knowledge_document(self, attachment_id: str) -> dict[str, Any]:
        """幂等完成知识库文档解析和向量索引。

        参数：
            attachment_id：知识库文档附件 ID。

        返回：
            解析和索引阶段的统计信息。

        异常：
            解析、PostgreSQL 写入或向量索引失败时向上抛出，由任务 worker 负责重试。
        """
        async with self._knowledge_lock(attachment_id):
            await self.initialize()
            attachment = await self.repository.get_attachment(attachment_id)
            if attachment is None or attachment.knowledge_base_id is None:
                raise FileNotFoundError("知识库文档不存在")
            metadata = await self.parse_attachment(attachment_id)
            # 删除请求可能在解析耗时阶段完成。再次读取附件能阻止后续向量
            # 写入，并由删除路径清理本轮已经生成的 PostgreSQL 派生数据。
            attachment = await self.repository.get_attachment(attachment_id)
            if attachment is None or attachment.knowledge_base_id is None:
                raise FileNotFoundError("知识库文档已删除")
            indexed = await self.index_attachment(attachment_id)
            updated = await self.repository.update_attachment(
                attachment_id,
                status=AttachmentStatus.READY.value,
                error_message=None,
            )
            if updated is None:
                await self.delete_attachment_vectors(attachment_id)
                raise FileNotFoundError("知识库文档已删除")
            return {**metadata, **indexed}

    async def delete_knowledge_document(
        self, attachment_id: str, knowledge_base_id: str
    ) -> bool:
        """删除知识库文档的向量索引和 PostgreSQL 派生数据。

        参数：
            attachment_id：要删除的知识库文档附件 ID。
            knowledge_base_id：目标文档所属的知识库 ID。

        返回：
            成功删除时返回 ``True``；文档不存在或不属于该知识库时返回 ``False``。

        异常：
            Chroma 删除失败时不会继续删除 PostgreSQL 数据，避免留下无法重建的状态。
        """
        async with self._knowledge_lock(attachment_id):
            attachment = await self.repository.get_attachment(attachment_id)
            if attachment is None or attachment.knowledge_base_id != knowledge_base_id:
                return False
            await self.initialize()
            try:
                await self.delete_attachment_vectors(attachment_id)
                deleted = await self.repository.soft_delete_knowledge_base_attachment(
                    attachment_id, knowledge_base_id
                )
            except Exception:
                # Chroma 先于 PostgreSQL 清理；PostgreSQL 失败时以仍在库中的分块重建向量。
                try:
                    current = await self.repository.get_attachment(
                        attachment_id, include_deleted=True
                    )
                    if current is not None and current.deleted_time is None:
                        await self.index_attachment(attachment_id)
                except Exception:
                    logger.exception(
                        "knowledge_delete_vector_compensation_failed",
                        attachment_id=attachment_id,
                    )
                raise
            if not deleted:
                current = await self.repository.get_attachment(
                    attachment_id, include_deleted=True
                )
                if current is not None and current.deleted_time is None:
                    await self.index_attachment(attachment_id)
            return deleted

    async def delete_attachment_vectors(self, attachment_id: str) -> None:
        """删除某附件在 Chroma 中的全部向量。

        参数：
            attachment_id：附件唯一标识。

        返回：
            None。

        异常：
            Chroma 删除失败时向上抛出异常。
        """
        await self._vector_store.delete_attachment(attachment_id)

    def _knowledge_lock(self, attachment_id: str) -> asyncio.Lock:
        """返回同一知识库文档共享的异步互斥锁。

        参数：
            attachment_id：附件唯一标识。

        返回：
            当前进程内用于串行化解析和删除的锁。

        异常：
            不主动抛出业务异常。
        """
        return self._knowledge_document_locks.setdefault(attachment_id, asyncio.Lock())

    def _chunk_units(
        self, attachment_id: str, units: list[ExtractedUnit]
    ) -> list[FileChunk]:
        """将内容单元分块，支持换行符边界和重叠窗口。

        分块策略：
        - 使用次要 LLM 对应的 token 计数器限制每个块的 token 数。
        - 优先在换行符处断开（避免拆断行）。
        - 相邻块按 token 数保留重叠窗口，确保跨块搜索不丢失上下文。

        参数：
            attachment_id: 附件 ID，写入每个分块的元数据。
            units: 提取的内容单元列表。

        返回值：
            分块后的 ``FileChunk`` 列表，按序号排列。
        """
        max_tokens = max(1, self.settings.file_chunk_tokens)
        overlap_tokens = min(max_tokens // 3, self.settings.file_chunk_overlap_tokens)
        chunks: list[FileChunk] = []
        ordinal = 0
        for group in self._group_units_for_chunking(units):
            # 二进制文档中的 NUL 不是可见文本，且 PostgreSQL TEXT 不允许保存；
            # 在分块前清理可使 token 计数和字符定位都对应最终落库内容。
            content = (
                "\n\n".join(unit.content for unit in group).replace("\x00", "").strip()
            )
            if not content:
                continue
            start = 0
            while start < len(content):
                end = self._find_chunk_end(content, start, max_tokens)
                end = self._prefer_semantic_boundary(content, start, end)
                piece = content[start:end].strip()
                if piece:
                    token_count = self.file_token_counter.count_text_tokens(piece)
                    chunks.append(
                        FileChunk(
                            id=generate_time_id(),
                            attachment_id=attachment_id,
                            ordinal=ordinal,
                            content=piece,
                            token_count=token_count,
                            locator=FileLocator.model_validate(
                                self._chunk_locator(group, start, end)
                            ),
                            metadata=FileMetadata.model_validate(
                                self._group_metadata(group)
                            ),
                        )
                    )
                    ordinal += 1
                if end >= len(content):
                    break
                overlap_start = self._find_overlap_start(
                    content, start, end, overlap_tokens
                )
                start = max(start + 1, overlap_start)
        return chunks

    @staticmethod
    def _group_units_for_chunking(
        units: list[ExtractedUnit],
    ) -> list[list[ExtractedUnit]]:
        groups: list[list[ExtractedUnit]] = []
        current: list[ExtractedUnit] = []
        for unit in units:
            is_pdf_page = unit.kind == "page" or unit.metadata.get("kind") == "page"
            if current and (
                not is_pdf_page
                or not current[-1].can_merge_after
                or not unit.can_merge_before
            ):
                groups.append(current)
                current = []
            if not is_pdf_page:
                groups.append([unit])
            else:
                current.append(unit)
        if current:
            groups.append(current)
        return groups

    @staticmethod
    def _group_metadata(group: list[ExtractedUnit]) -> dict[str, Any]:
        if len(group) == 1:
            return dict(group[0].metadata)
        return {
            "kind": "pdf_document",
            "ocr": any(bool(unit.metadata.get("ocr")) for unit in group),
            "chunk_strategy": "pdf-semantic-v1",
        }

    @staticmethod
    def _chunk_locator(
        group: list[ExtractedUnit], start: int, end: int
    ) -> dict[str, Any]:
        if len(group) == 1:
            locator = dict(group[0].locator)
            page = locator.get("page")
            if page is not None:
                locator.setdefault("page_start", page)
                locator.setdefault("page_end", page)
            locator.update(char_start=start, char_end=end)
            return locator
        cursor = 0
        pages: list[int] = []
        for unit in group:
            unit_start = cursor
            cursor += len(unit.content) + 2
            unit_end = cursor
            if end > unit_start and start < unit_end:
                page = unit.locator.get("page")
                if isinstance(page, int):
                    pages.append(page)
        locator = {
            "page_start": min(pages) if pages else None,
            "page_end": max(pages) if pages else None,
            "char_start": start,
            "char_end": end,
        }
        return {key: value for key, value in locator.items() if value is not None}

    @staticmethod
    def _prefer_semantic_boundary(content: str, start: int, end: int) -> int:
        if end >= len(content):
            return end
        midpoint = start + max(1, (end - start) // 2)
        newline = content.rfind("\n", midpoint, end)
        if newline > start:
            return newline
        matches = list(re.finditer(r"[。！？；!?;]\s*", content[midpoint:end]))
        return midpoint + matches[-1].end() if matches else end

    def _find_chunk_end(self, content: str, start: int, max_tokens: int) -> int:
        """查找适合 token 预算的最长字符切片。"""
        if self.file_token_counter.count_text_tokens(content[start:]) <= max_tokens:
            return len(content)
        low = start + 1
        high = len(content)
        best = low
        while low <= high:
            middle = (low + high) // 2
            if (
                self.file_token_counter.count_text_tokens(content[start:middle])
                <= max_tokens
            ):
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        return best

    def _find_overlap_start(
        self, content: str, chunk_start: int, chunk_end: int, overlap_tokens: int
    ) -> int:
        """查找适合重叠 token 预算的最早后缀。"""
        if overlap_tokens <= 0:
            return chunk_end
        low = chunk_start
        high = chunk_end
        best = chunk_end
        while low <= high:
            middle = (low + high) // 2
            if (
                self.file_token_counter.count_text_tokens(content[middle:chunk_end])
                <= overlap_tokens
            ):
                best = middle
                high = middle - 1
            else:
                low = middle + 1
        return best

    async def list_files(self, session_id: str) -> list[dict[str, Any]]:
        """列出会话附件和全局知识库文档（公开字段）。"""
        session_files = await self.repository.list_session_attachments(session_id)
        knowledge_documents = (
            await self.repository.list_global_knowledge_ready_documents()
        )
        return [
            attachment_to_payload(item)
            for item in [*session_files, *knowledge_documents]
        ]

    async def list_session_files(self, session_id: str) -> list[dict[str, Any]]:
        """仅列出直接上传到会话的附件，供会话附件管理界面使用。

        参数：
            session_id (str): 会话唯一标识。

        返回值：
            list[dict[str, Any]]: 不包含知识库文档的公开附件列表。

        异常：
            数据库读取失败时传播底层异常。
        """
        return [
            attachment_to_payload(item)
            for item in await self.repository.list_session_attachments(session_id)
        ]

    async def get_file_info(self, session_id: str, file_id: str) -> dict[str, Any]:
        """获取附件详情（含元数据）。"""
        return attachment_to_payload(
            await self.require_attachment(session_id, file_id), include_metadata=True
        )

    async def read_file(
        self,
        session_id: str,
        file_id: str,
        locator: dict[str, Any] | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """读取文件内容分块。

        支持按定位器（page/sheet/path）过滤，返回匹配的分块列表。
        附件未就绪时返回 waiting 状态。

        参数：
            session_id: 会话 ID。
            file_id: 附件 ID。
            locator: 可选的定位器过滤条件（如 ``{"page": 3}``）。
            limit: 返回的最大分块数（1-50）。

        返回值：
            包含 file/chunks/waiting 等字段的结果字典。
        """
        attachment = await self.require_attachment(session_id, file_id)
        if attachment.status == AttachmentStatus.FAILED:
            return {
                "file": attachment_to_payload(attachment),
                "waiting": False,
                "error": attachment.error_message or "文件处理失败",
                "message": "文件处理失败，无法读取内容。请重新上传或重试处理。",
            }
        if attachment.status != AttachmentStatus.READY:
            await self.emit(
                EventType.AGENT_WAITING_FILE,
                session_id,
                {
                    "message_id": attachment.message_id,
                    "file_id": file_id,
                    "status": attachment.status.value,
                },
            )
            return {
                "file": attachment_to_payload(attachment),
                "waiting": True,
                "message": "文件仍在处理中，请稍后再次读取。",
            }
        limit = min(max(limit, 1), 50)
        chunks = await self.repository.get_chunks(
            file_id, limit=100_000 if locator else limit
        )
        if not chunks and attachment.adapter_name == "image":
            return {
                "file": attachment_to_payload(attachment),
                "chunks": [],
                "message": "图片未识别出可读取的 OCR 文本；如需描述图片画面，请使用 analyze_file，并确保视觉模型能力已启用。",
            }
        locator = locator or {}
        if "page" in locator:
            requested_page = int(locator["page"])
            page_chunks: list[FileChunk] = []
            for chunk in chunks:
                start_page = chunk.locator.page_start or chunk.locator.page
                end_page = chunk.locator.page_end or chunk.locator.page
                if (
                    start_page is not None
                    and end_page is not None
                    and start_page <= requested_page <= end_page
                ):
                    page_chunks.append(chunk)
            chunks = page_chunks
        for key in ("sheet", "path"):
            if key in locator:
                chunks = [
                    chunk
                    for chunk in chunks
                    if getattr(chunk.locator, key, None) == locator[key]
                ]
        chunks = chunks[:limit]
        return {
            "file": attachment_to_payload(attachment),
            "chunks": [
                {
                    "content": c.content,
                    "locator": c.locator.model_dump(mode="json", exclude_none=True),
                    "metadata": c.metadata.model_dump(mode="json", exclude_none=True),
                }
                for c in chunks
            ],
        }

    async def search_file(
        self,
        session_id: str,
        file_id: str,
        query: str,
        limit: int = 10,
        *,
        agent_run_id: str | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        """混合搜索文件内容（PostgreSQL FTS 关键词 + ChromaDB 向量语义）。

        使用 RRF (Reciprocal Rank Fusion) 融合两种搜索结果，
        公式：score = Σ 1/(60 + rank)。

        参数：
            session_id: 会话 ID。
            file_id: 附件 ID。
            query: 搜索查询文本。
            limit: 返回的最大结果数（1-50）。

        返回值：
            包含 query/results/score 的搜索结果字典。
        """
        attachment = await self.require_attachment(session_id, file_id)
        started = asyncio.get_running_loop().time()
        run_id: str
        try:
            run_id = await self._trace_writer.start_run(
                RetrievalRunRequest(
                    query=query,
                    scope="file",
                    config={
                        "file_id": file_id,
                        "limit": limit,
                        "fusion": "rrf",
                        "rrf_k": 60,
                    },
                    index_generation="file_chunks/current",
                    session_id=session_id,
                    agent_run_id=agent_run_id,
                    message_id=message_id,
                )
            )
        except Exception as exc:
            logger.warning("retrieval_trace_start_failed", error=str(exc))
            raise
        if attachment.status == AttachmentStatus.FAILED:
            await self._complete_retrieval_trace(
                run_id,
                started,
                candidate_count=0,
                status="failed",
                error_message=attachment.error_message or "文件处理失败",
            )
            return self._failed_file_search_response(attachment, query)

        limit = min(max(limit, 1), 50)
        try:
            keyword = await self._search_file_keywords(file_id, query, limit)
            vector = await self._search_file_vectors(file_id, query, limit)
            ordered = self._fuse_file_results(keyword, vector, limit)
            for item in ordered:
                item["document_version"] = attachment.document_version
        except asyncio.CancelledError:
            await self._complete_retrieval_trace(
                run_id,
                started,
                candidate_count=0,
                status="cancelled",
                error_message="检索任务被取消",
            )
            raise
        except Exception as exc:
            await self._complete_retrieval_trace(
                run_id,
                started,
                candidate_count=0,
                status="failed",
                error_message=str(exc),
            )
            raise
        try:
            raw_candidates = [
                RetrievalCandidate(
                    provider="keyword",
                    stage="native",
                    source_type="chunk",
                    source_id=chunk.id,
                    native_rank=rank,
                    native_score=chunk.native_score,
                    content_preview=chunk.content,
                    source_title=attachment.filename,
                    metadata={"document_version": attachment.document_version},
                    locator=chunk.locator.model_dump(mode="json", exclude_none=True),
                )
                for rank, chunk in enumerate(keyword, 1)
            ]
            raw_candidates.extend(
                RetrievalCandidate(
                    provider="vector",
                    stage="native",
                    source_type="chunk",
                    source_id=str(item.get("id", "")),
                    native_rank=rank,
                    native_score=item.get("native_score"),
                    content_preview=str(item.get("content", "")),
                    source_title=attachment.filename,
                    metadata={"document_version": attachment.document_version},
                    locator=item.get("locator", {}),
                )
                for rank, item in enumerate(vector, 1)
            )
            fused_candidates = [
                RetrievalCandidate(
                    provider="fusion",
                    stage="fused",
                    source_type="chunk",
                    source_id=str(item.get("id", "")),
                    fused_rank=rank,
                    fused_score=item.get("fused_score"),
                    selected_for_result=True,
                    content_preview=str(item.get("content", "")),
                    source_title=attachment.filename,
                    locator=item.get("locator", {}),
                    metadata={
                        "document_version": attachment.document_version,
                        "keyword_rank": item.get("keyword_rank"),
                        "vector_rank": item.get("vector_rank"),
                    },
                )
                for rank, item in enumerate(ordered, 1)
            ]
            await self._trace_writer.record_candidates(
                run_id, [*raw_candidates, *fused_candidates]
            )
            await self._complete_retrieval_trace(
                run_id,
                started,
                candidate_count=len(ordered),
                selected_count=len(ordered),
                status="succeeded",
            )
        except Exception as exc:
            logger.warning("retrieval_trace_record_failed", error=str(exc))
            await self._complete_retrieval_trace(
                run_id,
                started,
                candidate_count=len(ordered),
                selected_count=len(ordered),
                status="partial",
                error_message=str(exc),
            )
        for item in ordered:
            item["retrieval_run_id"] = run_id
        response: dict[str, Any] = {"query": query, "results": ordered}
        if not ordered and attachment.adapter_name == "image":
            response["message"] = (
                "图片没有可搜索的 OCR 文本；搜索工具无法检索视觉元素，请改用 analyze_file。"
            )
        return response

    async def search_knowledge(
        self,
        query: str,
        limit: int = 10,
        knowledge_base_ids: list[str] | None = None,
        *,
        session_id: str | None = None,
        agent_run_id: str | None = None,
        message_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """在全部未删除知识库文档的分块上执行一次全局混合检索。

        参数：
            query：用户检索文本。
            limit：最多返回的分块数量，范围为 1-50。

        返回：
            带文档 ID、定位器和融合分数的分块结果。

        异常：
            PostgreSQL 读取失败时向上抛出；向量检索失败时降级为关键词结果。
        """
        limit = min(max(limit, 1), 50)
        if knowledge_base_ids is not None:
            documents = await self.repository.list_knowledge_ready_documents(
                knowledge_base_ids
            )
        else:
            documents = await self.repository.list_global_knowledge_ready_documents()

        allowed_documents = {doc.id for doc in documents}
        started = asyncio.get_running_loop().time()
        run_id: str
        try:
            run_id = await self._trace_writer.start_run(
                RetrievalRunRequest(
                    query=query,
                    scope="knowledge",
                    config={"limit": limit, "fusion": "rrf", "rrf_k": 60},
                    index_generation="file_chunks/current",
                    session_id=session_id,
                    agent_run_id=agent_run_id,
                    message_id=message_id,
                )
            )
        except Exception as exc:
            logger.warning("retrieval_trace_start_failed", error=str(exc))
            raise
        vector_error: str | None = None
        try:
            keyword = [
                chunk
                for chunk in await self.repository.search_knowledge_chunks(
                    query, allowed_documents, limit
                )
                if chunk.attachment_id in allowed_documents
            ]
            vector: list[dict[str, Any]] = []
            try:
                vector = await self._vector_store.query(query, max(limit * 3, limit))
            except Exception as exc:
                vector_error = str(exc)
                logger.warning("knowledge_vector_search_failed", error=vector_error)
            live_ids = allowed_documents
            vector_before_filter = list(vector)
            vector = [
                item
                for item in vector
                if str(item.get("attachment_id", "")) in live_ids
            ]
            fused = self._fuse_file_results(keyword, vector, limit)
            document_versions = {
                document.id: document.document_version for document in documents
            }
            for item in fused:
                item["document_version"] = document_versions.get(
                    str(item.get("attachment_id", ""))
                )
        except asyncio.CancelledError:
            await self._complete_retrieval_trace(
                run_id,
                started,
                candidate_count=0,
                status="cancelled",
                error_message="检索任务被取消",
            )
            raise
        except Exception as exc:
            await self._complete_retrieval_trace(
                run_id,
                started,
                candidate_count=0,
                status="failed",
                error_message=str(exc),
            )
            raise
        try:
            raw_candidates = [
                RetrievalCandidate(
                    provider="keyword",
                    stage="native",
                    source_type="chunk",
                    source_id=chunk.id,
                    native_rank=rank,
                    native_score=chunk.native_score,
                    content_preview=chunk.content,
                    locator=chunk.locator.model_dump(mode="json", exclude_none=True),
                    source_title=next(
                        (
                            document.filename
                            for document in documents
                            if document.id == chunk.attachment_id
                        ),
                        None,
                    ),
                    metadata={"attachment_id": chunk.attachment_id},
                )
                for rank, chunk in enumerate(keyword, 1)
            ]
            raw_candidates.extend(
                RetrievalCandidate(
                    provider="vector",
                    stage="native",
                    source_type="chunk",
                    source_id=str(item.get("id", "")),
                    native_rank=rank,
                    native_score=item.get("native_score"),
                    filter_reason=(
                        None
                        if str(item.get("attachment_id", "")) in live_ids
                        else "document_not_live"
                    ),
                    content_preview=str(item.get("content", "")),
                    locator=item.get("locator", {}),
                    source_title=next(
                        (
                            document.filename
                            for document in documents
                            if document.id == item.get("attachment_id")
                        ),
                        None,
                    ),
                    metadata={
                        "attachment_id": item.get("attachment_id", ""),
                        "document_version": next(
                            (
                                document.document_version
                                for document in documents
                                if document.id == item.get("attachment_id")
                            ),
                            None,
                        ),
                    },
                )
                for rank, item in enumerate(vector_before_filter, 1)
            )
            fused_candidates = [
                RetrievalCandidate(
                    provider="fusion",
                    stage="fused",
                    source_type="chunk",
                    source_id=str(item.get("id", "")),
                    fused_rank=rank,
                    fused_score=item.get("fused_score"),
                    selected_for_result=True,
                    content_preview=str(item.get("content", "")),
                    locator=item.get("locator", {}),
                    source_title=next(
                        (
                            document.filename
                            for document in documents
                            if document.id == item.get("attachment_id")
                        ),
                        None,
                    ),
                    metadata={
                        "attachment_id": item.get("attachment_id", ""),
                        "document_version": next(
                            (
                                document.document_version
                                for document in documents
                                if document.id == item.get("attachment_id")
                            ),
                            None,
                        ),
                        "keyword_rank": item.get("keyword_rank"),
                        "vector_rank": item.get("vector_rank"),
                    },
                )
                for rank, item in enumerate(fused, 1)
            ]
            await self._trace_writer.record_candidates(
                run_id, [*raw_candidates, *fused_candidates]
            )
            await self._complete_retrieval_trace(
                run_id,
                started,
                candidate_count=len(fused),
                selected_count=len(fused),
                status="partial" if vector_error else "succeeded",
                error_message=vector_error,
            )
        except Exception as exc:
            logger.warning("retrieval_trace_record_failed", error=str(exc))
            await self._complete_retrieval_trace(
                run_id,
                started,
                candidate_count=len(fused),
                selected_count=len(fused),
                status="partial",
                error_message=str(exc),
            )
        for item in fused:
            item["retrieval_run_id"] = run_id
        return fused

    def _failed_file_search_response(
        self,
        attachment: Attachment,
        query: str,
    ) -> dict[str, Any]:
        """

        参数：
            attachment (Attachment): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            query (str): 检索或搜索文本；应为非空字符串。

        返回值：
            dict[str, Any]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        return {
            "query": query,
            "results": [],
            "error": attachment.error_message or "文件处理失败",
        }

    async def _search_file_keywords(
        self,
        file_id: str,
        query: str,
        limit: int,
    ) -> list[FileChunk]:
        """

        参数：
            file_id (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            query (str): 检索或搜索文本；应为非空字符串。
            limit (int): 最大返回数量；应为非负整数。
        返回值：
            list[FileChunk]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        try:
            keyword = await self.repository.search_chunks(file_id, query, limit=limit)
        except Exception:
            raise
        return keyword

    async def _search_file_vectors(
        self,
        file_id: str,
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """

        参数：
            file_id (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            query (str): 检索或搜索文本；应为非空字符串。
            limit (int): 最大返回数量；应为非负整数。
        返回值：
            list[dict[str, Any]]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        vector: list[dict[str, Any]] = []
        try:
            vector = await self._vector_store.query(
                query, limit, where={"attachment_id": file_id}
            )
        except Exception as exc:
            logger.warning("file_vector_search_failed", file_id=file_id, error=str(exc))
            return vector
        return vector

    def _fuse_file_results(
        self,
        keyword: list[FileChunk],
        vector: list[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        """

        参数：
            keyword (list[FileChunk]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            vector (list[dict[str, Any]]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            limit (int): 最大返回数量；应为非负整数。
        返回值：
            list[dict[str, Any]]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        scores: dict[str, float] = {}
        values: dict[str, dict[str, Any]] = {}
        for rank, chunk in enumerate(keyword, 1):
            scores[chunk.id] = scores.get(chunk.id, 0) + 1 / (60 + rank)
            values[chunk.id] = {
                "id": chunk.id,
                "content": chunk.content,
                "attachment_id": chunk.attachment_id,
                "locator": chunk.locator.model_dump(mode="json", exclude_none=True),
                "native_score": chunk.native_score,
                "keyword_rank": rank,
            }
        for rank, item in enumerate(vector, 1):
            scores[item["id"]] = scores.get(item["id"], 0) + 1 / (60 + rank)
            previous = values.get(item["id"], {})
            values[item["id"]] = {
                **previous,
                **item,
                "keyword_native_score": previous.get("native_score"),
                "vector_native_score": item.get("native_score"),
                "vector_rank": rank,
            }
        ordered = sorted(
            values.values(), key=lambda item: scores[item["id"]], reverse=True
        )[:limit]
        for rank, item in enumerate(ordered, 1):
            item["fused_score"] = scores[item["id"]]
            item["fused_rank"] = rank
        return ordered

    async def extract_table(self, session_id: str, file_id: str) -> dict[str, Any]:
        """提取文件的表格数据（从解析阶段缓存的 artifact 读取）。"""
        attachment = await self.require_attachment(session_id, file_id)
        key = self.cache_key(
            attachment,
            "tables",
            {},
            attachment.adapter_version or "",
            self.settings.primary_llm.model,
        )
        artifact = await self.repository.get_artifact(key)
        return {
            "tables": (
                json.loads(artifact.content) if artifact and artifact.content else []
            )
        }

    async def summarize_file(
        self, session_id: str, file_id: str, summary_type: str = "general"
    ) -> dict[str, Any]:
        """生成文件摘要（多级 LLM 摘要：分块 → 章节 → 文档）。

        使用 ``secondary_llm`` 处理分块和章节摘要（成本低），
        ``primary_llm`` 生成最终摘要（质量高）。结果缓存到 artifact 表。

        参数：
            session_id: 会话 ID。
            file_id: 附件 ID。
            summary_type: 摘要类型（如 ``"general"``、``"technical"``）。

        返回值：
            包含 summary 和 cached 标志的字典。
        """
        attachment = await self.require_attachment(session_id, file_id)
        key = self.cache_key(
            attachment,
            "summary",
            {"summary_type": summary_type},
            attachment.adapter_version or "",
            self.settings.primary_llm.model,
        )
        cached = await self.repository.get_artifact(key)
        if cached:
            return {"summary": cached.content or "", "cached": True}
        chunks = await self.repository.get_chunks(file_id, limit=100_000)
        if not chunks:
            if attachment.adapter_name == "image":
                message = (
                    "图片未识别出可总结的 OCR 文本。"
                    "如果需要描述截图画面，请使用 analyze_file；若未配置视觉模型，只能返回图片尺寸等元数据。"
                )
                await self.repository.put_artifact(
                    file_id, "summary", key, message, {"summary_type": summary_type}
                )
                return {"summary": message, "cached": False, "no_text": True}
            raise ValueError("文件尚未解析完成或没有可总结内容")
        summaries: list[str] = []
        for start in range(0, len(chunks), 8):
            summaries.append(
                await self._llm_summary(
                    self.secondary_llm,
                    "\n\n".join(c.content for c in chunks[start : start + 8]),
                    "分块摘要",
                )
            )
        while len(summaries) > 8:
            summaries = [
                await self._llm_summary(
                    self.secondary_llm,
                    "\n\n".join(summaries[start : start + 8]),
                    "章节摘要",
                )
                for start in range(0, len(summaries), 8)
            ]
        final = await self._llm_summary(
            self.primary_llm, "\n\n".join(summaries), f"{summary_type} 文档摘要"
        )
        await self.repository.put_artifact(
            file_id, "summary", key, final, {"summary_type": summary_type}
        )
        return {"summary": final, "cached": False}

    async def analyze_file(
        self, session_id: str, file_id: str, task: str
    ) -> dict[str, Any]:
        """对文件执行高级分析（适配器级 + 视觉模型）。

        图片文件在适配器分析基础上，额外调用视觉模型描述画面内容
        （需 primary_llm 声明 supports_vision）。

        参数：
            session_id: 会话 ID。
            file_id: 附件 ID。
            task: 分析任务的自然语言描述。

        返回值：
            分析结果字典，图片文件可能包含 ``vision`` 字段。
        """
        attachment = await self.require_attachment(session_id, file_id)
        adapter = self.adapter_registry.select(
            attachment.filename, attachment.mime_type
        )
        path = self.storage.resolve(attachment.storage_key)
        result = await adapter.analyze(path, task)
        if adapter.info.name == "image" and self.settings.primary_llm.supports_vision:
            result["vision"] = await self._vision_analysis(path, task)
        elif adapter.info.name == "image":
            result["can_describe_visual_content"] = False
            if str(result.get("ocr_text", "")).strip():
                result["analysis_source"] = "ocr"
                result["message"] = (
                    "当前主模型未声明支持视觉输入，因此无法描述图片画面；"
                    "本次使用 OCR 识别出的文字作为 fallback，仅能分析图片中的可识别文本。"
                )
            else:
                result["message"] = (
                    "当前主模型未声明支持视觉输入，因此无法描述图片画面；"
                    "图片也未识别出可用的 OCR 文本，只能返回尺寸、模式和 OCR 可用性。"
                )
        return result

    async def analyze_codebase(self, session_id: str, file_id: str) -> dict[str, Any]:
        """分析代码项目：返回语言分布、文件数、符号数和依赖数。"""
        attachment = await self.require_attachment(session_id, file_id)
        return {
            "file": attachment_to_payload(attachment),
            "languages": attachment.metadata.languages,
            "files": attachment.metadata.files or 1,
            "symbols": attachment.metadata.symbol_count or 0,
            "dependencies": attachment.metadata.dependency_count or 0,
        }

    async def find_symbol(
        self, session_id: str, file_id: str, name: str
    ) -> list[dict[str, Any]]:
        """在代码附件中按名称搜索符号（模糊匹配）。"""
        await self.require_attachment(session_id, file_id)
        return await self.repository.find_symbols(file_id, name)

    async def get_call_graph(
        self, session_id: str, file_id: str, symbol: str, direction: str = "both"
    ) -> list[dict[str, Any]]:
        """获取代码符号的调用关系图。

        参数：
            session_id: 会话 ID。
            file_id: 附件 ID。
            symbol: 符号名称。
            direction: 关系方向 — ``"outgoing"``（调用）、``"incoming"``（被调用）、
                ``"both"``（双向）。
        """
        await self.require_attachment(session_id, file_id)
        return await self.repository.find_dependencies(file_id, symbol, direction)

    async def _llm_summary(
        self, provider: LLMProvider, content: str, label: str
    ) -> str:
        """调用 LLM 生成文本摘要。

        参数：
            provider: LLM 提供者实例。
            content: 待摘要的文本内容。
            label: 摘要类型标签（如 ``"分块摘要"``、``"文档摘要"``）。

        返回值：
            生成的摘要文本。
        """
        response = await provider.ainvoke(
            [
                HumanMessage(
                    content=f"请生成忠实、紧凑的{label}。保留事实、数字、风险和结论，不添加原文没有的信息。\n\n{content}"
                )
            ]
        )
        return extract_message_text(response).strip()

    async def _vision_analysis(self, path: Path, task: str) -> str:
        """调用视觉模型分析图片内容。

        将图片编码为 base64 后通过多模态消息发送给 LLM。

        参数：
            path: 图片文件路径。
            task: 分析任务描述。

        返回值：
            视觉模型的分析结果文本。
        """
        import base64

        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        response = await self.primary_llm.ainvoke(
            [
                HumanMessage(
                    content=[
                        {"type": "text", "text": task or "请描述并分析这张图片。"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{encoded}"},
                        },
                    ]
                )
            ]
        )
        return extract_message_text(response)

    @staticmethod
    def cache_key(
        attachment: Attachment,
        capability: str,
        params: dict[str, Any],
        adapter_version: str,
        model_version: str = "configured",
        prompt_version: str = "v2",
    ) -> str:
        """生成产物缓存键（基于文件哈希 + 能力 + 参数 + 版本的 SHA-256）。

        相同文件、相同能力、相同参数和版本的组合产生相同的缓存键，
        用于 artifact 表的 upsert 去重。
        """
        raw = json.dumps(
            {
                "file_hash": attachment.sha256,
                "capability": capability,
                "params": params,
                "adapter_version": adapter_version,
                "model_version": model_version,
                "prompt_version": prompt_version,
            },
            sort_keys=True,
            ensure_ascii=True,
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    async def emit(
        self, event_type: EventType, session_id: str, data: dict[str, Any]
    ) -> None:
        """通过应用事件契约发布文件生命周期事件。

        参数:
            event_type (EventType): 文件生命周期事件类型。
            session_id (str): 事件所属会话 ID，必须非空。
            data (dict[str, Any]): 要发布的事件数据，必须可以转换为 JSON。
        返回值:
            None: 事件已交给事件发布器。
        异常:
            payload 无法序列化或事件发布失败时传播相应异常。
        """
        await self._event_publisher.publish(
            ApplicationEvent(
                event_type=event_type,
                durability=EventDurability.DURABLE,
                session_id=session_id,
                message_id=str(data["message_id"]) if data.get("message_id") else None,
                attachment_id=(
                    str(data.get("attachment_id") or data.get("id"))
                    if data.get("attachment_id") or data.get("id")
                    else None
                ),
                run_id=str(data["run_id"]) if data.get("run_id") else None,
                transition_id=(
                    str(data["transition_id"]) if data.get("transition_id") else None
                ),
                payload=data,
            )
        )

    async def emit_attachment(
        self, attachment: Attachment, *, run_id: str | None = None
    ) -> None:
        """推送附件状态更新事件。"""
        # 知识库文档不属于单一会话，状态由知识库 API 查询，不写入会话 SSE。
        if attachment.session_id is None:
            return
        data = attachment_to_payload(attachment, True)
        data["attachment_id"] = attachment.id
        if run_id:
            data["run_id"] = run_id
        data["transition_id"] = (
            f"attachment:{attachment.id}:status:{attachment.status.value}:"
            f"run:{run_id or 'none'}"
        )
        await self.emit(
            EventType.ATTACHMENT_UPDATED,
            attachment.session_id,
            data,
        )
