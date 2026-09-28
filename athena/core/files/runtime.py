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
from typing import Any, Protocol

from athena.config.settings import Settings
from athena.core.retrieval import RetrievalCandidate, RetrievalRunRequest
from athena.core.retrieval.ports import RetrievalTraceWriter
from athena.core.files.extraction import ExtractedUnit
from athena.core.files.chunking import FileChunker
from athena.core.files.retrieval_pipeline import (
    HybridCandidatePipeline,
    RetrievalTraceRecorder,
)
from athena.core.files.ingestion import FileIngestionService
from athena.core.files.analysis import FileAnalysisService
from athena.core.files.ports import FileVectorStore
from athena.core.files.ports import FileReranker
from athena.core.files.contracts import FileRetrievalCandidate
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
        reranker: FileReranker | None = None,
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
        self._event_publisher = event_publisher
        self._trace_writer = trace_writer
        self._vector_store = vector_store
        self._reranker = reranker
        self.file_token_counter = file_token_counter or EmbeddingTokenCounter(
            conservative_text_token_count
        )
        self._chunker = FileChunker(self.file_token_counter)
        self._candidate_pipeline = HybridCandidatePipeline(reranker)
        self._trace_recorder = RetrievalTraceRecorder(trace_writer)
        self._ingestion = FileIngestionService(
            repository,
            settings=settings,
            storage=self.storage,
            adapter_registry=self.adapter_registry,
            vector_store=vector_store,
            chunker=self._chunker,
            cache_key=self.cache_key,
            emit_attachment=self.emit_attachment,
            initialize=self.initialize,
        )
        self._analysis = FileAnalysisService(
            repository,
            settings=settings,
            primary_llm=primary_llm,
            secondary_llm=secondary_llm,
            adapter_registry=self.adapter_registry,
            storage=self.storage,
            cache_key=self.cache_key,
        )

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
        await self._trace_recorder.complete(
            run_id,
            started,
            candidate_count=candidate_count,
            selected_count=selected_count,
            status=status,
            error_message=error_message,
        )

    async def initialize(self) -> None:
        """初始化运行时：同步适配器注册表和文件向量索引。"""
        await self.repository.sync_adapters(
            adapter.info for adapter in self.adapter_registry.list()
        )
        if self._reranker is not None:
            await self._reranker.initialize()

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
        return await self._ingestion.parse_attachment(attachment_id, run_id=run_id)

    async def index_attachment(self, attachment_id: str) -> dict[str, Any]:
        return await self._ingestion.index_attachment(attachment_id)

    async def process_knowledge_document(self, attachment_id: str) -> dict[str, Any]:
        """幂等完成知识库文档解析和向量索引。

        参数：
            attachment_id：知识库文档附件 ID。

        返回：
            解析和索引阶段的统计信息。

        异常：
            解析、PostgreSQL 写入或向量索引失败时向上抛出，由任务 worker 负责重试。
        """
        return await self._ingestion.process_knowledge_document(attachment_id)

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
        return await self._ingestion.delete_knowledge_document(
            attachment_id, knowledge_base_id
        )

    async def delete_attachment_vectors(self, attachment_id: str) -> None:
        """删除某附件在 Chroma 中的全部向量。

        参数：
            attachment_id：附件唯一标识。

        返回：
            None。

        异常：
            Chroma 删除失败时向上抛出异常。
        """
        await self._ingestion.delete_attachment_vectors(attachment_id)

    def _chunk_units(
        self, attachment_id: str, units: list[ExtractedUnit]
    ) -> list[FileChunk]:
        self._chunker = FileChunker(self.file_token_counter)
        return self._chunker.chunk(
            attachment_id,
            units,
            max_tokens=self.settings.file_chunk_tokens,
            overlap_tokens=self.settings.file_chunk_overlap_tokens,
        )

    @staticmethod
    def _chunk_locator(
        group: list[ExtractedUnit], start: int, end: int
    ) -> dict[str, Any]:
        return FileChunker.chunk_locator(group, start, end)

    def _find_chunk_end(self, content: str, start: int, max_tokens: int) -> int:
        self._chunker = FileChunker(self.file_token_counter)
        return self._chunker.find_chunk_end(content, start, max_tokens)

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
                        "candidate_k": self.settings.file_rerank_candidate_k,
                        "rerank_k": self.settings.file_rerank_k,
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

        # API 调用方的 limit 仍然是最终返回上限；上下文 Provider 会自行传入更小的 limit。
        result_k = min(max(limit, 1), 50)
        candidate_k = max(result_k, self.settings.file_rerank_candidate_k)
        rerank_k = min(candidate_k, self.settings.file_rerank_k)
        try:
            keyword = await self._search_file_keywords(file_id, query, candidate_k)
            vector = await self._search_file_vectors(file_id, query, candidate_k)
            fused = self._fuse_file_results(
                keyword,
                vector,
                candidate_k,
                source_title=attachment.filename,
                document_version=attachment.document_version,
            )
            reranked = await self._rerank_file_results(
                query, fused[:rerank_k], rerank_k
            )
            ordered = reranked[:result_k]
            selected_ids = {item.source_id for item in ordered}
            for item in fused:
                item.selected_for_result = item.source_id in selected_ids
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
                    source_id=chunk.source_id,
                    native_rank=rank,
                    native_score=chunk.keyword_score,
                    content_preview=chunk.content,
                    source_title=attachment.filename,
                    metadata={"document_version": attachment.document_version},
                    locator=chunk.locator,
                )
                for rank, chunk in enumerate(keyword, 1)
            ]
            raw_candidates.extend(
                RetrievalCandidate(
                    provider="vector",
                    stage="native",
                    source_type="chunk",
                    source_id=item.source_id,
                    native_rank=rank,
                    native_score=item.vector_score,
                    content_preview=item.content,
                    source_title=attachment.filename,
                    metadata={"document_version": attachment.document_version},
                    locator=item.locator,
                )
                for rank, item in enumerate(vector, 1)
            )
            fused_candidates = [
                RetrievalCandidate(
                    provider="fusion",
                    stage="fused",
                    source_type="chunk",
                    source_id=item.source_id,
                    fused_rank=item.fused_rank or rank,
                    fused_score=item.fused_score,
                    selected_for_result=item.selected_for_result,
                    content_preview=item.content,
                    source_title=attachment.filename,
                    locator=item.locator,
                    metadata={
                        "document_version": attachment.document_version,
                        "keyword_rank": item.keyword_rank,
                        "vector_rank": item.vector_rank,
                        "keyword_score": item.keyword_score,
                        "vector_score": item.vector_score,
                    },
                )
                for rank, item in enumerate(fused, 1)
            ]
            reranked_candidates = [
                RetrievalCandidate(
                    provider="reranker",
                    stage="reranked",
                    source_type="chunk",
                    source_id=item.source_id,
                    fused_rank=item.fused_rank,
                    fused_score=item.fused_score,
                    rerank_rank=item.rerank_rank,
                    rerank_score=item.rerank_score,
                    selected_for_result=item.selected_for_result,
                    content_preview=item.content,
                    source_title=attachment.filename,
                    locator=item.locator,
                    metadata={"document_version": attachment.document_version},
                )
                for item in reranked
            ]
            await self._trace_recorder.record(
                run_id,
                [*raw_candidates, *fused_candidates, *reranked_candidates],
                candidate_count=len(fused),
                selected_count=len(ordered),
                started=started,
            )
        except Exception as exc:
            logger.warning("retrieval_trace_record_failed", error=str(exc))
            await self._complete_retrieval_trace(
                run_id,
                started,
                candidate_count=len(fused),
                selected_count=len(ordered),
                status="partial",
                error_message=str(exc),
            )
        for item in ordered:
            item.retrieval_run_id = run_id
        response: dict[str, Any] = {
            "query": query,
            "results": [item.to_dict() for item in ordered],
        }
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
        result_k = min(max(limit, 1), self.settings.knowledge_result_k, 50)
        candidate_k = max(result_k, self.settings.knowledge_rerank_candidate_k)
        rerank_k = min(candidate_k, self.settings.knowledge_rerank_k)
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
                    config={
                        "limit": result_k,
                        "candidate_k": candidate_k,
                        "rerank_k": rerank_k,
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
        vector_error: str | None = None
        try:
            keyword = [
                FileRetrievalCandidate(
                    source_id=chunk.id,
                    content=chunk.content,
                    attachment_id=chunk.attachment_id,
                    locator=chunk.locator.model_dump(mode="json", exclude_none=True),
                    keyword_score=chunk.native_score,
                )
                for chunk in await self.repository.search_knowledge_chunks(
                    query, allowed_documents, candidate_k
                )
                if chunk.attachment_id in allowed_documents
            ]
            vector: list[FileRetrievalCandidate] = []
            try:
                vector = await self._vector_store.query(query, candidate_k)
            except Exception as exc:
                vector_error = str(exc)
                logger.warning("knowledge_vector_search_failed", error=vector_error)
            live_ids = allowed_documents
            vector_before_filter = list(vector)
            vector = [item for item in vector if item.attachment_id in live_ids]
            fused_pool = self._fuse_file_results(keyword, vector, candidate_k)
            document_versions = {
                document.id: document.document_version for document in documents
            }
            titles = {document.id: document.filename for document in documents}
            for item in fused_pool:
                item.document_version = document_versions.get(item.attachment_id)
                item.source_title = titles.get(item.attachment_id)
            reranked = await self._rerank_file_results(
                query, fused_pool[:rerank_k], rerank_k
            )
            selected_ids = {item.source_id for item in reranked[:result_k]}
            for item in fused_pool:
                item.selected_for_result = item.source_id in selected_ids
            selected_results = self._diversify_knowledge_results(
                reranked, result_k, max_per_document=3
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
                    source_id=chunk.source_id,
                    native_rank=rank,
                    native_score=chunk.keyword_score,
                    content_preview=chunk.content,
                    locator=chunk.locator,
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
                    source_id=item.source_id,
                    native_rank=rank,
                    native_score=item.vector_score,
                    filter_reason=(
                        None if item.attachment_id in live_ids else "document_not_live"
                    ),
                    content_preview=item.content,
                    locator=item.locator,
                    source_title=next(
                        (
                            document.filename
                            for document in documents
                            if document.id == item.attachment_id
                        ),
                        None,
                    ),
                    metadata={
                        "attachment_id": item.attachment_id,
                        "document_version": next(
                            (
                                document.document_version
                                for document in documents
                                if document.id == item.attachment_id
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
                    source_id=item.source_id,
                    fused_rank=item.fused_rank or rank,
                    fused_score=item.fused_score,
                    selected_for_result=item.selected_for_result,
                    content_preview=item.content,
                    locator=item.locator,
                    source_title=item.source_title,
                    metadata={
                        "attachment_id": item.attachment_id,
                        "document_version": item.document_version,
                        "keyword_rank": item.keyword_rank,
                        "vector_rank": item.vector_rank,
                        "keyword_score": item.keyword_score,
                        "vector_score": item.vector_score,
                    },
                )
                for rank, item in enumerate(fused_pool, 1)
            ]
            reranked_candidates = [
                RetrievalCandidate(
                    provider="reranker",
                    stage="reranked",
                    source_type="chunk",
                    source_id=item.source_id,
                    fused_rank=item.fused_rank,
                    fused_score=item.fused_score,
                    rerank_rank=item.rerank_rank,
                    rerank_score=item.rerank_score,
                    selected_for_result=item.selected_for_result,
                    content_preview=item.content,
                    source_title=item.source_title,
                    locator=item.locator,
                    metadata={"attachment_id": item.attachment_id},
                )
                for item in reranked
            ]
            await self._trace_recorder.record(
                run_id,
                [*raw_candidates, *fused_candidates, *reranked_candidates],
                candidate_count=len(fused_pool),
                selected_count=len(selected_results),
                started=started,
                error_message=vector_error,
            )
        except Exception as exc:
            logger.warning("retrieval_trace_record_failed", error=str(exc))
            await self._complete_retrieval_trace(
                run_id,
                started,
                candidate_count=len(fused_pool),
                selected_count=len(selected_results),
                status="partial",
                error_message=str(exc),
            )
        for item in selected_results:
            item.retrieval_run_id = run_id
        return [item.to_dict() for item in selected_results]

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
    ) -> list[FileRetrievalCandidate]:
        """

        参数：
            file_id (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            query (str): 检索或搜索文本；应为非空字符串。
            limit (int): 最大返回数量；应为非负整数。
        返回值：
            list[FileRetrievalCandidate]: 返回关键词候选。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        try:
            keyword = await self.repository.search_chunks(file_id, query, limit=limit)
        except Exception:
            raise
        return [
            FileRetrievalCandidate(
                source_id=chunk.id,
                content=chunk.content,
                attachment_id=chunk.attachment_id,
                locator=chunk.locator.model_dump(mode="json", exclude_none=True),
                keyword_score=chunk.native_score,
            )
            for chunk in keyword
        ]

    async def _search_file_vectors(
        self,
        file_id: str,
        query: str,
        limit: int,
    ) -> list[FileRetrievalCandidate]:
        """

        参数：
            file_id (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            query (str): 检索或搜索文本；应为非空字符串。
            limit (int): 最大返回数量；应为非负整数。
        返回值：
            list[FileRetrievalCandidate]: 返回向量候选。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        vector: list[FileRetrievalCandidate] = []
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
        keyword: list[FileRetrievalCandidate],
        vector: list[FileRetrievalCandidate],
        limit: int,
        *,
        source_title: str | None = None,
        document_version: int | None = None,
    ) -> list[FileRetrievalCandidate]:
        """

        参数：
            keyword (list[FileChunk]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            vector (list[dict[str, Any]]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            limit (int): 最大返回数量；应为非负整数。
        返回值：
            list[FileRetrievalCandidate]: RRF 融合后的候选。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        pipeline = getattr(self, "_candidate_pipeline", None)
        if pipeline is None:
            pipeline = HybridCandidatePipeline(getattr(self, "_reranker", None))
        return pipeline.fuse(
            keyword,
            vector,
            limit,
            source_title=source_title,
            document_version=document_version,
        )

    async def _rerank_file_results(
        self,
        query: str,
        candidates: list[FileRetrievalCandidate],
        limit: int,
    ) -> list[FileRetrievalCandidate]:
        """重排文件候选；模型失败时保留 RRF 顺序并记录降级。"""
        pipeline = getattr(self, "_candidate_pipeline", None)
        if pipeline is None:
            pipeline = HybridCandidatePipeline(getattr(self, "_reranker", None))
        return await pipeline.rerank(query, candidates, limit)

    @staticmethod
    def _diversify_knowledge_results(
        candidates: list[FileRetrievalCandidate],
        limit: int,
        *,
        max_per_document: int,
    ) -> list[FileRetrievalCandidate]:
        """限制单文档占比，避免知识库结果被单一文档垄断。"""
        return HybridCandidatePipeline.diversify(
            candidates, limit, max_per_document=max_per_document
        )

    async def extract_table(self, session_id: str, file_id: str) -> dict[str, Any]:
        """提取文件的表格数据（从解析阶段缓存的 artifact 读取）。"""
        attachment = await self.require_attachment(session_id, file_id)
        return await self._analysis.extract_table(attachment)

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
        return await self._analysis.summarize(attachment, summary_type)

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
        return await self._analysis.analyze(attachment, task)

    async def analyze_codebase(self, session_id: str, file_id: str) -> dict[str, Any]:
        """分析代码项目：返回语言分布、文件数、符号数和依赖数。"""
        attachment = await self.require_attachment(session_id, file_id)
        return self._analysis.codebase_overview(attachment)

    async def find_symbol(
        self, session_id: str, file_id: str, name: str
    ) -> list[dict[str, Any]]:
        """在代码附件中按名称搜索符号（模糊匹配）。"""
        await self.require_attachment(session_id, file_id)
        return await self._analysis.find_symbol(file_id, name)

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
        return await self._analysis.call_graph(file_id, symbol, direction)

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
