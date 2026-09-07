"""协调关键词存储和向量存储的长期记忆服务。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from athena.config.settings import Settings
from athena.core.memory.ports import MemoryRepository, MemoryVectorStore
from athena.utils.ids import generate_time_id
from athena.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class _AccessStat:
    """表示 AccessStat 组件，封装相关状态和行为。
    """
    count: int = 1
    last_accessed: str = ""


class MemoryManager:
    """长期记忆双写应用服务。

    SQLite 是生命周期管理和关键词搜索的事实来源，Chroma 是可替换的向量索引。
    跨存储操作使用显式的补偿机制，因为两种技术之间不存在原子事务。
    """

    def __init__(
        self,
        settings: Settings,
        repository: MemoryRepository,
        vector_store: MemoryVectorStore,
    ) -> None:
        """

        参数：
            settings (Settings): 全局配置对象。
            repository (MemoryRepository): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            vector_store (MemoryVectorStore): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        self._settings = settings
        self._repository = repository
        self._vectors = vector_store
        self._initialized = False
        self._access_stats: dict[str, _AccessStat] = {}

    async def initialize(self) -> None:
        """初始化资源。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        if self._initialized:
            return
        await self._vectors.initialize()
        self._initialized = True
        logger.info("memory_manager_initialized", path=str(self._settings.chroma_path))

    def _record_access(self, memory_id: str) -> None:
        """

        参数：
            memory_id (str): 记忆记录唯一标识。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        now = datetime.now().isoformat()
        stat = self._access_stats.get(memory_id)
        if stat is None:
            self._access_stats[memory_id] = _AccessStat(last_accessed=now)
        else:
            stat.count += 1
            stat.last_accessed = now

    def record_selected_access(self, memory_ids: Iterable[str]) -> None:
        """记录最终写入上下文的记忆访问，单个请求内按 ID 去重。"""
        for memory_id in dict.fromkeys(memory_ids):
            if memory_id:
                self._record_access(memory_id)

    def pending_access_stats(self, ids: Iterable[str]) -> dict[str, tuple[int, str]]:
        """

        参数：
            ids (Iterable[str]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            dict[str, tuple[int, str]]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        return {
            memory_id: (
                self._access_stats[memory_id].count,
                self._access_stats[memory_id].last_accessed,
            )
            for memory_id in ids
            if memory_id in self._access_stats
        }

    async def flush_access_stats(self) -> int:
        """

        返回值：
            int: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        stats = dict(self._access_stats)
        self._access_stats.clear()
        if not stats:
            return 0
        expires_at = (
            datetime.now() + timedelta(days=self._settings.memory_ttl_days)
        ).isoformat()
        try:
            rows = await self._repository.flush_access_stats(stats, expires_at)
        except Exception:
            self._access_stats.update(stats)
            logger.exception("memory_flush_sqlite_failed")
            raise
        try:
            for row in rows:
                await self._vectors.update(
                    row["id"],
                    metadata={
                        "last_accessed": row["last_accessed"],
                        "access_count": row["access_count"],
                        "expires_at": (
                            "" if row["pinned"] else (row["expires_at"] or "")
                        ),
                    },
                )
        except Exception:
            logger.exception("memory_flush_chromadb_failed")
            raise
        return len(stats)

    async def run_periodic_flush(self) -> None:
        """

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        while True:
            await asyncio.sleep(self._settings.memory_sync_interval)
            try:
                await self.flush_access_stats()
            except Exception:
                logger.exception("memory_periodic_flush_failed")
            try:
                await self.cleanup_expired()
            except Exception:
                logger.exception("memory_periodic_cleanup_failed")

    async def add_memory(
        self,
        content: str,
        metadata: dict[str, Any] | None = None,
        pinned: bool = False,
    ) -> str:
        """

        参数：
            content (str): 待保存或处理的内容。
            metadata (dict[str, Any] | None): 附加元数据字典。
            pinned (bool): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            str: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        await self.initialize()
        metadata = metadata or {}
        if not metadata.get("session_id"):
            logger.warning("add_memory_missing_session_id")
        reserved = {
            "created_at",
            "last_accessed",
            "access_count",
            "pinned",
            "expires_at",
        }
        metadata = {
            key: value for key, value in metadata.items() if key not in reserved
        }
        memory_id = generate_time_id()
        now = datetime.now().isoformat()
        expires_at = (
            None
            if pinned
            else (
                datetime.now() + timedelta(days=self._settings.memory_ttl_days)
            ).isoformat()
        )
        record = {
            "id": memory_id,
            "content": content,
            "metadata": metadata,
            "pinned": pinned,
            "expires_at": expires_at,
            "created_at": now,
        }
        await self._repository.add(record)
        vector_metadata = {
            "created_at": now,
            "last_accessed": now,
            "access_count": 0,
            "pinned": pinned,
            "expires_at": expires_at or "",
            "status": "active",
            **metadata,
        }
        try:
            await self._vectors.add(memory_id, content, vector_metadata)
        except Exception:
            await self._repository.hard_delete(memory_id)
            logger.exception("memory_add_vector_failed", memory_id=memory_id)
            raise
        return memory_id

    async def search(
        self,
        query: str,
        n_results: int = 5,
        where: dict[str, Any] | None = None,
        record_access: bool = True,
    ) -> list[dict[str, Any]]:
        """执行搜索。

        参数：
            query (str): 检索或搜索文本；应为非空字符串。
            n_results (int): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            where (dict[str, Any] | None): 可选过滤条件。
            record_access (bool): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            list[dict[str, Any]]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        await self.initialize()
        try:
            results = await self._vectors.query(query, n_results, where)
        except Exception as exc:
            logger.error("memory_search_failed", error=str(exc))
            return []
        ids = results.get("ids") or []
        if not ids or not ids[0]:
            return []
        output: list[dict[str, Any]] = []
        for memory_id, document, metadata, distance in zip(
            ids[0],
            (results.get("documents") or [[]])[0],
            (results.get("metadatas") or [[]])[0],
            (results.get("distances") or [[]])[0],
        ):
            if (metadata or {}).get("status", "active") != "active":
                continue
            output.append(
                {
                    "id": memory_id,
                    "content": document,
                    "metadata": metadata or {},
                    "score": max(0.0, 1.0 - float(distance) / 2.0),
                    "source": "vector",
                }
            )
            if record_access:
                self._record_access(memory_id)
        return output

    async def keyword_search(
        self,
        query: str,
        n_results: int = 10,
        where: dict[str, Any] | None = None,
        record_access: bool = True,
    ) -> list[dict[str, Any]]:
        """

        参数：
            query (str): 检索或搜索文本；应为非空字符串。
            n_results (int): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            where (dict[str, Any] | None): 可选过滤条件。
            record_access (bool): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            list[dict[str, Any]]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        try:
            results = await self._repository.keyword_search(query, n_results, where)
        except Exception as exc:
            logger.error("memory_keyword_search_failed", error=str(exc))
            return []
        if record_access:
            for item in results:
                self._record_access(item["id"])
        return results

    async def get(self, memory_id: str) -> dict[str, Any] | None:
        """获取数据。

        参数：
            memory_id (str): 记忆记录唯一标识。

        返回值：
            dict[str, Any] | None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        await self.initialize()
        try:
            result = await self._vectors.get(memory_id)
        except Exception as exc:
            logger.error("memory_get_failed", memory_id=memory_id, error=str(exc))
            return None
        if not result or not result.get("ids"):
            return None
        self._record_access(memory_id)
        return {
            "id": result["ids"][0],
            "content": (result.get("documents") or [""])[0],
            "metadata": (result.get("metadatas") or [{}])[0],
        }

    async def list_memories(
        self,
        limit: int = 50,
        offset: int = 0,
        pinned_only: bool = False,
        expired_only: bool = False,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """

        参数：
            limit (int): 最大返回数量；应为非负整数。
            offset (int): 分页偏移量；应为非负整数。
            pinned_only (bool): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            expired_only (bool): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            session_id (str | None): 会话唯一标识。

        返回值：
            list[dict[str, Any]]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        return await self._repository.list(
            limit=limit,
            offset=offset,
            pinned_only=pinned_only,
            expired_only=expired_only,
            session_id=session_id,
        )

    async def count_memories(self) -> dict[str, int]:
        """

        返回值：
            dict[str, int]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        return await self._repository.counts()

    async def update_memory(self, memory_id: str, content: str) -> bool:
        """

        参数：
            memory_id (str): 记忆记录唯一标识。
            content (str): 待保存或处理的内容。

        返回值：
            bool: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        await self.initialize()
        previous = await self._repository.update_content(memory_id, content)
        if previous is None:
            return False
        try:
            await self._vectors.update(memory_id, content=content)
        except Exception:
            await self._repository.update_content(memory_id, previous)
            logger.exception("memory_update_vector_failed", memory_id=memory_id)
            raise
        return True

    async def supersede(self, old_memory_id: str, new_memory_id: str) -> bool:
        """Mark an active memory as superseded by a newly-created record."""
        changed = await self._repository.mark_superseded(old_memory_id, new_memory_id)
        if changed:
            await self._vectors.update(
                old_memory_id,
                metadata={"status": "superseded", "superseded_by": new_memory_id},
            )
        return changed

    async def delete(self, memory_id: str) -> None:
        """删除数据。

        参数：
            memory_id (str): 记忆记录唯一标识。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        await self.initialize()
        await self._repository.soft_delete([memory_id])
        try:
            await self._vectors.delete([memory_id])
        except Exception:
            await self._repository.restore_deleted([memory_id])
            logger.exception("memory_delete_vector_failed", memory_id=memory_id)
            raise

    async def pin(self, memory_id: str, pinned: bool = True) -> None:
        """更新固定状态。

        参数：
            memory_id (str): 记忆记录唯一标识。
            pinned (bool): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        await self.initialize()
        expires_at = (
            None
            if pinned
            else (
                datetime.now() + timedelta(days=self._settings.memory_ttl_days)
            ).isoformat()
        )
        previous = await self._repository.set_pin(memory_id, pinned, expires_at)
        if previous is None:
            return
        try:
            await self._vectors.update(
                memory_id,
                metadata={
                    "pinned": pinned,
                    "expires_at": expires_at or "",
                },
            )
        except Exception:
            await self._repository.set_pin(memory_id, previous[0], previous[1])
            logger.exception("memory_pin_vector_failed", memory_id=memory_id)
            raise

    async def cleanup_expired(self) -> int:
        """

        返回值：
            int: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        await self.initialize()
        ids = await self._repository.expired_ids(datetime.now().isoformat())
        if not ids:
            return 0
        await self._repository.soft_delete(ids)
        try:
            await self._vectors.delete(ids)
        except Exception:
            await self._repository.restore_deleted(ids)
            logger.exception("memory_cleanup_vector_failed")
            raise
        return len(ids)
