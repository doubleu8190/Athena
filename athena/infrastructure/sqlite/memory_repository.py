"""长期记忆记录的 SQLite/FTS5 仓库。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, case, func, insert, select, text, update

from athena.infrastructure.sqlite.engine import get_session
from athena.infrastructure.sqlite.models import MemoryModel
from athena.infrastructure.sqlite.repositories import _json_dumps, _json_loads

_SEMANTIC_KEYS = frozenset({"session_id", "type", "category", "confidence", "source"})
_SYSTEM_KEYS = frozenset(
    {
        "created_at",
        "last_accessed",
        "access_count",
        "pinned",
        "expires_at",
    }
)
_FILTER_COLUMNS = frozenset(
    {"session_id", "type", "category", "confidence", "source", "pinned"}
)


def _metadata(row: Any) -> dict[str, Any]:
    """执行“metadata”操作。

    参数：
        row (Any): 输入参数；其类型和取值约束由方法签名及实现定义。

    返回值：
        dict[str, Any]: 操作结果；具体语义由调用场景决定。

    异常：
        Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
    """
    metadata: dict[str, Any] = {
        "created_at": row.created_at,
        "last_accessed": row.last_accessed,
        "access_count": row.access_count,
        "pinned": bool(row.pinned),
        "expires_at": row.expires_at or "",
        "session_id": row.session_id,
    }
    for key in ("type", "category", "confidence", "source"):
        value = getattr(row, key)
        if value is not None:
            metadata[key] = value
    metadata.update(_json_loads(row.metadata_json, {}))
    return metadata


class SqliteMemoryRepository:
    """表示 SqliteMemoryRepository 组件，封装相关状态和行为。
    """
    async def add(self, record: dict[str, Any]) -> None:
        """添加数据。

        参数：
            record (dict[str, Any]): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        metadata = record["metadata"]
        extras = {
            key: value
            for key, value in metadata.items()
            if key not in _SEMANTIC_KEYS and key not in _SYSTEM_KEYS
        }
        async with get_session() as session:
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
                    )
                )
                await session.execute(
                    text(
                        "INSERT INTO memory_fts (content, memory_id) VALUES (:content, :id)"
                    ),
                    {"content": record["content"], "id": record["id"]},
                )

    async def flush_access_stats(
        self, stats: dict[str, Any], expires_at: str
    ) -> list[dict[str, Any]]:
        """执行“flush access stats”操作。

        参数：
            stats (dict[str, Any]): 输入参数；其类型和取值约束由方法签名及实现定义。
            expires_at (str): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            list[dict[str, Any]]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
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
                        expires_at=case(
                            (MemoryModel.pinned == 0, expires_at),
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
                return [dict(row._mapping) for row in result.fetchall()]

    async def keyword_search(
        self, query: str, limit: int, where: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """执行“keyword search”操作。

        参数：
            query (str): 检索或搜索文本；应为非空字符串。
            limit (int): 最大返回数量；应为非负整数。
            where (dict[str, Any] | None): 可选过滤条件。

        返回值：
            list[dict[str, Any]]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
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
                   m.access_count, m.type, m.category, m.confidence, m.source,
                   bm25(memory_fts) AS rank
            FROM memory_fts
            JOIN memories m ON memory_fts.memory_id = m.id
            WHERE memory_fts MATCH :match_expr AND m.deleted_time IS NULL
        """
        params: dict[str, Any] = {"match_expr": " OR ".join(terms), "limit": limit}
        for key, value in (where or {}).items():
            if key not in _FILTER_COLUMNS:
                raise ValueError(f"Unsupported memory filter: {key}")
            sql += f" AND m.{key} = :where_{key}"
            params[f"where_{key}"] = value
        sql += " ORDER BY rank LIMIT :limit"
        async with get_session() as session:
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
            filters (Any): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            list[dict[str, Any]]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        stmt = select(MemoryModel).where(MemoryModel.deleted_time.is_(None))
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
        async with get_session() as session:
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
            }
            for row in rows
        ]

    async def counts(self) -> dict[str, int]:
        """统计数量。

        返回值：
            dict[str, int]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
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

    async def update_content(self, memory_id: str, content: str) -> str | None:
        """执行“update content”操作。

        参数：
            memory_id (str): 记忆记录唯一标识。
            content (str): 待保存或处理的内容。

        返回值：
            str | None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        async with get_session() as session:
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

    async def soft_delete(self, memory_ids: list[str]) -> None:
        """执行“soft delete”操作。

        参数：
            memory_ids (list[str]): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
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
                statement = text(
                    "DELETE FROM memory_fts WHERE memory_id IN :ids"
                ).bindparams(bindparam("ids", expanding=True))
                await session.execute(statement, {"ids": memory_ids})

    async def set_pin(
        self, memory_id: str, pinned: bool, expires_at: str | None
    ) -> tuple[bool, str | None] | None:
        """执行“set pin”操作。

        参数：
            memory_id (str): 记忆记录唯一标识。
            pinned (bool): 输入参数；其类型和取值约束由方法签名及实现定义。
            expires_at (str | None): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            tuple[bool, str | None] | None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        async with get_session() as session:
            async with session.begin():
                row = await session.get(MemoryModel, memory_id)
                if row is None:
                    return None
                previous = (bool(row.pinned), row.expires_at)
                row.pinned = 1 if pinned else 0
                row.expires_at = expires_at
                return previous

    async def expired_ids(self, now_iso: str) -> list[str]:
        """执行“expired ids”操作。

        参数：
            now_iso (str): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            list[str]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        async with get_session() as session:
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
        """执行“hard delete”操作。

        参数：
            memory_id (str): 记忆记录唯一标识。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    text("DELETE FROM memories WHERE id = :id"), {"id": memory_id}
                )
                await session.execute(
                    text("DELETE FROM memory_fts WHERE memory_id = :id"),
                    {"id": memory_id},
                )

    async def restore_deleted(self, memory_ids: list[str]) -> None:
        """执行“restore deleted”操作。

        参数：
            memory_ids (list[str]): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        async with get_session() as session:
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
