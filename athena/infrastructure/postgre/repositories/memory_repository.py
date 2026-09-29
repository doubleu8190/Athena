"""长期记忆记录的 PostgreSQL 仓库。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, func, insert, select, text, update

from athena.infrastructure.postgre.engine import (
    get_session,
)
from athena.infrastructure.postgre.models import MemoryModel
from .repository_utils import _json_dumps, _json_loads

_SEMANTIC_KEYS = frozenset({"session_id", "type", "category", "confidence", "source"})
_SYSTEM_KEYS = frozenset(
    {
        "logical_memory_id",
        "created_at",
        "last_accessed_at",
        "access_count",
        "status",
        "expires_at",
        "last_observed_at",
        "validity_status",
        "valid_until",
        "revision",
        "source_turn_id",
    }
)
_FILTER_COLUMNS = frozenset(
    {
        "session_id",
        "type",
        "category",
        "confidence",
        "source",
        "validity_status",
    }
)
_FILTER_COLUMN_MAP = {
    "type": "memory_type",
    "source": "source_kind",
}


def _metadata(row: Any) -> dict[str, Any]:
    """

    参数：
        row (Any): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

    返回值：
        dict[str, Any]: 返回该方法声明类型的业务结果，内容由方法职责确定。

    异常：
        异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
    """
    # JSON 只承载用户扩展字段。过滤旧版本中误写入的系统字段，并在最后
    # 叠加结构化列，确保列是唯一事实来源，历史 JSON 不能覆盖 live 值。
    raw_metadata = _json_loads(row.metadata_json, {})
    if not isinstance(raw_metadata, dict):
        raw_metadata = {}
    metadata: dict[str, Any] = {
        key: value
        for key, value in raw_metadata.items()
        if key not in _SEMANTIC_KEYS and key not in _SYSTEM_KEYS
    }
    metadata.update(
        {
            "logical_memory_id": row.logical_memory_id,
            "created_at": row.created_at,
            "last_accessed_at": row.last_accessed_at,
            "access_count": row.access_count,
            "expires_at": row.expires_at or "",
            "last_observed_at": row.last_observed_at or "",
            "validity_status": row.validity_status or "valid",
            "valid_until": row.valid_until or "",
            "revision": row.revision,
            "session_id": row.session_id,
            "status": getattr(row, "status", "active") or "active",
        }
    )
    for key, column in {
        "type": "memory_type",
        "category": "category",
        "confidence": "confidence",
        "source": "source_kind",
    }.items():
        value = getattr(row, column)
        if value is not None:
            metadata[key] = value
    if row.source_turn_id is not None:
        metadata["source_turn_id"] = row.source_turn_id
    return metadata


class PostgresMemoryRepository:
    """PostgreSQL 长期记忆生命周期与原生 FTS 关键词检索仓库。"""

    async def add(self, record: dict[str, Any]) -> None:
        """添加数据。

        参数：
            record (dict[str, Any]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        metadata = record["metadata"]
        extras = {
            key: value
            for key, value in metadata.items()
            if key not in _SEMANTIC_KEYS and key not in _SYSTEM_KEYS
        }
        async with get_session() as session:
            async with session.begin():
                if await session.get(MemoryModel, record["id"]) is not None:
                    return
                await session.execute(
                    insert(MemoryModel).values(
                        id=record["id"],
                        logical_memory_id=record.get("logical_memory_id", record["id"]),
                        session_id=metadata.get("session_id", ""),
                        content=record["content"],
                        metadata_json=_json_dumps(extras),
                        expires_at=record["expires_at"],
                        created_at=record["created_at"],
                        last_accessed_at=record["created_at"],
                        access_count=0,
                        memory_type=metadata.get("type"),
                        category=metadata.get("category"),
                        confidence=metadata.get("confidence"),
                        source_kind=metadata.get("source"),
                        source_turn_id=record.get(
                            "source_turn_id", metadata.get("source_turn_id")
                        ),
                        last_observed_at=metadata.get(
                            "last_observed_at", record["created_at"]
                        ),
                        validity_status=metadata.get("validity_status", "valid"),
                        valid_until=metadata.get("valid_until"),
                        revision=int(metadata.get("revision", 1)),
                    )
                )

    async def flush_access_stats(self, stats: dict[str, Any]) -> list[dict[str, Any]]:
        """

        参数：
            stats (dict[str, Any]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            list[dict[str, Any]]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        if not stats:
            return []
        ids = list(stats)
        async with get_session() as session:
            async with session.begin():
                result = await session.execute(
                    update(MemoryModel)
                    .where(MemoryModel.id.in_(ids), MemoryModel.deleted_time.is_(None))
                    .values(
                        last_accessed_at=case(
                            *[
                                (MemoryModel.id == mid, value.last_accessed_at)
                                for mid, value in stats.items()
                            ]
                        ),
                        access_count=MemoryModel.access_count
                        + case(
                            *[
                                (MemoryModel.id == mid, value.count)
                                for mid, value in stats.items()
                            ]
                        ),
                    )
                    .returning(
                        MemoryModel.id,
                        MemoryModel.expires_at,
                        MemoryModel.last_accessed_at,
                        MemoryModel.access_count,
                    )
                )
                return [
                    {
                        "id": row.id,
                        "expires_at": row.expires_at,
                        "last_accessed_at": row.last_accessed_at,
                        "access_count": row.access_count,
                    }
                    for row in result.fetchall()
                ]

    async def keyword_search(
        self, query: str, limit: int, where: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """

        参数：
            query (str): 检索或搜索文本；应为非空字符串。
            limit (int): 最大返回数量；应为非负整数。
            where (dict[str, Any] | None): 可选过滤条件。

        返回值：
            list[dict[str, Any]]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        if not query.strip():
            return []

        sql = """
            SELECT m.id, m.content, m.metadata_json, m.session_id,
                   m.created_at, m.expires_at, m.last_accessed_at,
                   m.access_count, m.memory_type, m.category, m.confidence,
                   m.source_kind, m.status, m.last_observed_at,
                   m.source_turn_id,
                   m.validity_status, m.valid_until,
                   m.revision, m.logical_memory_id,
                   ts_rank_cd(m.content_fts, websearch_to_tsquery('chinese', :query)) AS fts_score
            FROM memories m
            WHERE m.deleted_time IS NULL AND m.status = 'active'
              AND m.validity_status != 'invalid'
              AND (m.valid_until IS NULL OR m.valid_until >= :now)
              AND m.content_fts @@ websearch_to_tsquery('chinese', :query)
        """
        params: dict[str, Any] = {"now": datetime.now().isoformat(), "query": query}
        for key, value in (where or {}).items():
            if key not in _FILTER_COLUMNS:
                raise ValueError(f"Unsupported memory filter: {key}")
            sql += f" AND m.{_FILTER_COLUMN_MAP.get(key, key)} = :where_{key}"
            params[f"where_{key}"] = value
        sql += " ORDER BY fts_score DESC, m.created_at DESC LIMIT :limit"
        params["limit"] = limit
        async with get_session() as session:
            rows = (await session.execute(text(sql), params)).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            results.append(
                {
                    "id": row.id,
                    "content": row.content,
                    "metadata": _metadata(row),
                    "score": float(row.fts_score),
                }
            )
        return results

    async def list(self, **filters: Any) -> list[dict[str, Any]]:
        """列出数据。

        参数：
            filters (Any): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            list[dict[str, Any]]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        stmt = select(MemoryModel).where(MemoryModel.deleted_time.is_(None))
        if not filters.get("include_superseded"):
            stmt = stmt.where(MemoryModel.status == "active")
        if filters.get("expired_only"):
            stmt = stmt.where(
                MemoryModel.expires_at.is_not(None),
                MemoryModel.expires_at < datetime.now().isoformat(),
            )
        if filters.get("session_id"):
            stmt = stmt.where(MemoryModel.session_id == filters["session_id"])
        stmt = (
            stmt.order_by(MemoryModel.created_at.desc())
            .offset(filters.get("offset", 0))
            .limit(filters.get("limit", 50))
        )
        async with get_session() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": row.id,
                "content": row.content,
                "metadata": _metadata(row),
                "expires_at": row.expires_at,
                "created_at": row.created_at,
                "last_accessed_at": row.last_accessed_at,
                "access_count": row.access_count,
                "validity_status": row.validity_status,
                "valid_until": row.valid_until,
                "revision": row.revision,
            }
            for row in rows
        ]

    async def counts(self) -> dict[str, int]:
        """统计数量。

        返回值：
            dict[str, int]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        now = datetime.now()
        conditions = {
            "total": [MemoryModel.deleted_time.is_(None)],
            "expired": [
                MemoryModel.deleted_time.is_(None),
                MemoryModel.expires_at.is_not(None),
                MemoryModel.expires_at < now.isoformat(),
            ],
            "recent_week": [
                MemoryModel.deleted_time.is_(None),
                MemoryModel.created_at >= (now - timedelta(days=7)).isoformat(),
            ],
        }
        async with get_session() as session:
            result: dict[str, int] = {}
            for name, clauses in conditions.items():
                value = (
                    await session.execute(
                        select(func.count(MemoryModel.id)).where(*clauses)
                    )
                ).scalar_one()
                result[name] = int(value)
        return result

    async def get_active_memory(self, memory_id: str) -> dict[str, Any] | None:
        """读取一条可被修订的活跃记忆及其结构化元数据。

        参数：
            memory_id：目标记忆 ID。

        返回：
            活跃记录存在时返回内容和元数据；否则返回 None。

        异常：
            PostgreSQL 查询失败时向上抛出异常。
        """
        async with get_session() as session:
            row = (
                await session.execute(
                    select(MemoryModel).where(
                        MemoryModel.id == memory_id,
                        MemoryModel.status == "active",
                        MemoryModel.deleted_time.is_(None),
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "id": row.id,
            "logical_memory_id": row.logical_memory_id,
            "content": row.content,
            "metadata": _metadata(row),
        }

    async def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        async with get_session() as session:
            row = await session.get(MemoryModel, memory_id)
        if row is None or row.deleted_time is not None:
            return None
        return {
            "id": row.id,
            "logical_memory_id": row.logical_memory_id,
            "content": row.content,
            "status": row.status,
            "metadata": _metadata(row),
        }

    async def list_revisions(self, memory_id: str) -> list[dict[str, Any]]:
        """Fallback revision listing for isolated PostgreSQL-only tests.

        The production runtime uses Neo4j for revision traversal. This query
        keeps the repository useful for local fakes and transitional callers.
        """
        async with get_session() as session:
            target = await session.get(MemoryModel, memory_id)
            logical_id = target.logical_memory_id if target is not None else memory_id
            rows = (
                await session.execute(
                    select(MemoryModel)
                    .where(
                        MemoryModel.logical_memory_id == logical_id,
                        MemoryModel.deleted_time.is_(None),
                    )
                    .order_by(MemoryModel.revision.asc(), MemoryModel.created_at.asc())
                )
            ).scalars().all()
        return [
            {
                "id": row.id,
                "logical_memory_id": row.logical_memory_id,
                "content": row.content,
                "metadata": _metadata(row),
                "revision": row.revision,
                "status": row.status,
                "created_at": row.created_at,
            }
            for row in rows
        ]

    async def mark_superseded(self, old_id: str, new_id: str) -> bool:
        async with get_session() as session:
            async with session.begin():
                row = await session.get(MemoryModel, old_id)
                if row is None or row.deleted_time is not None:
                    return False
                if row.status == "superseded":
                    return True
                if row.status != "active":
                    return False
                row.status = "superseded"
                return True

    async def restore_active(self, memory_id: str) -> None:
        """回滚 supersede 后的旧记忆状态并恢复关键词索引。

        参数：
            memory_id：需要重新激活的旧版本 ID。

        返回：
            None。

        异常：
            PostgreSQL 更新失败时向上抛出异常。
        """
        async with get_session() as session:
            async with session.begin():
                row = await session.get(MemoryModel, memory_id)
                if row is None:
                    return
                row.status = "active"

    async def set_validity(
        self,
        memory_id: str,
        validity_status: str,
        valid_until: str | None,
        last_observed_at: str | None,
    ) -> dict[str, Any] | None:
        """更新活跃记忆的事实有效性并同步 FTS 索引。

        参数：
            memory_id：目标活跃记忆 ID。
            validity_status：事实有效性状态。
            valid_until：事实有效截止时间。
            last_observed_at：本次事实观测时间。

        返回：
            更新前的有效性字段；记录不存在时返回 None。

        异常：
            PostgreSQL 事务失败时向上抛出异常。
        """
        async with get_session() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(MemoryModel).where(
                            MemoryModel.id == memory_id,
                            MemoryModel.status == "active",
                            MemoryModel.deleted_time.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    return None
                previous = {
                    "validity_status": row.validity_status,
                    "valid_until": row.valid_until,
                    "last_observed_at": row.last_observed_at,
                }
                row.validity_status = validity_status
                row.valid_until = valid_until
                row.last_observed_at = last_observed_at
                return previous

    async def soft_delete(self, memory_ids: list[str]) -> None:
        """

        参数：
            memory_ids (list[str]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        if not memory_ids:
            return
        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    update(MemoryModel)
                    .where(MemoryModel.id.in_(memory_ids))
                    .values(deleted_time=datetime.now().isoformat())
                )

    async def expired_ids(self, now_iso: str) -> list[str]:
        """

        参数：
            now_iso (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            list[str]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(MemoryModel.id).where(
                        MemoryModel.expires_at.is_not(None),
                        MemoryModel.expires_at < now_iso,
                        MemoryModel.deleted_time.is_(None),
                    )
                )
            ).fetchall()
        return [row[0] for row in rows]

    async def hard_delete(self, memory_id: str) -> None:
        """

        参数：
            memory_id (str): 记忆记录唯一标识。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    text("DELETE FROM memories WHERE id = :id"), {"id": memory_id}
                )

    async def restore_deleted(self, memory_ids: list[str]) -> None:
        """

        参数：
            memory_ids (list[str]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        async with get_session() as session:
            async with session.begin():
                for memory_id in memory_ids:
                    row = await session.get(MemoryModel, memory_id)
                    if row is None:
                        continue
                    row.deleted_time = None
