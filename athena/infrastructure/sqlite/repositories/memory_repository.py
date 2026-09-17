"""长期记忆记录的 SQLite/FTS5 仓库。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, case, func, insert, select, text, update

from athena.infrastructure.sqlite.engine import (
    get_memory_database_session,
)
from athena.infrastructure.sqlite.models import MemoryModel
from .repository_utils import _json_dumps, _json_loads

_SEMANTIC_KEYS = frozenset({"session_id", "type", "category", "confidence", "source"})
_SYSTEM_KEYS = frozenset(
    {
        "created_at",
        "last_accessed",
        "access_count",
        "pinned",
        "status",
        "expires_at",
        "last_observed_at",
        "validity_status",
        "valid_until",
        "revision_of",
        "revision",
    }
)
_FILTER_COLUMNS = frozenset(
    {
        "session_id",
        "type",
        "category",
        "confidence",
        "source",
        "pinned",
        "validity_status",
    }
)


def _metadata(row: Any) -> dict[str, Any]:
    """

    参数：
        row (Any): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

    返回值：
        dict[str, Any]: 返回该方法声明类型的业务结果，内容由方法职责确定。

    异常：
        异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
    """
    metadata: dict[str, Any] = {
        "created_at": row.created_at,
        "last_accessed": row.last_accessed,
        "access_count": row.access_count,
        "pinned": bool(row.pinned),
        "expires_at": row.expires_at or "",
        "last_observed_at": row.last_observed_at or "",
        "validity_status": row.validity_status or "valid",
        "valid_until": row.valid_until or "",
        "revision_of": row.revision_of or "",
        "revision": row.revision,
        "session_id": row.session_id,
        "status": getattr(row, "status", "active") or "active",
    }
    for key in ("type", "category", "confidence", "source"):
        value = getattr(row, key)
        if value is not None:
            metadata[key] = value
    metadata.update(_json_loads(row.metadata_json, {}))
    return metadata


class SQLiteMemoryRepository:
    """SQLite 长期记忆生命周期与 FTS5 关键词检索仓库。"""
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
        async with get_memory_database_session() as session:
            async with session.begin():
                await session.execute(
                    insert(MemoryModel).values(
                        id=record["id"],
                        session_id=metadata.get("session_id", ""),
                        content=record["content"],
                        metadata_json=_json_dumps(extras),
                        pinned=1 if record["pinned"] else 0,
                        expires_at=record["expires_at"],
                        created_at=record["created_at"],
                        last_accessed=record["created_at"],
                        access_count=0,
                        type=metadata.get("type"),
                        category=metadata.get("category"),
                        confidence=metadata.get("confidence"),
                        source=metadata.get("source"),
                        source_turn_id=metadata.get("source_turn_id"),
                        last_observed_at=metadata.get(
                            "last_observed_at", record["created_at"]
                        ),
                        validity_status=metadata.get("validity_status", "valid"),
                        valid_until=metadata.get("valid_until"),
                        revision_of=metadata.get("revision_of"),
                        revision=int(metadata.get("revision", 1)),
                    )
                )
                await session.execute(
                    text(
                        "INSERT INTO memory_fts (content, memory_id) VALUES (:content, :id)"
                    ),
                    {"content": record["content"], "id": record["id"]},
                )

    async def clear_all(self) -> int:
        """清空记忆库中的记录、关系、全文索引和处理任务。

        参数：
            无。

        返回：
            删除前的记忆记录数量。

        异常：
            SQLite 删除操作失败时向上抛出异常，事务会自动回滚。
        """
        async with get_memory_database_session() as session:
            async with session.begin():
                memory_count = (
                    await session.execute(select(func.count(MemoryModel.id)))
                ).scalar_one()
                await session.execute(text("DELETE FROM memory_relations"))
                await session.execute(text("DELETE FROM memory_fts"))
                await session.execute(text("DELETE FROM memories"))
                await session.execute(text("DELETE FROM memory_processing_jobs"))
        return int(memory_count)

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
        async with get_memory_database_session() as session:
            async with session.begin():
                result = await session.execute(
                    update(MemoryModel)
                    .where(MemoryModel.id.in_(ids), MemoryModel.deleted_time.is_(None))
                    .values(
                        last_accessed=case(
                            *[
                                (MemoryModel.id == mid, value.last_accessed)
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
                        MemoryModel.pinned,
                        MemoryModel.expires_at,
                        MemoryModel.last_accessed,
                        MemoryModel.access_count,
                    )
                )
                return [dict(row._mapping) for row in result.fetchall()]

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
        tokens = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fa5]+", query)
        terms: list[str] = []
        for token in tokens:
            escaped = token.replace('"', '""')
            if re.search(r"[\u4e00-\u9fa5]", token) or len(token) >= 3:
                terms.append(f'"{escaped}" OR "{escaped}"*')
            else:
                terms.append(f'"{escaped}"')
        if not terms:
            return []

        sql = """
            SELECT m.id, m.content, m.metadata_json, m.session_id,
                   m.created_at, m.pinned, m.expires_at, m.last_accessed,
                   m.access_count, m.type, m.category, m.confidence, m.source, m.status,
                   m.last_observed_at, m.validity_status, m.valid_until,
                   m.revision_of, m.revision,
                   bm25(memory_fts) AS rank
            FROM memory_fts
            JOIN memories m ON memory_fts.memory_id = m.id
            WHERE memory_fts MATCH :match_expr AND m.deleted_time IS NULL
              AND m.status = 'active'
              AND m.validity_status != 'invalid'
              AND (m.valid_until IS NULL OR m.valid_until >= :now)
        """
        params: dict[str, Any] = {
            "match_expr": " OR ".join(terms),
            "limit": limit,
            "now": datetime.now().isoformat(),
        }
        for key, value in (where or {}).items():
            if key not in _FILTER_COLUMNS:
                raise ValueError(f"Unsupported memory filter: {key}")
            sql += f" AND m.{key} = :where_{key}"
            params[f"where_{key}"] = value
        sql += " ORDER BY rank LIMIT :limit"
        async with get_memory_database_session() as session:
            rows = (await session.execute(text(sql), params)).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            rank = row.rank if row.rank is not None else 0.0
            results.append(
                {
                    "id": row.id,
                    "content": row.content,
                    "metadata": _metadata(row),
                    "score": abs(rank) / (1.0 + abs(rank)),
                    "source": "keyword",
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
        if filters.get("pinned_only"):
            stmt = stmt.where(MemoryModel.pinned == 1)
        if filters.get("expired_only"):
            stmt = stmt.where(
                MemoryModel.expires_at.is_not(None),
                MemoryModel.expires_at < datetime.now().isoformat(),
                MemoryModel.pinned == 0,
            )
        if filters.get("session_id"):
            stmt = stmt.where(MemoryModel.session_id == filters["session_id"])
        stmt = (
            stmt.order_by(MemoryModel.created_at.desc())
            .offset(filters.get("offset", 0))
            .limit(filters.get("limit", 50))
        )
        async with get_memory_database_session() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": row.id,
                "content": row.content,
                "metadata": _metadata(row),
                "pinned": bool(row.pinned),
                "expires_at": row.expires_at,
                "created_at": row.created_at,
                "last_accessed": row.last_accessed,
                "access_count": row.access_count,
                "validity_status": row.validity_status,
                "valid_until": row.valid_until,
                "revision_of": row.revision_of,
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
            "pinned": [MemoryModel.deleted_time.is_(None), MemoryModel.pinned == 1],
            "expired": [
                MemoryModel.deleted_time.is_(None),
                MemoryModel.expires_at.is_not(None),
                MemoryModel.expires_at < now.isoformat(),
                MemoryModel.pinned == 0,
            ],
            "recent_week": [
                MemoryModel.deleted_time.is_(None),
                MemoryModel.created_at >= (now - timedelta(days=7)).isoformat(),
            ],
        }
        async with get_memory_database_session() as session:
            result: dict[str, int] = {}
            for name, clauses in conditions.items():
                value = (
                    await session.execute(
                        select(func.count(MemoryModel.id)).where(*clauses)
                    )
                ).scalar_one()
                result[name] = int(value)
        return result

    async def update_content(self, memory_id: str, content: str) -> str | None:
        """

        参数：
            memory_id (str): 记忆记录唯一标识。
            content (str): 待保存或处理的内容。

        返回值：
            str | None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        async with get_memory_database_session() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(MemoryModel).where(
                            MemoryModel.id == memory_id,
                            MemoryModel.deleted_time.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    return None
                previous = row.content
                row.content = content
                await session.execute(
                    text("DELETE FROM memory_fts WHERE memory_id = :id"),
                    {"id": memory_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO memory_fts (content, memory_id) VALUES (:content, :id)"
                    ),
                    {"content": content, "id": memory_id},
                )
                return previous

    async def get_active_memory(self, memory_id: str) -> dict[str, Any] | None:
        """读取一条可被修订的活跃记忆及其结构化元数据。

        参数：
            memory_id：目标记忆 ID。

        返回：
            活跃记录存在时返回内容、固定状态和元数据；否则返回 None。

        异常：
            SQLite 查询失败时向上抛出异常。
        """
        async with get_memory_database_session() as session:
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
            "content": row.content,
            "pinned": bool(row.pinned),
            "metadata": _metadata(row),
        }

    async def mark_superseded(self, old_id: str, new_id: str) -> bool:
        now = datetime.now().isoformat()
        async with get_memory_database_session() as session:
            async with session.begin():
                result = await session.execute(
                    update(MemoryModel).where(
                        MemoryModel.id == old_id,
                        MemoryModel.status == "active",
                        MemoryModel.deleted_time.is_(None),
                    ).values(status="superseded", superseded_by=new_id, superseded_at=now)
                )
                if result.rowcount != 1:
                    return False
                await session.execute(text("""INSERT OR IGNORE INTO memory_relations
                    (source_memory_id, target_memory_id, relation_type, created_at)
                    VALUES (:source, :target, 'supersedes', :created)"""),
                    {"source": new_id, "target": old_id, "created": now})
                await session.execute(text("DELETE FROM memory_fts WHERE memory_id = :id"), {"id": old_id})
                return True

    async def restore_active(self, memory_id: str) -> None:
        """回滚 supersede 后的旧记忆状态并恢复关键词索引。

        参数：
            memory_id：需要重新激活的旧版本 ID。

        返回：
            None。

        异常：
            SQLite 更新失败时向上抛出异常。
        """
        async with get_memory_database_session() as session:
            async with session.begin():
                row = await session.get(MemoryModel, memory_id)
                if row is None:
                    return
                row.status = "active"
                row.superseded_by = None
                row.superseded_at = None
                await session.execute(
                    text("DELETE FROM memory_fts WHERE memory_id = :id"),
                    {"id": memory_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO memory_fts (content, memory_id) VALUES (:content, :id)"
                    ),
                    {"content": row.content, "id": memory_id},
                )

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
            SQLite 事务失败时向上抛出异常。
        """
        async with get_memory_database_session() as session:
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
                if validity_status == "invalid":
                    await session.execute(
                        text("DELETE FROM memory_fts WHERE memory_id = :id"),
                        {"id": memory_id},
                    )
                else:
                    await session.execute(
                        text("DELETE FROM memory_fts WHERE memory_id = :id"),
                        {"id": memory_id},
                    )
                    await session.execute(
                        text(
                            "INSERT INTO memory_fts (content, memory_id) VALUES (:content, :id)"
                        ),
                        {"content": row.content, "id": memory_id},
                    )
                return previous

    async def add_relation(
        self, source_id: str, target_id: str, relation_type: str
    ) -> None:
        async with get_memory_database_session() as session:
            async with session.begin():
                await session.execute(
                    text("""INSERT OR IGNORE INTO memory_relations
                        (source_memory_id, target_memory_id, relation_type, created_at)
                        VALUES (:source, :target, :relation, :created)"""),
                    {"source": source_id, "target": target_id,
                     "relation": relation_type, "created": datetime.now().isoformat()},
                )

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
        async with get_memory_database_session() as session:
            async with session.begin():
                await session.execute(
                    update(MemoryModel)
                    .where(MemoryModel.id.in_(memory_ids))
                    .values(deleted_time=datetime.now().isoformat())
                )
                statement = text(
                    "DELETE FROM memory_fts WHERE memory_id IN :ids"
                ).bindparams(bindparam("ids", expanding=True))
                await session.execute(statement, {"ids": memory_ids})

    async def set_pin(
        self, memory_id: str, pinned: bool, expires_at: str | None
    ) -> tuple[bool, str | None] | None:
        """

        参数：
            memory_id (str): 记忆记录唯一标识。
            pinned (bool): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            expires_at (str | None): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            tuple[bool, str | None] | None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        async with get_memory_database_session() as session:
            async with session.begin():
                row = await session.get(MemoryModel, memory_id)
                if row is None:
                    return None
                previous = (bool(row.pinned), row.expires_at)
                row.pinned = 1 if pinned else 0
                row.expires_at = expires_at
                return previous

    async def expired_ids(self, now_iso: str) -> list[str]:
        """

        参数：
            now_iso (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            list[str]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        async with get_memory_database_session() as session:
            rows = (
                await session.execute(
                    select(MemoryModel.id).where(
                        MemoryModel.expires_at.is_not(None),
                        MemoryModel.expires_at < now_iso,
                        MemoryModel.pinned == 0,
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
        async with get_memory_database_session() as session:
            async with session.begin():
                await session.execute(
                    text("DELETE FROM memories WHERE id = :id"), {"id": memory_id}
                )
                await session.execute(
                    text("DELETE FROM memory_fts WHERE memory_id = :id"),
                    {"id": memory_id},
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
        async with get_memory_database_session() as session:
            async with session.begin():
                for memory_id in memory_ids:
                    row = await session.get(MemoryModel, memory_id)
                    if row is None:
                        continue
                    row.deleted_time = None
                    await session.execute(
                        text("DELETE FROM memory_fts WHERE memory_id = :id"),
                        {"id": memory_id},
                    )
                    await session.execute(
                        text(
                            "INSERT INTO memory_fts (content, memory_id) VALUES (:content, :id)"
                        ),
                        {"content": row.content, "id": memory_id},
                    )
