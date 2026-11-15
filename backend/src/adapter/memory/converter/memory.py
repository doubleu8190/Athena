"""把记忆 ORM 行投影为不可变领域对象。"""

from __future__ import annotations

from adapter.memory.model import MemoryModel
from common.date import as_utc
from domain.memory import LoadPolicy, Memory, MemoryKind, MemorySource


def memory_to_domain(row: MemoryModel) -> Memory:
    """投影一条非墓碑记忆，并拒绝可用投影中的空分类。"""
    if row.deleted_time is not None or not row.origin or not row.kind or not row.load_policy:
        raise ValueError("memory_projection_invalid")
    sources = tuple(
        MemorySource(
            kind=item.source_kind,
            reference=item.source_ref,
            source_at=None if item.source_at is None else as_utc(item.source_at),
        )
        for item in row.sources
    )
    return Memory(
        id=row.id,
        namespace_id=row.namespace_id,
        scope_key=row.scope_key,
        content=row.content,
        status=row.status,
        origin=row.origin,
        version=row.version,
        expires_at=None if row.expires_at is None else as_utc(row.expires_at),
        sources=sources,
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
        kind=MemoryKind(row.kind),
        load_policy=LoadPolicy(row.load_policy),
        content_hash=row.content_hash,
        explicit_long_term=bool(row.explicit_long_term),
        last_evidenced_at=(
            None if row.last_evidenced_at is None else as_utc(row.last_evidenced_at)
        ),
        superseded_by_id=row.superseded_by_id,
    )


__all__ = ["memory_to_domain"]
