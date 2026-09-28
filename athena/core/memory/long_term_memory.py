"""协调关键词存储和向量存储的长期记忆服务。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from athena.config.settings import Settings
from athena.core.memory.ports import MemoryRepository, MemoryVectorStore
from athena.utils.id_generation import generate_time_id
from athena.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class _AccessStat:
    """表示 AccessStat 组件，封装相关状态和行为。"""

    count: int = 1
    last_accessed_at: str = ""


class LongTermMemoryService:
    """长期记忆应用服务。

    PostgreSQL 是生命周期管理、关键词搜索和向量索引的事实来源。
    PostgreSQL 事务负责正文、生命周期字段和向量索引的一致性边界。
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
        logger.info(
            "long_term_memory_service_initialized",
            backend="pgvector",
        )

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
            self._access_stats[memory_id] = _AccessStat(last_accessed_at=now)
        else:
            stat.count += 1
            stat.last_accessed_at = now

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
                self._access_stats[memory_id].last_accessed_at,
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
        try:
            rows = await self._repository.flush_access_stats(stats)
        except Exception:
            self._access_stats.update(stats)
            logger.exception("memory_flush_postgresql_failed")
            raise
        try:
            for row in rows:
                await self._vectors.update(
                    row["id"],
                    metadata={
                        "last_accessed_at": row["last_accessed_at"],
                        "access_count": row["access_count"],
                    },
                )
        except Exception:
            logger.exception("memory_flush_pgvector_failed")
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
        input_metadata = dict(metadata or {})
        if not input_metadata.get("session_id"):
            logger.warning("add_memory_missing_session_id")
        validity_status = str(input_metadata.get("validity_status", "valid"))
        if validity_status not in {"valid", "uncertain", "invalid"}:
            raise ValueError("validity_status must be valid, uncertain, or invalid")
        valid_until = input_metadata.get("valid_until")
        if valid_until is not None:
            try:
                datetime.fromisoformat(str(valid_until))
            except ValueError as exc:
                raise ValueError("valid_until must be an ISO-8601 datetime") from exc
        revision_of = input_metadata.get("revision_of")
        logical_memory_id = str(
            input_metadata.get("logical_memory_id") or generate_time_id()
        )
        revision = int(input_metadata.get("revision", 1))
        last_observed_at = (
            input_metadata.get("last_observed_at") or datetime.now().isoformat()
        )
        reserved = {
            "created_at",
            "last_accessed_at",
            "access_count",
            "pinned",
            "expires_at",
            "status",
            "last_observed_at",
            "validity_status",
            "valid_until",
            "revision_of",
            "revision",
            "logical_memory_id",
            "source_turn_id",
            "superseded_by",
            "superseded_at",
        }
        metadata = {
            key: value for key, value in input_metadata.items() if key not in reserved
        }
        memory_id = generate_time_id()
        now = datetime.now().isoformat()
        metadata.update(
            {
                "source_turn_id": input_metadata.get("source_turn_id"),
                "validity_status": validity_status,
                "valid_until": valid_until,
                "revision_of": revision_of,
                "revision": revision,
                "logical_memory_id": logical_memory_id,
                "last_observed_at": last_observed_at,
            }
        )
        expires_at = (
            None
            if pinned
            else (
                datetime.now() + timedelta(days=self._settings.memory_ttl_days)
            ).isoformat()
        )
        record = {
            "id": memory_id,
            "logical_memory_id": logical_memory_id,
            "content": content,
            "metadata": metadata,
            "source_turn_id": input_metadata.get("source_turn_id"),
            "pinned": pinned,
            "expires_at": expires_at,
            "created_at": now,
        }
        await self._repository.add(record)
        vector_metadata = {
            "created_at": now,
            "last_accessed_at": now,
            "access_count": 0,
            "pinned": pinned,
            "expires_at": expires_at or "",
            "last_observed_at": last_observed_at,
            "validity_status": validity_status,
            "valid_until": valid_until or "",
            "revision_of": revision_of or "",
            "revision": revision,
            "logical_memory_id": logical_memory_id,
            "status": "active",
            **{key: value for key, value in metadata.items() if value is not None},
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
        record_access: bool = False,
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
            result_metadata = metadata or {}
            if result_metadata.get("status", "active") != "active":
                continue
            if result_metadata.get("validity_status", "valid") == "invalid":
                continue
            valid_until = result_metadata.get("valid_until")
            if valid_until and valid_until < datetime.now().isoformat():
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
        record_access: bool = False,
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

    async def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        """按 ID 从向量索引读取一条长期记忆。

        参数：
            memory_id: 长期记忆唯一标识。

        返回值：
            找到时返回正文和元数据；记录不存在或向量索引读取失败时返回 ``None``。
            管理页面读取不会改变记忆热度。

        异常：
            不主动抛出业务异常。
        """
        await self.initialize()
        try:
            result = await self._vectors.get(memory_id)
        except Exception as exc:
            logger.error("memory_get_failed", memory_id=memory_id, error=str(exc))
            return None
        if not result or not result.get("ids"):
            return None
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

    async def list_memory_revisions(self, memory_id: str) -> list[dict[str, Any]]:
        """读取一条逻辑记忆的不可变 revision 历史。

        参数：
            memory_id: 任意 revision ID 或逻辑记忆 ID。

        返回值：
            按 revision 序号升序排列的历史记录。

        异常：
            PostgreSQL 读取失败时向上抛出异常。
        """
        return await self._repository.list_revisions(memory_id)

    async def update_memory(self, memory_id: str, content: str) -> str | None:
        """

        参数：
            memory_id (str): 记忆记录唯一标识。
            content (str): 待保存或处理的内容。

        返回值：
            str | None: 新修订版 ID；目标不存在或已不是活跃版本时返回 None。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        return await self.revise_memory(memory_id, content)

    async def revise_memory(
        self,
        memory_id: str,
        content: str,
        *,
        metadata_overrides: dict[str, Any] | None = None,
    ) -> str | None:
        """以不可变修订版替换一条活跃记忆。

        参数：
            memory_id：待替代的活跃记忆 ID。
            content：新修订版的记忆正文。
            metadata_overrides：本次观测提供的来源、分类和置信度等元数据。

        返回：
            新修订版 ID；旧记录不存在或已失效时返回 None。

        异常：
            双写或版本切换失败时向上抛出异常，并清理新建记录。
        """
        await self.initialize()
        previous = await self._repository.get_active_memory(memory_id)
        if previous is None:
            return None
        old_metadata = dict(previous["metadata"])
        overrides = metadata_overrides or {}
        revision = int(old_metadata.get("revision", 1)) + 1
        revision_metadata = {
            key: old_metadata[key]
            for key in ("session_id", "type", "category", "confidence", "source")
            if old_metadata.get(key) is not None
        }
        revision_metadata.update(overrides)
        revision_metadata.update(
            {
                "revision_of": memory_id,
                "revision": revision,
                # 只有新的事实观测才更新该时间；纯访问不会影响它。
                "last_observed_at": datetime.now().isoformat(),
                "validity_status": overrides.get(
                    "validity_status", old_metadata.get("validity_status", "valid")
                ),
                "valid_until": overrides.get(
                    "valid_until", old_metadata.get("valid_until") or None
                ),
            }
        )
        revision_metadata["logical_memory_id"] = str(
            old_metadata.get("logical_memory_id") or memory_id
        )
        valid_until = revision_metadata.get("valid_until")
        if valid_until:
            try:
                datetime.fromisoformat(str(valid_until))
            except ValueError as exc:
                raise ValueError("valid_until must be an ISO-8601 datetime") from exc
        new_memory_id = await self.add_memory(
            content=content,
            metadata=revision_metadata,
            pinned=bool(previous["pinned"]),
        )
        try:
            if not await self.supersede_memory(memory_id, new_memory_id):
                await self.delete_memory(new_memory_id)
                return None
        except Exception:
            await self.delete_memory(new_memory_id)
            raise
        return new_memory_id

    async def set_memory_validity(
        self,
        memory_id: str,
        validity_status: str,
        *,
        valid_until: str | None = None,
    ) -> bool:
        """更新当前记忆的事实有效性，不改变访问热度或保留期限。

        参数：
            memory_id：目标活跃记忆 ID。
            validity_status：``valid``、``uncertain`` 或 ``invalid``。
            valid_until：事实有效截止时间，必须是 ISO-8601 时间或 None。

        返回：
            记录成功更新时返回 True；目标不存在或不是当前版本时返回 False。

        异常：
            PostgreSQL 或向量同步失败时向上抛出异常，并尽力恢复旧状态。
        """
        if validity_status not in {"valid", "uncertain", "invalid"}:
            raise ValueError("validity_status must be valid, uncertain, or invalid")
        if valid_until is not None:
            try:
                datetime.fromisoformat(valid_until)
            except ValueError as exc:
                raise ValueError("valid_until must be an ISO-8601 datetime") from exc
        observed_at = datetime.now().isoformat()
        previous = await self._repository.set_validity(
            memory_id,
            validity_status,
            valid_until,
            observed_at,
        )
        if previous is None:
            return False
        try:
            await self._vectors.update(
                memory_id,
                metadata={
                    "validity_status": validity_status,
                    "valid_until": valid_until or "",
                    "last_observed_at": observed_at,
                },
            )
        except Exception:
            await self._repository.set_validity(
                memory_id,
                previous["validity_status"],
                previous["valid_until"],
                previous["last_observed_at"],
            )
            logger.exception("memory_validity_vector_failed", memory_id=memory_id)
            raise
        return True

    async def supersede_memory(self, old_memory_id: str, new_memory_id: str) -> bool:
        """将旧记忆标记为被新记忆替代。

        参数：
            old_memory_id: 要归档的旧记忆 ID。
            new_memory_id: 替代旧记忆的新记录 ID。

        返回值：
            成功更新旧记录时返回 ``True``，旧记录不存在时返回 ``False``。

        异常：
            PostgreSQL 或向量索引更新失败时向上抛出异常。
        """
        changed = await self._repository.mark_superseded(old_memory_id, new_memory_id)
        if changed:
            try:
                await self._vectors.update(
                    old_memory_id,
                    metadata={"status": "superseded"},
                )
            except Exception:
                await self._repository.restore_active(old_memory_id)
                raise
        return changed

    async def add_memory_relation(
        self, source_memory_id: str, target_memory_id: str, relation_type: str
    ) -> None:
        """保存两条长期记忆之间的语义关系。

        参数：
            source_memory_id: 关系起点的记忆 ID。
            target_memory_id: 关系终点的记忆 ID。
            relation_type: 关系类型，例如 ``supports`` 或 ``contradicts``。

        返回值：
            无。

        异常：
            关系写入失败时向上抛出异常。
        """
        await self._repository.add_relation(
            source_memory_id, target_memory_id, relation_type
        )

    async def delete_memory(self, memory_id: str) -> None:
        """软删除长期记忆，并同步移除其向量索引。

        参数：
            memory_id: 要删除的长期记忆 ID。

        返回值：
            无。

        异常：
            向量索引删除失败时恢复 PostgreSQL 软删除并向上抛出异常。
        """
        await self.initialize()
        await self._repository.soft_delete([memory_id])
        try:
            await self._vectors.delete([memory_id])
        except Exception:
            await self._repository.restore_deleted([memory_id])
            logger.exception("memory_delete_vector_failed", memory_id=memory_id)
            raise

    async def set_memory_pinned(self, memory_id: str, pinned: bool = True) -> None:
        """设置长期记忆的固定状态和对应过期时间。

        参数：
            memory_id: 要更新的长期记忆 ID。
            pinned: ``True`` 时固定且不设过期时间，``False`` 时恢复 TTL。

        返回值：
            无；记录不存在时不做修改。

        异常：
            向量索引更新失败时恢复 PostgreSQL 中的固定状态并向上抛出异常。
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
