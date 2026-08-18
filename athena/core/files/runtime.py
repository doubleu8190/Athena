"""File Intelligence 运行时 — 文件解析、索引、搜索和分析的核心编排层。

``FileIntelligenceRuntime`` 是文件智能系统的中枢，协调以下能力：

- **解析**：通过适配器提取文件内容、符号和表格，分块存储到 SQLite。
- **索引**：将分块写入 ChromaDB 向量索引，支持语义搜索。
- **读取**：按定位器（页码、工作表、路径）精准读取文件分块。
- **搜索**：融合 FTS5 关键词搜索和向量语义搜索的混合检索。
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
from pathlib import Path
from typing import Any, Awaitable, Callable

from langchain_core.messages import HumanMessage

from athena.config.settings import Settings, get_settings
from athena.core.files.base import ExtractedUnit, ExtractionContext
from athena.core.files.registry import AdapterRegistry
from athena.core.files.repository import FileRepository
from athena.core.files.storage import StorageLayer
from athena.core.llm.provider import LLMProvider
from athena.gateway.ws.events import EventType, build_event
from athena.models.file import Attachment, AttachmentStatus, FileChunk, FileTask, FileTaskType
from athena.utils.ids import generate_time_id
from athena.utils.llm import estimate_tokens, extract_message_text
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class FileAccessError(PermissionError):
    """文件访问权限错误（文件不存在或不属于当前会话）。"""


class FileIntelligenceRuntime:
    """File Intelligence 运行时，协调文件的解析、索引、搜索和分析。

    通过依赖注入获取 Repository、LLM 和 WebSocket 管理器，
    内部管理 StorageLayer、AdapterRegistry 和 ChromaDB 向量索引。

    Args:
        repository: 文件持久化仓库。
        primary_llm: 主 LLM 提供者（用于文档摘要和视觉分析）。
        secondary_llm: 次要 LLM 提供者（用于分块摘要，成本更低）。
        settings: 全局配置；为 ``None`` 时使用默认配置。
        ws_manager: WebSocket 管理器，用于推送文件状态事件。
    """

    def __init__(
        self,
        repository: FileRepository,
        primary_llm: LLMProvider,
        secondary_llm: LLMProvider,
        *,
        settings: Settings | None = None,
        ws_manager: Any | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings or get_settings()
        self.storage = StorageLayer(self.settings.files_path, self.settings.file_max_upload_bytes)
        self.adapters = AdapterRegistry()
        self.primary_llm = primary_llm
        self.secondary_llm = secondary_llm
        self.ws = ws_manager
        self._chroma_client: Any | None = None
        self._collection: Any | None = None
        self._task_enqueuer: Callable[..., Awaitable[Any]] | None = None

    def set_task_enqueuer(self, enqueuer: Callable[..., Awaitable[Any]]) -> None:
        """设置文件任务入队回调（由 FileWorker 注入）。"""
        self._task_enqueuer = enqueuer

    @property
    def task_queue_available(self) -> bool:
        """文件任务队列是否可用。"""
        return self._task_enqueuer is not None

    async def enqueue_task(
        self, session_id: str, file_id: str, task_type: FileTaskType, **kwargs: Any
    ) -> FileTask:
        """向文件任务队列提交任务。

        Args:
            session_id: 会话 ID。
            file_id: 附件 ID。
            task_type: 任务类型（解析/索引/摘要/代码分析等）。
            **kwargs: 附加任务参数。

        Returns:
            创建的 ``FileTask`` 实例。

        Raises:
            RuntimeError: 任务队列未初始化。
        """
        if self._task_enqueuer is None:
            raise RuntimeError("文件任务 worker 未初始化")
        return await self._task_enqueuer(session_id, file_id, task_type, **kwargs)

    async def initialize(self) -> None:
        """初始化运行时：同步适配器注册表到 DB，创建 ChromaDB 向量集合。"""
        await self.repository.sync_adapters(adapter.info for adapter in self.adapters.list())
        try:
            import chromadb

            self._chroma_client = chromadb.PersistentClient(path=str(self.settings.chroma_path))
            self._collection = self._chroma_client.get_or_create_collection(
                "athena_file_chunks", metadata={"hnsw:space": "cosine"}
            )
        except Exception as exc:
            logger.warning("file_vector_index_unavailable", error=str(exc))

    async def cleanup_unreferenced_blobs(self) -> int:
        """回收无引用的内容寻址 blob（附件软删除后调用）。

        Returns:
            删除的 blob 数量。
        """
        live = await self.repository.live_storage_keys()
        removed = 0
        for key in self.storage.blob_keys():
            if key in live:
                continue
            if self.storage.delete_blob(key):
                removed += 1
        return removed

    async def require_attachment(self, session_id: str, file_id: str) -> Attachment:
        """获取附件并校验会话归属。

        Args:
            session_id: 会话 ID。
            file_id: 附件 ID。

        Returns:
            ``Attachment`` 实例。

        Raises:
            FileAccessError: 附件不存在或不属于当前会话。
        """
        attachment = await self.repository.get_attachment(file_id, session_id)
        if attachment is None:
            raise FileAccessError("文件不存在或不属于当前会话")
        return attachment

    async def parse_attachment(self, attachment_id: str) -> dict[str, Any]:
        """解析附件：提取内容、分块、构建符号索引和表格缓存。"""
        attachment = await self.repository.get_attachment(attachment_id)
        if attachment is None:
            raise FileNotFoundError("附件不存在")
        adapter = self.adapters.select(attachment.filename, attachment.mime_type)
        processing = await self.repository.update_attachment(
            attachment.id, status=AttachmentStatus.PROCESSING.value,
            adapter_name=adapter.info.name, adapter_version=adapter.info.version,
            capabilities=adapter.info.capabilities, error_message=None,
        )
        if processing is not None:
            await self.emit_attachment(processing)
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
            # Adapters receive the content-addressed blob path. Replace that
            # implementation detail with the user-visible asset name in all
            # locators and code indexes before persistence.
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
            await self.repository.replace_code_index(attachment.id, result.symbols, result.dependencies)
            if result.tables:
                cache_key = self.cache_key(attachment, "tables", {}, adapter.info.version, self.settings.primary_llm.model)
                await self.repository.put_artifact(
                    attachment.id, "tables", cache_key,
                    json.dumps(result.tables, ensure_ascii=False, default=str),
                    {"count": len(result.tables)},
                )
            metadata = {**result.metadata, "chunk_count": len(chunks), "symbol_count": len(result.symbols), "dependency_count": len(result.dependencies)}
            await self.repository.update_attachment(attachment.id, metadata=metadata)
            return metadata
        finally:
            self.storage.cleanup_workspace(workspace)

    async def index_attachment(self, attachment_id: str, *, mark_ready: bool = True) -> dict[str, Any]:
        """将附件分块写入 ChromaDB 向量索引。

        按 100 个分块一批写入，失败时记录警告但不中断流程。
        ``mark_ready=True`` 时将附件状态更新为 READY。

        Args:
            attachment_id: 附件 ID。
            mark_ready: 是否在索引完成后标记附件为就绪状态。

        Returns:
            包含 chunks 数量和 vector_indexed 标志的字典。
        """
        attachment = await self.repository.get_attachment(attachment_id)
        if attachment is None:
            raise FileNotFoundError("附件不存在")
        chunks = await self.repository.get_chunks(attachment_id, limit=100_000)
        if self._collection is not None:
            try:
                await asyncio.to_thread(self._collection.delete, where={"attachment_id": attachment_id})
                for start in range(0, len(chunks), 100):
                    batch = chunks[start : start + 100]
                    if batch:
                        await asyncio.to_thread(
                            self._collection.add,
                            ids=[chunk.id for chunk in batch], documents=[chunk.content for chunk in batch],
                            metadatas=[{
                                "attachment_id": attachment_id,
                                "ordinal": chunk.ordinal,
                                "locator_json": json.dumps(chunk.locator, ensure_ascii=False),
                            } for chunk in batch],
                        )
            except Exception as exc:
                logger.warning("file_embedding_index_failed", attachment_id=attachment_id, error=str(exc))
        if mark_ready:
            ready = await self.repository.update_attachment(
                attachment_id, status=AttachmentStatus.READY.value, error_message=None
            )
            if ready is not None:
                await self.emit_attachment(ready)
        return {"chunks": len(chunks), "vector_indexed": self._collection is not None}

    def _chunk_units(self, attachment_id: str, units: list[ExtractedUnit]) -> list[FileChunk]:
        """将内容单元分块，支持换行符边界和重叠窗口。

        分块策略：
        - 最大块大小由 ``file_chunk_tokens * 4`` 决定（约 4 字符/token）。
        - 优先在换行符处断开（避免拆断行）。
        - 相邻块有重叠窗口，确保跨块搜索不丢失上下文。

        Args:
            attachment_id: 附件 ID，写入每个分块的元数据。
            units: 提取的内容单元列表。

        Returns:
            分块后的 ``FileChunk`` 列表，按序号排列。
        """
        max_chars = max(400, self.settings.file_chunk_tokens * 4)
        overlap = min(max_chars // 3, self.settings.file_chunk_overlap_tokens * 4)
        chunks: list[FileChunk] = []
        ordinal = 0
        for unit in units:
            content = unit.content.strip()
            if not content:
                continue
            start = 0
            while start < len(content):
                end = min(len(content), start + max_chars)
                # 优先在换行符处断开
                if end < len(content):
                    boundary = content.rfind("\n", start + max_chars // 2, end)
                    if boundary > start:
                        end = boundary
                piece = content[start:end].strip()
                if piece:
                    chunks.append(FileChunk(
                        id=generate_time_id(), attachment_id=attachment_id, ordinal=ordinal,
                        content=piece, token_count=estimate_tokens(piece),
                        locator={**unit.locator, "char_start": start, "char_end": end}, metadata=unit.metadata,
                    ))
                    ordinal += 1
                if end >= len(content):
                    break
                start = max(start + 1, end - overlap)
        return chunks

    async def list_files(self, session_id: str) -> list[dict[str, Any]]:
        """列出会话的所有附件（公开字段）。"""
        return [self.public_attachment(item) for item in await self.repository.list_attachments(session_id)]

    async def get_file_info(self, session_id: str, file_id: str) -> dict[str, Any]:
        """获取附件详情（含元数据）。"""
        return self.public_attachment(await self.require_attachment(session_id, file_id), include_metadata=True)

    async def read_file(self, session_id: str, file_id: str, locator: dict[str, Any] | None = None, limit: int = 10) -> dict[str, Any]:
        """读取文件内容分块。

        支持按定位器（page/sheet/path）过滤，返回匹配的分块列表。
        附件未就绪时返回 waiting 状态和关联任务信息。

        Args:
            session_id: 会话 ID。
            file_id: 附件 ID。
            locator: 可选的定位器过滤条件（如 ``{"page": 3}``）。
            limit: 返回的最大分块数（1-50）。

        Returns:
            包含 file/chunks/waiting 等字段的结果字典。
        """
        attachment = await self.require_attachment(session_id, file_id)
        if attachment.status == AttachmentStatus.FAILED:
            return {
                "file": self.public_attachment(attachment),
                "waiting": False,
                "error": attachment.error_message or "文件处理失败",
                "message": "文件处理失败，无法读取内容。请重新上传或重试处理。",
            }
        if attachment.status != AttachmentStatus.READY:
            tasks = await self.repository.list_tasks(session_id, file_id)
            await self.emit(
                EventType.AGENT_WAITING_FILE,
                session_id,
                {"file_id": file_id, "status": attachment.status.value,
                 "task_ids": [task.id for task in tasks if task.status in ("queued", "running", "waiting")]},
            )
            return {
                "file": self.public_attachment(attachment),
                "waiting": True,
                "message": "文件仍在处理中，请在任务完成后再次读取。",
                "tasks": [task.model_dump(mode="json") for task in tasks],
            }
        limit = min(max(limit, 1), 50)
        chunks = await self.repository.get_chunks(file_id, limit=100_000 if locator else limit)
        if not chunks and attachment.adapter_name == "image":
            return {
                "file": self.public_attachment(attachment),
                "chunks": [],
                "message": "图片未识别出可读取的 OCR 文本；如需描述图片画面，请使用 analyze_file，并确保视觉模型能力已启用。",
            }
        locator = locator or {}
        for key in ("page", "sheet", "path"):
            if key in locator:
                chunks = [chunk for chunk in chunks if chunk.locator.get(key) == locator[key]]
        chunks = chunks[:limit]
        return {"file": self.public_attachment(attachment), "chunks": [{"content": c.content, "locator": c.locator, "metadata": c.metadata} for c in chunks]}

    async def search_file(self, session_id: str, file_id: str, query: str, limit: int = 10) -> dict[str, Any]:
        """混合搜索文件内容（FTS5 关键词 + ChromaDB 向量语义）。

        使用 RRF (Reciprocal Rank Fusion) 融合两种搜索结果，
        公式：score = Σ 1/(60 + rank)。

        Args:
            session_id: 会话 ID。
            file_id: 附件 ID。
            query: 搜索查询文本。
            limit: 返回的最大结果数（1-50）。

        Returns:
            包含 query/results/score 的搜索结果字典。
        """
        attachment = await self.require_attachment(session_id, file_id)
        if attachment.status == AttachmentStatus.FAILED:
            return {
                "query": query,
                "results": [],
                "error": attachment.error_message or "文件处理失败",
            }
        limit = min(max(limit, 1), 50)
        keyword = await self.repository.search_chunks(file_id, query, limit=limit)
        vector: list[dict[str, Any]] = []
        if self._collection is not None:
            try:
                result = await asyncio.to_thread(
                    self._collection.query, query_texts=[query], n_results=limit,
                    where={"attachment_id": file_id}, include=["documents", "metadatas", "distances"],
                )
                for chunk_id, content, metadata, distance in zip(
                    (result.get("ids") or [[]])[0], (result.get("documents") or [[]])[0],
                    (result.get("metadatas") or [[]])[0], (result.get("distances") or [[]])[0],
                ):
                    locator = json.loads((metadata or {}).get("locator_json", "{}"))
                    vector.append({"id": chunk_id, "content": content, "locator": locator, "score": max(0.0, 1 - float(distance) / 2)})
            except Exception as exc:
                logger.warning("file_vector_search_failed", file_id=file_id, error=str(exc))
        scores: dict[str, float] = {}
        values: dict[str, dict[str, Any]] = {}
        for rank, chunk in enumerate(keyword, 1):
            scores[chunk.id] = scores.get(chunk.id, 0) + 1 / (60 + rank)
            values[chunk.id] = {"id": chunk.id, "content": chunk.content, "locator": chunk.locator}
        for rank, item in enumerate(vector, 1):
            scores[item["id"]] = scores.get(item["id"], 0) + 1 / (60 + rank)
            values[item["id"]] = item
        ordered = sorted(values.values(), key=lambda item: scores[item["id"]], reverse=True)[:limit]
        for item in ordered:
            item["score"] = scores[item["id"]]
        response: dict[str, Any] = {"query": query, "results": ordered}
        if not ordered and attachment.adapter_name == "image":
            response["message"] = "图片没有可搜索的 OCR 文本；搜索工具无法检索视觉元素，请改用 analyze_file。"
        return response

    async def extract_table(self, session_id: str, file_id: str) -> dict[str, Any]:
        """提取文件的表格数据（从解析阶段缓存的 artifact 读取）。"""
        attachment = await self.require_attachment(session_id, file_id)
        key = self.cache_key(attachment, "tables", {}, attachment.adapter_version or "", self.settings.primary_llm.model)
        artifact = await self.repository.get_artifact(key)
        return {"tables": json.loads(artifact["content"]) if artifact and artifact.get("content") else []}

    async def summarize_file(self, session_id: str, file_id: str, summary_type: str = "general") -> dict[str, Any]:
        """生成文件摘要（多级 LLM 摘要：分块 → 章节 → 文档）。

        使用 ``secondary_llm`` 处理分块和章节摘要（成本低），
        ``primary_llm`` 生成最终摘要（质量高）。结果缓存到 artifact 表。

        Args:
            session_id: 会话 ID。
            file_id: 附件 ID。
            summary_type: 摘要类型（如 ``"general"``、``"technical"``）。

        Returns:
            包含 summary 和 cached 标志的字典。
        """
        attachment = await self.require_attachment(session_id, file_id)
        key = self.cache_key(attachment, "summary", {"summary_type": summary_type}, attachment.adapter_version or "", self.settings.primary_llm.model)
        cached = await self.repository.get_artifact(key)
        if cached:
            return {"summary": cached.get("content", ""), "cached": True}
        chunks = await self.repository.get_chunks(file_id, limit=100_000)
        if not chunks:
            if attachment.adapter_name == "image":
                message = (
                    "图片未识别出可总结的 OCR 文本。"
                    "如果需要描述截图画面，请使用 analyze_file；若未配置视觉模型，只能返回图片尺寸等元数据。"
                )
                await self.repository.put_artifact(file_id, "summary", key, message, {"summary_type": summary_type})
                return {"summary": message, "cached": False, "no_text": True}
            raise ValueError("文件尚未解析完成或没有可总结内容")
        summaries: list[str] = []
        for start in range(0, len(chunks), 8):
            summaries.append(await self._llm_summary(self.secondary_llm, "\n\n".join(c.content for c in chunks[start : start + 8]), "分块摘要"))
        while len(summaries) > 8:
            summaries = [await self._llm_summary(self.secondary_llm, "\n\n".join(summaries[start : start + 8]), "章节摘要") for start in range(0, len(summaries), 8)]
        final = await self._llm_summary(self.primary_llm, "\n\n".join(summaries), f"{summary_type} 文档摘要")
        await self.repository.put_artifact(file_id, "summary", key, final, {"summary_type": summary_type})
        return {"summary": final, "cached": False}

    async def analyze_file(self, session_id: str, file_id: str, task: str) -> dict[str, Any]:
        """对文件执行高级分析（适配器级 + 视觉模型）。

        图片文件在适配器分析基础上，额外调用视觉模型描述画面内容
        （需 primary_llm 声明 supports_vision）。

        Args:
            session_id: 会话 ID。
            file_id: 附件 ID。
            task: 分析任务的自然语言描述。

        Returns:
            分析结果字典，图片文件可能包含 ``vision`` 字段。
        """
        attachment = await self.require_attachment(session_id, file_id)
        adapter = self.adapters.select(attachment.filename, attachment.mime_type)
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
        return {"file": self.public_attachment(attachment), "languages": attachment.metadata.get("languages", {}),
                "files": attachment.metadata.get("files", 1), "symbols": attachment.metadata.get("symbol_count", 0),
                "dependencies": attachment.metadata.get("dependency_count", 0)}

    async def find_symbol(self, session_id: str, file_id: str, name: str) -> list[dict[str, Any]]:
        """在代码附件中按名称搜索符号（模糊匹配）。"""
        await self.require_attachment(session_id, file_id)
        return await self.repository.find_symbols(file_id, name)

    async def get_call_graph(self, session_id: str, file_id: str, symbol: str, direction: str = "both") -> list[dict[str, Any]]:
        """获取代码符号的调用关系图。

        Args:
            session_id: 会话 ID。
            file_id: 附件 ID。
            symbol: 符号名称。
            direction: 关系方向 — ``"outgoing"``（调用）、``"incoming"``（被调用）、
                ``"both"``（双向）。
        """
        await self.require_attachment(session_id, file_id)
        return await self.repository.find_dependencies(file_id, symbol, direction)

    async def _llm_summary(self, provider: LLMProvider, content: str, label: str) -> str:
        """调用 LLM 生成文本摘要。

        Args:
            provider: LLM 提供者实例。
            content: 待摘要的文本内容。
            label: 摘要类型标签（如 ``"分块摘要"``、``"文档摘要"``）。

        Returns:
            生成的摘要文本。
        """
        response = await provider.ainvoke([HumanMessage(content=f"请生成忠实、紧凑的{label}。保留事实、数字、风险和结论，不添加原文没有的信息。\n\n{content}")])
        return extract_message_text(response).strip()

    async def _vision_analysis(self, path: Path, task: str) -> str:
        """调用视觉模型分析图片内容。

        将图片编码为 base64 后通过多模态消息发送给 LLM。

        Args:
            path: 图片文件路径。
            task: 分析任务描述。

        Returns:
            视觉模型的分析结果文本。
        """
        import base64

        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        response = await self.primary_llm.ainvoke([HumanMessage(content=[
            {"type": "text", "text": task or "请描述并分析这张图片。"},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
        ])])
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
        raw = json.dumps({"file_hash": attachment.sha256, "capability": capability, "params": params,
                          "adapter_version": adapter_version, "model_version": model_version, "prompt_version": prompt_version},
                         sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def public_attachment(attachment: Attachment, include_metadata: bool = False) -> dict[str, Any]:
        """将 Attachment 转换为公开 API 字典（排除 storage_key 等内部字段）。

        Args:
            attachment: 附件领域模型。
            include_metadata: 是否包含 metadata 字段（默认 ``False``）。
        """
        data = {"id": attachment.id, "session_id": attachment.session_id, "message_id": attachment.message_id,
                "filename": attachment.filename, "mime_type": attachment.mime_type, "size_bytes": attachment.size_bytes,
                "sha256": attachment.sha256, "status": attachment.status.value, "adapter_name": attachment.adapter_name,
                "adapter_version": attachment.adapter_version, "capabilities": attachment.capabilities,
                "error_message": attachment.error_message, "created_at": attachment.created_at.isoformat(),
                "updated_at": attachment.updated_at.isoformat()}
        if include_metadata:
            data["metadata"] = attachment.metadata
        return data

    async def emit(self, event_type: EventType | str, session_id: str, data: dict[str, Any]) -> None:
        """向会话推送 WebSocket 事件（ws_manager 为 None 时静默跳过）。"""
        if self.ws is not None:
            await self.ws.send_to_session(session_id, build_event(event_type, data, session_id=session_id))

    async def emit_file_event(self, event_type: str, session_id: str, task: Any) -> None:
        """推送文件任务事件。"""
        await self.emit(event_type, session_id, task.model_dump(mode="json"))

    async def emit_attachment(self, attachment: Attachment) -> None:
        """推送附件状态更新事件。"""
        await self.emit("attachment_updated", attachment.session_id, self.public_attachment(attachment, True))
