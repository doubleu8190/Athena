"""记忆存储管理器 — 基于 ChromaDB + SQLite FTS5 的双写长期记忆.

支持语义检索、自动摘要与生命周期管理。
- add_memory: 写入记忆（同时双写 ChromaDB + SQLite FTS5）
- search: 向量检索（ChromaDB）
- keyword_search: 关键词检索（SQLite FTS5）
- delete/pin: 生命周期管理（双写同步）
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, TYPE_CHECKING

from chromadb import Collection, QueryResult
from chromadb.api import ClientAPI
from sqlalchemy import case, func, insert, select, text, update

from athena.config.settings import Settings, get_settings
from athena.db.engine import get_session
from athena.db.models import MemoryModel
from athena.db.repository import _json_dumps, _json_loads
from athena.utils.ids import generate_time_id
from athena.utils.logging import get_logger

if TYPE_CHECKING:
    import chromadb

logger = get_logger(__name__)


@dataclass
class _AccessStat:
    """内存中累积的一次访问统计（待批量落盘）."""

    count: int = 1
    last_accessed: str = ""


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
        # 内存中的访问统计累积（读路径 O(1) 记录，周期批量落盘）
        self._access_stats: dict[str, _AccessStat] = {}

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
    # 访问追踪：读路径内存累积 + 周期批量落盘（滑动 TTL 在此刷新）
    # ------------------------------------------------------------------

    def _record_access(self, memory_id: str) -> None:
        """O(1) 记录一次访问，仅累积在内存，不触发任何 I/O.

        单线程 asyncio 下，与 flush 的"快照+清空"之间无 await，不会交错。
        """
        now_iso = datetime.now().isoformat()
        st = self._access_stats.get(memory_id)
        if st is None:
            self._access_stats[memory_id] = _AccessStat(count=1, last_accessed=now_iso)
        else:
            st.count += 1
            st.last_accessed = now_iso

    def pending_access_stats(self, ids: Iterable[str]) -> dict[str, tuple[int, str]]:
        """返回内存中尚未落盘的访问统计，供检索打分实时叠加.

        Returns:
            {memory_id: (count, last_accessed)}
        """
        out: dict[str, tuple[int, str]] = {}
        for mid in ids:
            st = self._access_stats.get(mid)
            if st is not None:
                out[mid] = (st.count, st.last_accessed)
        return out

    async def flush_access_stats(self) -> int:
        """把累积的访问统计批量持久化到 SQLite，并镜像到 ChromaDB.

        SQLite 为唯一事实源（保持"先 SQLite 后 Chroma"惯例）；Chroma 仅按
        逐 key 合并更新 3 个字段。滑动 TTL：非 pinned 记忆的 expires_at
        刷新为 now + ttl_days。

        Returns:
            本次落盘的记忆条数
        """
        # 快照 + 清空必须是同步对（无 await 间隔），防止并发访问交错
        stats = dict(self._access_stats)
        self._access_stats.clear()
        if not stats:
            return 0

        await self.initialize()
        new_expires = (
            datetime.now() + timedelta(days=self._settings.memory_ttl_days)
        ).isoformat()

        ids = list(stats.keys())

        # 1. SQLite：单条批量 UPDATE + RETURNING 取回 fresh 值（省去 readback 事务）
        rows: Any = []
        try:
            async with get_session() as session:
                async with session.begin():
                    # 用 CASE 表达式按 id 分支赋值，一条 SQL 搞定 N 条记录
                    la_case = case(
                        *[(MemoryModel.id == mid, st.last_accessed) for mid, st in stats.items()],
                    )
                    inc_case = case(
                        *[(MemoryModel.id == mid, st.count) for mid, st in stats.items()],
                    )
                    result = await session.execute(
                        update(MemoryModel)
                        .where(
                            MemoryModel.id.in_(ids),
                            MemoryModel.deleted_time.is_(None),
                        )
                        .values(
                            last_accessed=la_case,
                            access_count=MemoryModel.access_count + inc_case,
                            expires_at=case(
                                (MemoryModel.pinned == 0, new_expires),
                                else_=MemoryModel.expires_at,
                            ),
                        )
                        .returning(
                            MemoryModel.id,
                            MemoryModel.pinned,
                            MemoryModel.expires_at,
                            MemoryModel.last_accessed,
                            MemoryModel.access_count,
                        )
                    )
                    rows = result.fetchall()
        except Exception as e:
            logger.error("memory_flush_sqlite_failed", error=str(e))
            raise

        # 2. Chroma 镜像（update 为逐 key 合并，仅改这 3 个字段）
        #    用 asyncio.to_thread 避免同步 ChromaDB 调用阻塞事件循环
        if rows:
            try:
                chroma_ids = [r.id for r in rows]
                chroma_metas = [
                    {
                        "last_accessed": r.last_accessed,
                        "access_count": r.access_count,
                        "expires_at": "" if r.pinned else (r.expires_at or ""),
                    }
                    for r in rows
                ]
                await asyncio.to_thread(
                    self.collection.update, chroma_ids, chroma_metas
                )
            except Exception as e:
                # SQLite 已提交，仅 Chroma 落后一个周期；下次访问自动补同步
                logger.error("memory_flush_chromadb_failed", error=str(e))
                raise

        logger.info("memory_access_flushed", count=len(stats))
        return len(stats)

    async def run_periodic_flush(self) -> None:
        """后台看门狗：按 memory_sync_interval 周期 flush 访问统计并清理过期.

        先 flush 再 cleanup：刚被访问的记忆先滑动 TTL，随后才判定过期，
        避免活跃记忆被误删。两步各自独立守护，一次失败不中断循环。
        """
        while True:
            await asyncio.sleep(self._settings.memory_sync_interval)
            try:
                await self.flush_access_stats()
            except Exception as e:
                logger.error("memory_periodic_flush_failed", error=str(e))
            try:
                await self.cleanup_expired()
            except Exception as e:
                logger.error("memory_periodic_cleanup_failed", error=str(e))

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
            metadata: 附加元数据（建议包含 session_id 溯源，以及 category/confidence/type 等；
                检索不按会话过滤，session_id 仅用于来源追踪）
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
                            metadata_json=_json_dumps(meta),
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

        ChromaDB 定位为长期记忆存储，默认跨会话全库检索：session_id 仅作
        溯源元数据，不作为检索隔离维度。需要显式缩小范围时才传 where。

        Args:
            query: 查询文本
            n_results: 返回结果数量
            where: 可选过滤条件（ChromaDB metadata 过滤语法，如
                {"category": "preference"}）；默认 None 即全库检索
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
            # 在 ChromaDB 中，距离值越小，代表越相似，cosine 距离 ∈ [0,2]，线性归一化为 0-1 相似度
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
        for item in out:
            self._record_access(item["id"])
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

        职责边界：FTS5 只做精确词/整段/前缀召回，不做中文语义分词
        （分词语义由向量检索承担）。tokenize='unicode61' 把连续字符段
        （含中英混排）索引为单个 token，因此本方法：
        - 按 ASCII 词与 CJK 段分开切 token，避免中英粘连成不可命中的 token
        - 对 CJK 段及较长 ASCII 词追加前缀形式（"北京"* 可命中"北京烤鸭好吃"）

        Args:
            query: 查询文本
            n_results: 返回结果数量
            where: 可选 SQL 过滤条件（仅支持 memories 表顶层列，如
                {"category": "preference"}）；默认 None 即跨会话全库检索
        """
        # 构建 FTS5 MATCH 查询：对查询中的每个词用 OR 连接
        # FTS5 语法：双引号包裹避免特殊字符干扰
        import re

        tokens = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fa5]+", query)
        if not tokens:
            return []

        terms: list[str] = []
        for t in tokens:
            if re.search(r"[\u4e00-\u9fa5]", t):
                # CJK 段整段为单 token，精确匹配几乎不可命中；前缀可召回
                # 以该段开头的记忆（如"北京"→"北京烤鸭好吃"）
                terms.append(f'"{t}" OR "{t}"*')
            elif len(t) >= 3:
                # 较长 ASCII 词可能是中英混排段的词首（"ipv6"→"ipv6配置"）
                terms.append(f'"{t}" OR "{t}"*')
            else:
                # 短 ASCII 词精确匹配即可，前缀易引入 "we*"→"west" 类噪声
                terms.append(f'"{t}"')
        match_terms = " OR ".join(terms)
        if not match_terms:
            return []

        try:
            async with get_session() as session:
                # FTS5 MATCH 查询 + 关联 memories 表获取完整数据
                base_sql = """
                    SELECT m.id, m.content, m.metadata_json, m.session_id,
                           m.created_at, m.pinned, m.expires_at,
                           m.last_accessed, m.access_count,
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

                # FTS5 无 ORDER BY 时按 rowid/插入序返回（非相关序），
                # 必须显式按 rank(=bm25, 升序=最相关在前) 排序，否则 RRF 的 rank 输入无效
                sql = text(
                    base_sql + extra_conditions + f" ORDER BY rank LIMIT {n_results}"
                )
                result = await session.execute(sql, params)
                rows = result.fetchall()
        except Exception as e:
            logger.error("memory_keyword_search_failed", error=str(e))
            return []

        out: list[dict[str, Any]] = []
        for row in rows:
            meta = _json_loads(row.metadata_json, {})
            # metadata_json 是创建时冻结的快照，访问字段以 live 列为准覆盖
            meta["last_accessed"] = row.last_accessed
            meta["access_count"] = row.access_count
            # bm25 返回负值，越小（越负）越相关；abs/(1+abs) 映射为单调递增的
            # 0-1 相似度，与向量路径"score 越大越相似"的语义一致
            raw_rank = row.rank if row.rank is not None else 0.0
            score = abs(raw_rank) / (1.0 + abs(raw_rank))
            out.append(
                {
                    "id": row.id,
                    "content": row.content,
                    "metadata": meta,
                    "score": score,
                    "source": "keyword",
                }
            )
        for item in out:
            self._record_access(item["id"])
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
            self._record_access(memory_id)
            return {
                "id": result["ids"][0],
                "content": result["documents"][0] if result["documents"] else "",
                "metadata": result["metadatas"][0] if result["metadatas"] else {},
            }
        except Exception as e:
            logger.error("memory_get_failed", memory_id=memory_id, error=str(e))
            return None

    # ------------------------------------------------------------------
    # 列表/统计/编辑（管理页；SQLite 侧，避免 Chroma where 语法差异）
    # ------------------------------------------------------------------

    async def list_memories(
        self,
        limit: int = 50,
        offset: int = 0,
        pinned_only: bool = False,
        expired_only: bool = False,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """分页列出记忆条目（不含软删除），按创建时间倒序."""
        stmt = select(MemoryModel).where(MemoryModel.deleted_time.is_(None))
        if pinned_only:
            stmt = stmt.where(MemoryModel.pinned == 1)
        if expired_only:
            now_iso = datetime.now().isoformat()
            stmt = stmt.where(
                MemoryModel.expires_at.is_not(None),
                MemoryModel.expires_at < now_iso,
                MemoryModel.pinned == 0,
            )
        if session_id:
            stmt = stmt.where(MemoryModel.session_id == session_id)
        stmt = (
            stmt.order_by(MemoryModel.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        async with get_session() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()
        return [
            {
                "id": row.id,
                "content": row.content,
                "metadata": _json_loads(row.metadata_json, {}) or {},
                "pinned": bool(row.pinned),
                "expires_at": row.expires_at,
                "created_at": row.created_at,
                "last_accessed": row.last_accessed,
                "access_count": row.access_count,
            }
            for row in rows
        ]

    async def count_memories(self) -> dict[str, int]:
        """返回记忆统计 {total, pinned, expired, recent_week}."""
        now = datetime.now()
        now_iso = now.isoformat()
        week_ago_iso = (now - timedelta(days=7)).isoformat()
        async with get_session() as session:
            total = (
                await session.execute(
                    select(func.count(MemoryModel.id)).where(
                        MemoryModel.deleted_time.is_(None)
                    )
                )
            ).scalar_one()
            pinned = (
                await session.execute(
                    select(func.count(MemoryModel.id)).where(
                        MemoryModel.deleted_time.is_(None),
                        MemoryModel.pinned == 1,
                    )
                )
            ).scalar_one()
            expired = (
                await session.execute(
                    select(func.count(MemoryModel.id)).where(
                        MemoryModel.deleted_time.is_(None),
                        MemoryModel.expires_at.is_not(None),
                        MemoryModel.expires_at < now_iso,
                        MemoryModel.pinned == 0,
                    )
                )
            ).scalar_one()
            recent_week = (
                await session.execute(
                    select(func.count(MemoryModel.id)).where(
                        MemoryModel.deleted_time.is_(None),
                        MemoryModel.created_at >= week_ago_iso,
                    )
                )
            ).scalar_one()
        return {
            "total": int(total),
            "pinned": int(pinned),
            "expired": int(expired),
            "recent_week": int(recent_week),
        }

    async def update_memory(self, memory_id: str, content: str) -> bool:
        """更新记忆内容（双写 SQLite + ChromaDB）.

        先更新 SQLite（含 FTS5 重建索引），再更新 ChromaDB；
        ChromaDB 失败时回滚 SQLite。返回 False 表示记录不存在。
        """
        await self.initialize()
        now = datetime.now().isoformat()

        # 1. 先更新 SQLite（读回旧 content 用于回滚）
        try:
            async with get_session() as session:
                async with session.begin():
                    row = (
                        await session.execute(
                            text("SELECT content FROM memories WHERE id = :id AND deleted_time IS NULL"),
                            {"id": memory_id},
                        )
                    ).fetchone()
                    if row is None:
                        return False
                    old_content = row.content
                    await session.execute(
                        update(MemoryModel)
                        .where(MemoryModel.id == memory_id)
                        .values(content=content)
                    )
                    await session.execute(
                        text("DELETE FROM memory_fts WHERE memory_id = :memory_id"),
                        {"memory_id": memory_id},
                    )
                    await session.execute(
                        text(
                            "INSERT INTO memory_fts (content, memory_id) VALUES (:content, :memory_id)"
                        ),
                        {"content": content, "memory_id": memory_id},
                    )
        except Exception as e:
            logger.error("memory_update_sqlite_failed", memory_id=memory_id, error=str(e))
            raise

        # 2. 再更新 ChromaDB；失败则回滚 SQLite
        try:
            self.collection.update(
                ids=[memory_id],
                documents=[content],
            )
        except Exception as e:
            logger.error(
                "memory_update_chromadb_failed", memory_id=memory_id, error=str(e)
            )
            await self._rollback_update(memory_id, old_content)
            raise

        logger.info("memory_updated", memory_id=memory_id, len=len(content))
        return True

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
        # 取消固定后仍给予完整 TTL 窗口（滑动语义），而非立即过期
        ttl_days = self._settings.memory_ttl_days
        if pinned:
            expires_at = None
            chroma_expires_at = ""
        else:
            expires_at = (datetime.now() + timedelta(days=ttl_days)).isoformat()
            chroma_expires_at = expires_at
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
                        "expires_at": chroma_expires_at,
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

        先从 SQLite 查询过期 ID（索引命中），再软删 SQLite，最后删 ChromaDB；
        ChromaDB 失败时回滚 SQLite。
        """
        await self.initialize()
        now = datetime.now()
        now_iso = now.isoformat()

        # 1. 从 SQLite 查询过期 ID（利用 expires_at 索引，避免全表 ChromaDB 扫描）
        expired_ids: list[str] = []
        try:
            async with get_session() as session:
                rows = (
                    await session.execute(
                        select(MemoryModel.id).where(
                            MemoryModel.expires_at.isnot(None),
                            MemoryModel.expires_at < now_iso,
                            MemoryModel.pinned == 0,
                            MemoryModel.deleted_time.is_(None),
                        )
                    )
                ).fetchall()
                expired_ids = [r[0] for r in rows]
        except Exception as e:
            logger.error("memory_cleanup_query_failed", error=str(e))
            raise

        if not expired_ids:
            return 0

        # 2. 软删 SQLite（单条批量 UPDATE + 逐条 FTS 清理）
        try:
            async with get_session() as session:
                async with session.begin():
                    await session.execute(
                        update(MemoryModel)
                        .where(MemoryModel.id.in_(expired_ids))
                        .values(deleted_time=now_iso)
                    )
                    for mid in expired_ids:
                        await session.execute(
                            text("DELETE FROM memory_fts WHERE memory_id = :mid"),
                            {"mid": mid},
                        )
        except Exception as e:
            logger.error("memory_cleanup_sqlite_failed", error=str(e))
            raise

        # 3. 删 ChromaDB；失败则回滚 SQLite 软删除
        try:
            await asyncio.to_thread(self.collection.delete, expired_ids)
        except Exception as e:
            logger.error("memory_cleanup_chromadb_failed", error=str(e))
            await self._rollback_cleanup(expired_ids)
            raise

        logger.info("memory_cleanup", count=len(expired_ids))
        return len(expired_ids)

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
                            text(
                                "SELECT pinned, expires_at FROM memories WHERE id = :id"
                            ),
                            {"id": memory_id},
                        )
                    ).fetchone()
                    if row:
                        reverted_pinned = 0 if row.pinned else 1
                        reverted_expires = (
                            None
                            if row.pinned
                            else (row.expires_at or datetime.now().isoformat())
                        )
                        await session.execute(
                            update(MemoryModel)
                            .where(MemoryModel.id == memory_id)
                            .values(pinned=reverted_pinned, expires_at=reverted_expires)
                        )
            logger.warning("rollback_pin_succeeded", memory_id=memory_id)
        except Exception as e:
            logger.error("rollback_pin_failed", memory_id=memory_id, error=str(e))

    async def _rollback_update(self, memory_id: str, old_content: str) -> None:
        """回滚 update_memory：恢复 SQLite content + 重建 FTS5 索引."""
        try:
            async with get_session() as session:
                async with session.begin():
                    await session.execute(
                        update(MemoryModel)
                        .where(MemoryModel.id == memory_id)
                        .values(content=old_content)
                    )
                    await session.execute(
                        text("DELETE FROM memory_fts WHERE memory_id = :memory_id"),
                        {"memory_id": memory_id},
                    )
                    await session.execute(
                        text(
                            "INSERT INTO memory_fts (content, memory_id) VALUES (:content, :memory_id)"
                        ),
                        {"content": old_content, "memory_id": memory_id},
                    )
            logger.warning("rollback_update_succeeded", memory_id=memory_id)
        except Exception as e:
            logger.error("rollback_update_failed", memory_id=memory_id, error=str(e))

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
