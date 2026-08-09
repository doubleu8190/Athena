"""记忆管理测试 — 访问追踪、滑动 TTL、记忆度加权.

用 FakeChromaClient 替代真实 ChromaDB（真实默认 embedding 需下载 ONNX 模型，
网络不稳定且现有测试从不触碰 Chroma）；SQLite 走真实 init_engine + FTS5。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select, update

from athena.config.settings import Settings
from athena.core.memory.memory import MemoryManager
from athena.core.memory.retrieval import HybridRetrievalManager, SearchResult
from athena.db.engine import close_engine, get_session, init_engine
from athena.db.models import MemoryModel


# ---------------------------------------------------------------------------
# 伪 Chroma 客户端（覆盖 MemoryManager 使用的全部表面，update 为逐 key 合并）
# ---------------------------------------------------------------------------

class _FakeCollection:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}

    def add(self, ids: list[str], documents: list[str], metadatas: list[Any]) -> None:
        for mid, doc, meta in zip(ids, documents, metadatas):
            self._items[mid] = {"document": doc, "metadata": meta}

    def query(
        self,
        query_texts: list[str] | None = None,
        n_results: int = 5,
        include: list[str] | None = None,
        where: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        items = list(self._items.items())
        if where:
            items = [
                (i, d)
                for i, d in items
                if all(d["metadata"].get(k) == v for k, v in where.items())
            ]
        items = items[:n_results]
        ids = [i for i, _ in items]
        docs = [d["document"] for _, d in items]
        metas = [d["metadata"] for _, d in items]
        dists = [0.0] * len(items)
        # MemoryManager.search 按单行查询读取首元素，故用嵌套列表
        return {
            "ids": [ids],
            "documents": [docs],
            "metadatas": [metas],
            "distances": [dists],
        }

    def get(
        self, ids: list[str] | None = None, include: list[str] | None = None
    ) -> dict[str, Any]:
        if ids is None:
            selected = list(self._items.items())
        else:
            selected = [(i, d) for i, d in self._items.items() if i in ids]
        return {
            "ids": [i for i, _ in selected],
            "documents": [d["document"] for _, d in selected],
            "metadatas": [d["metadata"] for _, d in selected],
        }

    def update(self, ids: list[str], metadatas: list[Any] | None = None) -> None:
        for mid, meta in zip(ids, metadatas or []):
            if mid in self._items:
                self._items[mid]["metadata"].update(meta)

    def delete(self, ids: list[str]) -> None:
        for mid in ids:
            self._items.pop(mid, None)


class _FakeChromaClient:
    def __init__(self) -> None:
        self._collection: _FakeCollection | None = None

    def get_or_create_collection(
        self, name: str, metadata: dict[str, Any] | None = None
    ) -> _FakeCollection:
        if self._collection is None:
            self._collection = _FakeCollection()
        return self._collection


# ---------------------------------------------------------------------------
# 检索测试用 stub
# ---------------------------------------------------------------------------

class _StubLLM:
    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        return AIMessage(content="expanded query")


class _StubMemory:
    def __init__(
        self,
        vector_results: list[dict[str, Any]],
        pending: dict[str, tuple[int, str]] | None = None,
    ) -> None:
        self._vector_results = vector_results
        self.pending = pending or {}

    async def search(
        self, query: str, n_results: int = 5, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return self._vector_results

    async def keyword_search(
        self, query: str, n_results: int = 10, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return []

    def pending_access_stats(self, ids: Iterable[str]) -> dict[str, tuple[int, str]]:
        return {i: self.pending[i] for i in ids if i in self.pending}


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------

def _make_settings(**overrides: Any) -> Settings:
    # 显式覆盖权重，避免测试受本机 .env（VECTOR_WEIGHT/KEYWORD_WEIGHT）影响
    defaults: dict[str, Any] = {
        "memory_ttl_days": 90,
        "memory_sync_interval": 900,
        "memory_access_window_days": 7,
        "vector_weight": 0.75,
        "keyword_weight": 0.25,
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
async def mm(tmp_path):
    await init_engine(str(tmp_path / "test.db"))
    manager = MemoryManager(
        chroma_client=_FakeChromaClient(),
        settings=_make_settings(),
    )
    await manager.initialize()
    yield manager
    await close_engine()


async def _get_row(memory_id: str) -> MemoryModel | None:
    async with get_session() as session:
        result = await session.execute(
            select(MemoryModel).where(MemoryModel.id == memory_id)
        )
        return result.scalars().first()


# ---------------------------------------------------------------------------
# 访问追踪
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reads_record_access_in_memory(mm: MemoryManager):
    mid = await mm.add_memory(content="用户偏好简洁回答", metadata={"session_id": "s1"})
    await mm.search(query="偏好", where={"session_id": "s1"})
    await mm.get(mid)
    st = mm._access_stats.get(mid)
    assert st is not None
    assert st.count == 2
    # last_accessed 必须可解析为 isoformat
    datetime.fromisoformat(st.last_accessed)


@pytest.mark.asyncio
async def test_flush_updates_sqlite_and_chroma(mm: MemoryManager):
    mid = await mm.add_memory(
        content="技术决策：采用微服务架构", metadata={"session_id": "s1"}
    )
    await mm.search(query="微服务", where={"session_id": "s1"})
    await mm.search(query="微服务", where={"session_id": "s1"})

    assert await mm.flush_access_stats() == 1
    assert mm._access_stats == {}

    row = await _get_row(mid)
    assert row is not None
    assert row.access_count == 2
    assert row.last_accessed is not None

    # Chroma 元数据同步
    meta = mm.collection._items[mid]["metadata"]
    assert meta["access_count"] == 2
    assert meta["last_accessed"] is not None
    # 非 pinned：expires_at 已滑动到 now + ttl
    assert datetime.fromisoformat(meta["expires_at"]) > datetime.now() + timedelta(days=89)


@pytest.mark.asyncio
async def test_flush_pinned_keeps_expires_at(mm: MemoryManager):
    mid = await mm.add_memory(
        content="固定记忆", metadata={"session_id": "s1"}, pinned=True
    )
    await mm.search(query="固定", where={"session_id": "s1"})
    await mm.flush_access_stats()

    row = await _get_row(mid)
    assert row is not None
    assert row.pinned == 1
    assert row.expires_at is None  # SQLite 保持 NULL
    assert row.access_count == 1

    meta = mm.collection._items[mid]["metadata"]
    assert meta["expires_at"] == ""  # Chroma 保持空串
    assert meta["access_count"] == 1


@pytest.mark.asyncio
async def test_flush_after_delete_is_safe(mm: MemoryManager):
    mid = await mm.add_memory(content="将被删除的记忆", metadata={"session_id": "s1"})
    await mm.search(query="删除", where={"session_id": "s1"})  # 记录访问
    await mm.delete(mid)
    # 已删除记忆的统计落盘不应抛异常
    assert await mm.flush_access_stats() == 1
    row = await _get_row(mid)
    assert row is not None
    assert row.deleted_time is not None  # 保持软删，未被复活
    assert mid not in mm.collection._items


@pytest.mark.asyncio
async def test_flush_empty_stats_noop(mm: MemoryManager):
    assert await mm.flush_access_stats() == 0


# ---------------------------------------------------------------------------
# 滑动 TTL 与清理
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sliding_ttl_refreshes_on_access(mm: MemoryManager):
    mid = await mm.add_memory(content="过期测试记忆", metadata={"session_id": "s1"})
    # 强制到期（模拟创建久远、TTL 已过）
    past = (datetime.now() - timedelta(days=1)).isoformat()
    async with get_session() as session:
        async with session.begin():
            await session.execute(
                update(MemoryModel)
                .where(MemoryModel.id == mid)
                .values(expires_at=past)
            )
    # 访问 → flush 滑动 TTL
    await mm.search(query="过期", where={"session_id": "s1"})
    await mm.flush_access_stats()

    row = await _get_row(mid)
    assert row is not None
    assert datetime.fromisoformat(row.expires_at) > datetime.now()
    # 已滑动的记忆不会被清理
    assert await mm.cleanup_expired() == 0
    assert await mm.get(mid) is not None


@pytest.mark.asyncio
async def test_cleanup_expired_respects_expires_at(mm: MemoryManager):
    expired_mid = await mm.add_memory(content="过期记忆", metadata={"session_id": "s1"})
    keep_mid = await mm.add_memory(content="保留记忆", metadata={"session_id": "s1"})
    past = (datetime.now() - timedelta(days=1)).isoformat()
    async with get_session() as session:
        async with session.begin():
            await session.execute(
                update(MemoryModel)
                .where(MemoryModel.id == expired_mid)
                .values(expires_at=past)
            )
    mm.collection._items[expired_mid]["metadata"]["expires_at"] = past

    assert await mm.cleanup_expired() == 1
    assert await mm.get(expired_mid) is None
    assert await mm.get(keep_mid) is not None


# ---------------------------------------------------------------------------
# keyword 路径访问字段取自 live 列（非 metadata_json 冻结值）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_keyword_search_returns_fresh_access_fields(mm: MemoryManager):
    mid = await mm.add_memory(content="weather in shanghai is humid", metadata={"session_id": "s1"})
    await mm.search(query="weather", where={"session_id": "s1"})  # 记录访问
    await mm.flush_access_stats()

    results = await mm.keyword_search(query="shanghai", where={"session_id": "s1"})
    assert results
    assert results[0]["id"] == mid
    assert results[0]["metadata"]["access_count"] == 1
    assert results[0]["metadata"]["last_accessed"] is not None


# ---------------------------------------------------------------------------
# FTS 职责边界：精确词/整段/前缀召回（不做中文语义分词，子串交由向量路）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_keyword_cjk_prefix_recall(mm: MemoryManager):
    # 旧实现（"北京" 精确词）对 "北京烤鸭好吃" 无法命中；前缀形式 "北京"* 可召回
    await mm.add_memory(content="北京烤鸭好吃", metadata={"session_id": "s1"})
    results = await mm.keyword_search(query="北京", where={"session_id": "s1"})
    assert [r["content"] for r in results] == ["北京烤鸭好吃"]


@pytest.mark.asyncio
async def test_keyword_no_substring_recall(mm: MemoryManager):
    # "烤鸭" 是 "北京烤鸭好吃" 的子串而非前缀 → FTS 不召回（边界职责：交给向量路）
    await mm.add_memory(content="北京烤鸭好吃", metadata={"session_id": "s1"})
    results = await mm.keyword_search(query="烤鸭", where={"session_id": "s1"})
    assert results == []


@pytest.mark.asyncio
async def test_keyword_mixed_run_prefix_recall(mm: MemoryManager):
    # 中英混排整段为单 token；"ipv6" 通过前缀召回 "ipv6配置完成"
    await mm.add_memory(content="ipv6配置完成", metadata={"session_id": "s1"})
    results = await mm.keyword_search(query="ipv6", where={"session_id": "s1"})
    assert [r["content"] for r in results] == ["ipv6配置完成"]


@pytest.mark.asyncio
async def test_keyword_search_orders_by_relevance_not_insertion(mm: MemoryManager):
    # 回归：FTS5 无 ORDER BY 时按 rowid/插入序返回（弱命中可能排强命中前），
    # keyword_search 必须显式 ORDER BY rank(bm25)。先插入"仅前缀命中"（弱）、
    # 后插入"精确命中"（强），断言强命中排前且 score 更高（与向量路径语义一致）。
    await mm.add_memory(content="北京烤鸭好吃", metadata={"session_id": "s1"})
    await mm.add_memory(content="北京 天气", metadata={"session_id": "s1"})
    await mm.add_memory(content="无关记忆内容", metadata={"session_id": "s1"})

    results = await mm.keyword_search(query="北京", where={"session_id": "s1"})
    contents = [r["content"] for r in results]
    assert contents[0] == "北京 天气"  # 精确命中 > 仅前缀命中（而非插入序）
    assert "北京烤鸭好吃" in contents
    # score 单调递增：相关者分数更高（修复前的反相公式会给弱命中更高分）
    assert results[0]["score"] > results[1]["score"]


# ---------------------------------------------------------------------------
# 记忆度加权
# ---------------------------------------------------------------------------

def _retrieval_manager() -> HybridRetrievalManager:
    return HybridRetrievalManager(
        llm_provider=_StubLLM(),
        memory_manager=_StubMemory(vector_results=[]),
        settings=_make_settings(),
    )


@pytest.mark.asyncio
async def test_memorability_scoring():
    mgr = _retrieval_manager()
    assert mgr._calculate_memorability({}) == pytest.approx(1.0)
    assert mgr._calculate_memorability({"access_count": 0}) == pytest.approx(1.0)

    now = datetime.now().isoformat()
    hot = mgr._calculate_memorability(
        {"access_count": 50, "last_accessed": now}
    )
    assert hot == pytest.approx(2.10, abs=0.01)

    old = (datetime.now() - timedelta(days=10)).isoformat()
    stale = mgr._calculate_memorability(
        {"access_count": 5, "last_accessed": old}
    )
    assert stale == pytest.approx(1.27, abs=0.01)


@pytest.mark.asyncio
async def test_retrieve_ranks_hot_memory_higher():
    now = datetime.now().isoformat()
    hot = {
        "id": "hot",
        "content": "hot memory",
        "metadata": {
            "created_at": now,
            "access_count": 50,
            "last_accessed": now,
        },
        "score": 1.0,
    }
    cold = {
        "id": "cold",
        "content": "cold memory",
        "metadata": {"created_at": now, "access_count": 0},
        "score": 0.9,
    }
    mgr = HybridRetrievalManager(
        llm_provider=_StubLLM(),
        memory_manager=_StubMemory(vector_results=[hot, cold], pending={}),
        settings=_make_settings(),
    )
    results = await mgr.retrieve("query")
    # 阈值修复 canary：旧 0.07 阈值下两者都会被滤掉而返回空
    assert len(results) == 2
    assert results[0].chunk_id == "hot"
    assert results[0].score > results[1].score


class _WhereCapturingMemory(_StubMemory):
    """记录每次检索传入的 where，用于断言检索不再按会话过滤."""

    def __init__(
        self,
        vector_results: list[dict[str, Any]],
        keyword_results: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(vector_results)
        self._keyword_results = keyword_results or []
        self.search_wheres: list[dict[str, Any] | None] = []
        self.keyword_wheres: list[dict[str, Any] | None] = []

    async def search(
        self, query: str, n_results: int = 5, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.search_wheres.append(where)
        return self._vector_results

    async def keyword_search(
        self, query: str, n_results: int = 10, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.keyword_wheres.append(where)
        return self._keyword_results


@pytest.mark.asyncio
async def test_retrieve_is_cross_session_without_session_filter():
    """跨会话回归：检索不再按 session_id 过滤，其它会话的记忆也可召回."""
    now = datetime.now().isoformat()
    other = {
        "id": "other",
        "content": "来自其它会话的记忆",
        "metadata": {
            "session_id": "other-sess",
            "created_at": now,
            "access_count": 0,
        },
        "score": 1.0,
    }
    mem = _WhereCapturingMemory(vector_results=[other])
    mgr = HybridRetrievalManager(
        llm_provider=_StubLLM(),
        memory_manager=mem,
        settings=_make_settings(),
    )
    results = await mgr.retrieve("query")
    # 其它会话的记忆未被过滤掉，可被召回
    assert len(results) == 1
    assert results[0].chunk_id == "other"
    assert results[0].metadata.get("session_id") == "other-sess"
    # 向量/关键词两条路径都不再携带 session 过滤条件
    assert all(w is None or "session_id" not in w for w in mem.search_wheres)
    assert all(w is None or "session_id" not in w for w in mem.keyword_wheres)


@pytest.mark.asyncio
async def test_weighted_rrf_ranks_vector_first():
    # 权重必须真正生效：同 rank 的向量命中应高于关键词命中（0.75 > 0.25）
    v = SearchResult(
        content="v", score=0.5, source="vector", metadata={}, chunk_id="v"
    )
    k = SearchResult(
        content="k", score=0.5, source="keyword", metadata={}, chunk_id="k"
    )
    mgr = _retrieval_manager()
    fused = mgr._reciprocal_rank_fusion([v], [k])
    scores = {r.chunk_id: r.score for r in fused}
    assert scores["v"] == pytest.approx(0.75 / 61)
    assert scores["k"] == pytest.approx(0.25 / 61)
    assert scores["v"] > scores["k"]

