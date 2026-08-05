"""记忆存储管理器 — 基于 ChromaDB + SQLite FTS5 的双写长期记忆.

支持语义检索、自动摘要与生命周期管理。
- add_memory: 写入记忆（同时双写 ChromaDB + SQLite FTS5）
- search: 向量检索（ChromaDB）
- keyword_search: 关键词检索（SQLite FTS5）
- delete/pin: 生命周期管理（双写同步）
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, TYPE_CHECKING

from chromadb import Collection, QueryResult
from chromadb.api import ClientAPI
from sqlalchemy import insert, text, update

from athena.config.settings import Settings, get_settings
from athena.db.engine import get_session
from athena.db.models import MemoryModel
from athena.utils.ids import generate_time_id
from athena.utils.logging import get_logger

if TYPE_CHECKING:
    import chromadb

logger = get_logger(__name__)


class MemoryManager:
    """ChromaDB + SQLite FTS5 双写长期记忆管理器."""

    def __init__(
        self,
        chroma_client: ClientAPI | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = chroma_client
        self._collection_name = "athena_memory"
        self._collection: Collection | None = None
        self._initialized = False

    async def initialize(self) -> None:
        """初始化 ChromaDB 客户端与集合."""
        if self._initialized:
            return
        if self._client is None:
            try:
                import chromadb

                self._client = chromadb.PersistentClient(
                    path=str(self._settings.chroma_path)
                )
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
    def collection(self) -> Collection:
        """获取已初始化的 ChromaDB collection（调用前需先 initialize）."""
        if self._collection is None:
            raise RuntimeError(
                "MemoryManager not initialized. Call initialize() first."
            )
        return self._collection

    # ------------------------------------------------------------------
    # 写入：双写 SQLite + ChromaDB (memories 表 + memory_fts 虚拟表)
    # ------------------------------------------------------------------

    async def add_memory(
        self,
        content: str,
        metadata: dict[str, Any] | None = None,
        pinned: bool = False,
    ) -> str:
        """写入记忆条目（双写 SQLite FTS5 + ChromaDB）.

        写入顺序：SQLite → ChromaDB。ChromaDB 写入失败时回滚 SQLite，
        保证两边数据一致。

        Args:
            content: 记忆文本内容
            metadata: 附加元数据（须包含 session_id，以及 category/confidence/type 等）
            pinned: 是否固定（不被 TTL 清理）

        Returns:
            记忆 ID
        """
        metadata = metadata or {}
        session_id = metadata.get("session_id", "")
        if not session_id:
            logger.warning("add_memory_missing_session_id")

        await self.initialize()
        memory_id = generate_time_id()
        now = datetime.now().isoformat()
        ttl_days = self._settings.memory_ttl_days
        expires_at = (
            (datetime.now() + timedelta(days=ttl_days)).isoformat()
            if not pinned
            else None
        )

        meta = {
            "created_at": now,
            "last_accessed": now,
            "access_count": 0,
            "pinned": pinned,
            "expires_at": expires_at or "",
            **metadata,
        }

        # 1. 先写 SQLite（可事务回滚）
        try:
            async with get_session() as session:
                async with session.begin():
                    await session.execute(
                        insert(MemoryModel).values(
                            id=memory_id,
                            session_id=session_id,
                            content=content,
                            metadata_json=json.dumps(
                                meta, ensure_ascii=False, default=str
                            ),
                            pinned=1 if pinned else 0,
                            expires_at=expires_at,
                            created_at=now,
                            last_accessed=now,
                            access_count=0,
                        )
                    )
                    await session.execute(
                        text(
                            "INSERT INTO memory_fts (content, memory_id) VALUES (:content, :memory_id)"
                        ),
                        {"content": content, "memory_id": memory_id},
                    )
        except Exception as e:
            logger.error("memory_add_sqlite_failed", error=str(e))
            raise

        # 2. 再写 ChromaDB；失败则回滚 SQLite 保证一致
        try:
            self.collection.add(
                ids=[memory_id],
                documents=[content],
                metadatas=[meta],
            )
        except Exception as e:
            logger.error("memory_add_chromadb_failed", error=str(e))
            await self._rollback_add(memory_id)
            raise

        logger.info("memory_added", memory_id=memory_id, session_id=session_id)
        return memory_id

    # ------------------------------------------------------------------
    # 向量检索（ChromaDB）
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        n_results: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """向量检索记忆（ChromaDB）.

        Args:
            query: 查询文本
            n_results: 返回结果数量
            where: ChromaDB 过滤条件（如 {"session_id": "xxx"}）
        """
        await self.initialize()

        try:
            kwargs: dict[str, Any] = {
                "query_texts": [query],
                "n_results": n_results,
                "include": ["documents", "metadatas", "distances"],
            }
            if where:
                kwargs["where"] = where
            results: QueryResult = self.collection.query(**kwargs)
        except Exception as e:
            logger.error("memory_search_failed", error=str(e))
            return []

        ids = results.get("ids") or []
        if not ids or not ids[0]:
            return []

        # ChromaDB 单查询返回单行结果，逐字段取首行并做防御性兜底
        row_ids = ids[0]
        documents = (results.get("documents") or [[]])[0]
        metadatas = (results.get("metadatas") or [[]])[0]
        distances = (results.get("distances") or [[]])[0]

        out: list[dict[str, Any]] = []
        for doc_id, doc, meta, dist in zip(row_ids, documents, metadatas, distances):
            # cosine 距离 ∈ [0,2]，线性归一化为 0-1 相似度
            score = max(0.0, 1.0 - (float(dist) / 2.0))
            out.append(
                {
                    "id": doc_id,
                    "content": doc,
                    "metadata": meta or {},
                    "score": score,
                    "source": "vector",
                }
            )
        return out

    # ------------------------------------------------------------------
    # 关键词检索（SQLite FTS5）
    # ------------------------------------------------------------------

    async def keyword_search(
        self,
        query: str,
        n_results: int = 10,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """关键词检索记忆（SQLite FTS5 MATCH）.

        使用 FTS5 的 MATCH 操作符进行全文检索，
        tokenize='unicode61' 支持中文分词。

        Args:
            query: 查询文本
            n_results: 返回结果数量
            where: SQL 过滤条件（仅支持 memories 表顶层列，如 {"session_id": "xxx"}）
        """
        # 构建 FTS5 MATCH 查询：对查询中的每个词用 OR 连接
        # FTS5 语法：双引号包裹避免特殊字符干扰
        import re

        tokens = re.findall(r"[\w\u4e00-\u9fa5]+", query)
        if not tokens:
            return []

        # 转义双引号并构建 MATCH 表达式
        match_terms = " OR ".join(f'"{t}"' for t in tokens if len(t) > 0)
        if not match_terms:
            return []

        try:
            async with get_session() as session:
                # FTS5 MATCH 查询 + 关联 memories 表获取完整数据
                base_sql = """
                    SELECT m.id, m.content, m.metadata_json, m.session_id,
                           m.created_at, m.pinned, m.expires_at,
                           bm25(memory_fts) AS rank
                    FROM memory_fts
                    JOIN memories m ON memory_fts.memory_id = m.id
                    WHERE memory_fts MATCH :match_expr
                      AND m.deleted_time IS NULL
                """
                params: dict[str, Any] = {"match_expr": match_terms}

                # 将 where 条件转为 SQL AND 子句（仅支持 memories 表顶层列）
                extra_conditions = ""
                if where:
                    for key, value in where.items():
                        param_name = f"_where_{key}"
                        extra_conditions += f" AND m.{key} = :{param_name}"
                        params[param_name] = value

                sql = text(base_sql + extra_conditions + f" LIMIT {n_results}")
                result = await session.execute(sql, params)
                rows = result.fetchall()
        except Exception as e:
            logger.error("memory_keyword_search_failed", error=str(e))
            return []

        out: list[dict[str, Any]] = []
        for row in rows:
            meta = {}
            try:
                meta = json.loads(row.metadata_json) if row.metadata_json else {}
            except (json.JSONDecodeError, TypeError):
                pass
            # bm25 返回负值，越小越相关，转换为 0-1 的分数
            raw_rank = row.rank if row.rank is not None else 0.0
            score = max(0.0, min(1.0, 1.0 / (1.0 + abs(raw_rank))))
            out.append(
                {
                    "id": row.id,
                    "content": row.content,
                    "metadata": meta,
                    "score": score,
                    "source": "keyword",
                }
            )
        return out

    # ------------------------------------------------------------------
    # 单条获取
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # 删除：双删 SQLite + ChromaDB
    # ------------------------------------------------------------------

    async def delete(self, memory_id: str) -> None:
        """删除记忆条目（SQLite 软删除 + FTS5 删除 → ChromaDB 删除）.

        先软删 SQLite（可回滚），再删 ChromaDB；ChromaDB 失败时回滚 SQLite。
        """
        await self.initialize()

        # 1. 先软删 SQLite
        try:
            async with get_session() as session:
                async with session.begin():
                    now = datetime.now().isoformat()
                    await session.execute(
                        update(MemoryModel)
                        .where(MemoryModel.id == memory_id)
                        .values(deleted_time=now)
                    )
                    await session.execute(
                        text("DELETE FROM memory_fts WHERE memory_id = :memory_id"),
                        {"memory_id": memory_id},
                    )
        except Exception as e:
            logger.error(
                "memory_delete_sqlite_failed", memory_id=memory_id, error=str(e)
            )
            raise

        # 2. 再删 ChromaDB；失败则回滚 SQLite 软删除
        try:
            self.collection.delete(ids=[memory_id])
        except Exception as e:
            logger.error(
                "memory_delete_chromadb_failed", memory_id=memory_id, error=str(e)
            )
            await self._rollback_delete(memory_id)
            raise

        logger.info("memory_deleted", memory_id=memory_id)

    # ------------------------------------------------------------------
    # 固定/取消固定（双写同步）
    # ------------------------------------------------------------------

    async def pin(self, memory_id: str, pinned: bool = True) -> None:
        """固定/取消固定记忆（避免 TTL 清理）.

        先更新 SQLite，再更新 ChromaDB；ChromaDB 失败时回滚 SQLite。
        """
        await self.initialize()
        expires_at = None if pinned else datetime.now().isoformat()
        new_pinned = 1 if pinned else 0

        # 1. 先更新 SQLite
        try:
            async with get_session() as session:
                async with session.begin():
                    await session.execute(
                        update(MemoryModel)
                        .where(MemoryModel.id == memory_id)
                        .values(pinned=new_pinned, expires_at=expires_at)
                    )
        except Exception as e:
            logger.error("memory_pin_sqlite_failed", memory_id=memory_id, error=str(e))
            raise

        # 2. 再更新 ChromaDB；失败则回滚 SQLite
        try:
            self.collection.update(
                ids=[memory_id],
                metadatas=[
                    {
                        "pinned": pinned,
                        "expires_at": "" if pinned else datetime.now().isoformat(),
                    }
                ],
            )
        except Exception as e:
            logger.error(
                "memory_pin_chromadb_failed", memory_id=memory_id, error=str(e)
            )
            await self._rollback_pin(memory_id)
            raise

    # ------------------------------------------------------------------
    # 清理过期记忆（双写同步）
    # ------------------------------------------------------------------

    async def cleanup_expired(self) -> int:
        """清理过期且未固定的记忆.

        先软删 SQLite，再删 ChromaDB；ChromaDB 失败时回滚 SQLite。
        """
        await self.initialize()
        try:
            all_data = self.collection.get(
                include=["metadatas"],
            )
            ids = all_data.get("ids") if all_data else []
            if not ids:
                return 0
            now = datetime.now()
            expired_ids: list[str] = []
            for mid, meta in zip(ids, all_data.get("metadatas") or []):
                meta = meta or {}
                if meta.get("pinned"):
                    continue
                expires_at = meta.get("expires_at", "")
                if not expires_at or not isinstance(expires_at, str):
                    continue
                try:
                    exp_time = datetime.fromisoformat(expires_at)
                    if now > exp_time:
                        expired_ids.append(mid)
                except (ValueError, TypeError):
                    continue
            if not expired_ids:
                return 0

            now_iso = now.isoformat()

            # 1. 先软删 SQLite
            try:
                async with get_session() as session:
                    async with session.begin():
                        for mid in expired_ids:
                            await session.execute(
                                update(MemoryModel)
                                .where(MemoryModel.id == mid)
                                .values(deleted_time=now_iso)
                            )
                            await session.execute(
                                text("DELETE FROM memory_fts WHERE memory_id = :mid"),
                                {"mid": mid},
                            )
            except Exception as e:
                logger.error("memory_cleanup_sqlite_failed", error=str(e))
                raise

            # 2. 再删 ChromaDB；失败则回滚 SQLite 软删除
            try:
                self.collection.delete(ids=expired_ids)
            except Exception as e:
                logger.error("memory_cleanup_chromadb_failed", error=str(e))
                await self._rollback_cleanup(expired_ids)
                raise

            logger.info("memory_cleanup", count=len(expired_ids))
            return len(expired_ids)
        except Exception as e:
            logger.error("memory_cleanup_failed", error=str(e))
            return 0

    # ------------------------------------------------------------------
    # 回滚补偿（ChromaDB 失败时撤销已提交的 SQLite 写入）
    # ------------------------------------------------------------------

    async def _rollback_add(self, memory_id: str) -> None:
        """回滚 add_memory：硬删 SQLite 记录 + FTS5 索引."""
        try:
            async with get_session() as session:
                async with session.begin():
                    await session.execute(
                        text("DELETE FROM memories WHERE id = :id"),
                        {"id": memory_id},
                    )
                    await session.execute(
                        text("DELETE FROM memory_fts WHERE memory_id = :id"),
                        {"id": memory_id},
                    )
            logger.warning("rollback_add_succeeded", memory_id=memory_id)
        except Exception as e:
            logger.error("rollback_add_failed", memory_id=memory_id, error=str(e))

    async def _rollback_delete(self, memory_id: str) -> None:
        """回滚 delete：恢复 SQLite 软删除（置空 deleted_time）+ 重建 FTS5 索引."""
        try:
            async with get_session() as session:
                async with session.begin():
                    await session.execute(
                        update(MemoryModel)
                        .where(MemoryModel.id == memory_id)
                        .values(deleted_time=None)
                    )
                    # 从 memories 表取回 content 重建 FTS5
                    row = (
                        await session.execute(
                            text("SELECT content FROM memories WHERE id = :id"),
                            {"id": memory_id},
                        )
                    ).fetchone()
                    if row:
                        await session.execute(
                            text(
                                "INSERT INTO memory_fts (content, memory_id) VALUES (:content, :id)"
                            ),
                            {"content": row.content, "id": memory_id},
                        )
            logger.warning("rollback_delete_succeeded", memory_id=memory_id)
        except Exception as e:
            logger.error("rollback_delete_failed", memory_id=memory_id, error=str(e))

    async def _rollback_pin(self, memory_id: str) -> None:
        """回滚 pin：恢复 SQLite 的 pinned 和 expires_at 到操作前的值."""
        try:
            async with get_session() as session:
                async with session.begin():
                    # 读取当前值来反转
                    row = (
                        await session.execute(
                            text("SELECT pinned, expires_at FROM memories WHERE id = :id"),
                            {"id": memory_id},
                        )
                    ).fetchone()
                    if row:
                        reverted_pinned = 0 if row.pinned else 1
                        reverted_expires = None if row.pinned else (row.expires_at or datetime.now().isoformat())
                        await session.execute(
                            update(MemoryModel)
                            .where(MemoryModel.id == memory_id)
                            .values(pinned=reverted_pinned, expires_at=reverted_expires)
                        )
            logger.warning("rollback_pin_succeeded", memory_id=memory_id)
        except Exception as e:
            logger.error("rollback_pin_failed", memory_id=memory_id, error=str(e))

    async def _rollback_cleanup(self, memory_ids: list[str]) -> None:
        """回滚 cleanup_expired：恢复 SQLite 软删除 + 重建 FTS5 索引."""
        try:
            async with get_session() as session:
                async with session.begin():
                    for mid in memory_ids:
                        await session.execute(
                            update(MemoryModel)
                            .where(MemoryModel.id == mid)
                            .values(deleted_time=None)
                        )
                        row = (
                            await session.execute(
                                text("SELECT content FROM memories WHERE id = :id"),
                                {"id": mid},
                            )
                        ).fetchone()
                        if row:
                            await session.execute(
                                text(
                                    "INSERT INTO memory_fts (content, memory_id) VALUES (:content, :id)"
                                ),
                                {"content": row.content, "id": mid},
                            )
            logger.warning("rollback_cleanup_succeeded", count=len(memory_ids))
        except Exception as e:
            logger.error("rollback_cleanup_failed", error=str(e))


# 全局单例
_memory_manager: MemoryManager


def get_memory_manager() -> MemoryManager:
    """获取记忆管理器单例."""
    return _memory_manager


def set_memory_manager(manager: MemoryManager) -> None:
    """设置全局记忆管理器实例（测试用）."""
    global _memory_manager
    _memory_manager = manager
