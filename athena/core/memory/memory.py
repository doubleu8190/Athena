"""记忆存储管理器 — 基于 ChromaDB 的长期记忆.

支持语义检索、自动摘要与生命周期管理。
- add_memory: 写入记忆（同时双写 ChromaDB + SQLite FTS5）
- search: 向量检索
- delete/pin: 生命周期管理
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from athena.config.settings import Settings, get_settings
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class MemoryManager:
    """ChromaDB 长期记忆管理器."""

    def __init__(
        self,
        chroma_client: Any | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = chroma_client
        self._collection_name = "athena_memory"
        self._collection: Any | None = None
        self._initialized = False

    async def initialize(self) -> None:
        """初始化 ChromaDB 客户端与集合."""
        if self._initialized:
            return
        if self._client is None:
            try:
                import chromadb
                self._client = chromadb.PersistentClient(path=str(self._settings.chroma_path))
            except Exception as e:
                logger.error("chromadb_init_failed", error=str(e))
                raise
        self._collection = self._client.get_or_create_collection(
            name=self._collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        self._initialized = True
        logger.info("memory_manager_initialized", path=str(self._settings.chroma_path))

    @property
    def collection(self) -> Any:
        """获取已初始化的 ChromaDB collection（调用前需先 initialize）."""
        if self._collection is None:
            raise RuntimeError("MemoryManager not initialized. Call initialize() first.")
        return self._collection

    async def add_memory(
        self,
        content: str,
        session_id: str,
        metadata: dict[str, Any] | None = None,
        pinned: bool = False,
    ) -> str:
        """写入记忆条目.

        Args:
            content: 记忆文本内容
            session_id: 所属会话 ID
            metadata: 附加元数据（category/confidence/type 等）
            pinned: 是否固定（不被 TTL 清理）

        Returns:
            记忆 ID
        """
        await self.initialize()
        memory_id = str(uuid.uuid4())
        now = datetime.now().isoformat()
        ttl_days = self._settings.memory_ttl_days
        expires_at = (datetime.now() + timedelta(days=ttl_days)).isoformat() if not pinned else None

        meta = {
            "session_id": session_id,
            "created_at": now,
            "last_accessed": now,
            "access_count": 0,
            "pinned": pinned,
            "expires_at": expires_at or "",
            **(metadata or {}),
        }

        try:
            self.collection.add(
                ids=[memory_id],
                documents=[content],
                metadatas=[meta],
            )
            logger.info("memory_added", memory_id=memory_id, session_id=session_id)
        except Exception as e:
            logger.error("memory_add_failed", error=str(e))
            raise
        return memory_id

    async def search(
        self,
        query: str,
        session_id: str | None = None,
        n_results: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """向量检索记忆."""
        await self.initialize()
        query_filter: dict[str, Any] = {}
        if session_id:
            query_filter["session_id"] = session_id
        if where:
            query_filter.update(where)

        try:
            kwargs: dict[str, Any] = {
                "query_texts": [query],
                "n_results": n_results,
                "include": ["documents", "metadatas", "distances"],
            }
            if query_filter:
                kwargs["where"] = query_filter
            results = self.collection.query(**kwargs)
        except Exception as e:
            logger.error("memory_search_failed", error=str(e))
            return []

        if not results or not results.get("ids") or not results["ids"][0]:
            return []

        out: list[dict[str, Any]] = []
        for doc_id, doc, meta, dist in zip(
            results["ids"][0],
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            score = max(0.0, 1.0 - (dist / 2.0))
            out.append({
                "id": doc_id,
                "content": doc,
                "metadata": meta or {},
                "score": score,
                "source": "vector",
            })
        return out

    async def get(self, memory_id: str) -> dict[str, Any] | None:
        """根据 ID 获取记忆."""
        await self.initialize()
        try:
            result = self.collection.get(ids=[memory_id])
            if not result or not result.get("ids"):
                return None
            return {
                "id": result["ids"][0],
                "content": result["documents"][0] if result["documents"] else "",
                "metadata": result["metadatas"][0] if result["metadatas"] else {},
            }
        except Exception as e:
            logger.error("memory_get_failed", memory_id=memory_id, error=str(e))
            return None

    async def delete(self, memory_id: str) -> None:
        """删除记忆条目."""
        await self.initialize()
        try:
            self.collection.delete(ids=[memory_id])
            logger.info("memory_deleted", memory_id=memory_id)
        except Exception as e:
            logger.error("memory_delete_failed", memory_id=memory_id, error=str(e))

    async def pin(self, memory_id: str, pinned: bool = True) -> None:
        """固定/取消固定记忆（避免 TTL 清理）."""
        await self.initialize()
        try:
            self.collection.update(
                ids=[memory_id],
                metadatas=[{"pinned": pinned, "expires_at": "" if pinned else datetime.now().isoformat()}],
            )
        except Exception as e:
            logger.error("memory_pin_failed", memory_id=memory_id, error=str(e))

    async def cleanup_expired(self) -> int:
        """清理过期且未固定的记忆."""
        await self.initialize()
        try:
            all_data = self.collection.get(
                include=["metadatas"],
            )
            if not all_data or not all_data.get("ids"):
                return 0
            now = datetime.now()
            expired_ids: list[str] = []
            for mid, meta in zip(all_data["ids"], all_data.get("metadatas", [])):
                meta = meta or {}
                if meta.get("pinned"):
                    continue
                expires_at = meta.get("expires_at", "")
                if not expires_at:
                    continue
                try:
                    exp_time = datetime.fromisoformat(expires_at)
                    if now > exp_time:
                        expired_ids.append(mid)
                except (ValueError, TypeError):
                    continue
            if expired_ids:
                self.collection.delete(ids=expired_ids)
                logger.info("memory_cleanup", count=len(expired_ids))
            return len(expired_ids)
        except Exception as e:
            logger.error("memory_cleanup_failed", error=str(e))
            return 0


# 全局单例
_memory_manager: MemoryManager | None = None


def get_memory_manager(
    chroma_client: Any | None = None,
    settings: Settings | None = None,
) -> MemoryManager:
    """获取记忆管理器单例."""
    global _memory_manager
    if _memory_manager is None:
        _memory_manager = MemoryManager(chroma_client=chroma_client, settings=settings)
    return _memory_manager


def reset_memory_manager() -> None:
    """重置单例（测试用）."""
    global _memory_manager
    _memory_manager = None
