"""知识库分片读取、全文检索与 pgvector 语义检索的 SQLAlchemy 子仓储。"""

from __future__ import annotations

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.knowledge._shared import (
    _active_chunk_clauses,
    _join_active_chunks,
    _locator,
    _personal_privacy_guard,
    _rows_to_search_hits,
    _select_keyword_tokens,
    _tsquery_token,
    _visible_chunk_rows_query,
)
from adapter.knowledge.model import (
    KnowledgeBaseModel,
    KnowledgeChunkModel,
    KnowledgeDocumentModel,
    KnowledgeVersionModel,
)
from domain.knowledge.model import SearchHit


class KnowledgeSearchStore:
    """负责知识分片的单条读取、关键词召回、向量召回与活跃版本校验。"""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get_chunk(self, owner_id: str, base_id: str, chunk_id: str) -> SearchHit | None:
        """按知识库范围读取单个分片的正文与定位信息，跳过向量和大索引列。"""
        async with self._sessions() as session:
            # 仅显式投影正文、定位器与标题标量列，避免加载 1024 维 embedding 与 tsvector 大列。
            row = await session.execute(
                select(
                    KnowledgeChunkModel.id,
                    KnowledgeChunkModel.document_id,
                    KnowledgeChunkModel.version_id,
                    KnowledgeChunkModel.content,
                    KnowledgeChunkModel.locator,
                    KnowledgeDocumentModel.title,
                )
                .join(
                    KnowledgeVersionModel,
                    KnowledgeVersionModel.id == KnowledgeChunkModel.version_id,
                )
                .join(
                    KnowledgeDocumentModel,
                    KnowledgeDocumentModel.id == KnowledgeVersionModel.document_id,
                )
                .join(
                    KnowledgeBaseModel,
                    KnowledgeBaseModel.id == KnowledgeDocumentModel.knowledge_base_id,
                )
                .where(
                    KnowledgeChunkModel.id == chunk_id,
                    KnowledgeDocumentModel.knowledge_base_id == base_id,
                    KnowledgeDocumentModel.deleted_time.is_(None),
                    KnowledgeBaseModel.deleted_time.is_(None),
                    _personal_privacy_guard(owner_id),
                )
            )
            item = row.one_or_none()
            if item is None:
                return None
            cid, document_id, version_id, content, locator, title = item
            return SearchHit(
                cid,
                document_id,
                version_id,
                title,
                content,
                (_locator(locator),),
                (),
                0.0,
                knowledge_base_id=base_id,
            )

    async def search_keyword(
        self, owner_id: str, base_ids: list[str], tokens: tuple[str, ...], limit: int
    ) -> tuple[SearchHit, ...]:
        """执行基于 GIN tsvector 的受控关键词检索并按相关性排序。"""
        selected_tokens = _select_keyword_tokens(tokens)
        if not selected_tokens:
            return ()
        tsquery = func.to_tsquery(
            "simple", " | ".join(_tsquery_token(token) for token in selected_tokens)
        )
        # 显式标量列投影避免从数据库回传 1024 维 embedding 与 search_document 大列。
        query = (
            _visible_chunk_rows_query(owner_id, base_ids)
            .where(KnowledgeChunkModel.search_document.op("@@")(tsquery))
            .order_by(func.ts_rank_cd(KnowledgeChunkModel.search_document, tsquery).desc())
            .limit(min(limit, 40))
        )
        async with self._sessions() as session:
            rows = await session.execute(query)
            return _rows_to_search_hits(rows, "keyword")

    async def search_semantic(
        self,
        owner_id: str,
        base_ids: list[str],
        vector: list[float],
        limit: int,
        profile_id: str | None = None,
        *,
        min_score: float = 0.0,
    ) -> tuple[SearchHit, ...]:
        """在事务局部开启 pgvector HNSW 迭代扫描并执行余弦距离语义检索。"""
        distance = KnowledgeChunkModel.embedding.cosine_distance(vector)
        # 显式标量列投影仅读取展示与定位所需字段，消除 4KB/行的向量列传输与反序列化开销。
        query = _visible_chunk_rows_query(owner_id, base_ids).where(
            KnowledgeChunkModel.embedding.is_not(None)
        )
        if profile_id is not None:
            query = query.where(KnowledgeVersionModel.profile_id == profile_id)
        if min_score > 0.0:
            # 余弦相似度 = 1 - cosine_distance，过滤相关性低于阈值的噪声片段。
            query = query.where(distance <= (1.0 - min_score))
        query = query.order_by(distance).limit(min(limit, 40))
        async with self._sessions() as session:
            # 在 PostgreSQL 事务局部开启 pgvector HNSW 迭代扫描，
            # 避免带权限与活动版本过滤时因默认 ef_search 截断导致近邻召回不足。
            async with session.begin():
                await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
                rows = list(await session.execute(query))
            return _rows_to_search_hits(rows, "semantic")

    async def has_incompatible_profile(
        self, owner_id: str, base_ids: list[str], profile_id: str
    ) -> bool:
        """检查目标知识库集合中是否存在使用不同嵌入 profile 的活跃分片。"""
        query = (
            _join_active_chunks(select(KnowledgeChunkModel.id).select_from(KnowledgeChunkModel))
            .where(
                *_active_chunk_clauses(owner_id, base_ids=base_ids),
                KnowledgeVersionModel.profile_id != profile_id,
                KnowledgeChunkModel.embedding.is_not(None),
            )
            .limit(1)
        )
        async with self._sessions() as session:
            return await session.scalar(query) is not None

    async def filter_active_hits(
        self, owner_id: str, hits: tuple[SearchHit, ...]
    ) -> tuple[SearchHit, ...]:
        """仅保留当前仍属于文档活动版本且未软删除的命中分片。"""
        if not hits:
            return ()
        ids = [hit.chunk_id for hit in hits]
        async with self._sessions() as session:
            rows = await session.scalars(
                _join_active_chunks(select(KnowledgeChunkModel.id)).where(
                    KnowledgeChunkModel.id.in_(ids),
                    *_active_chunk_clauses(owner_id),
                )
            )
            active = set(rows)
        return tuple(hit for hit in hits if hit.chunk_id in active)
