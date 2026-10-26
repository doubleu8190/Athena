"""从有效长期事实实时派生有界概览与主题目录。"""

from __future__ import annotations

from datetime import datetime, timezone

from application.runs.context.contracts import ContextItem
from domain.memory import MemoryFactIdentity, MemoryRecord, MemoryRepositoryPort, MemoryStatus

from .fact_groups import build_fact_context_group, record_fact


async def build_memory_index_item(
    repository: MemoryRepositoryPort,
    records: list[MemoryRecord],
    *,
    session_id: str,
    project_id: str | None,
) -> ContextItem | None:
    """用当前有效来源生成短 L0 概览和 L1 主题目录。

    Args:
        repository: 用于复核事实组冲突的记忆仓储。
        records: 已按当前会话和项目作用域查询的有限记忆集合。
        session_id: 当前会话 ID。
        project_id: 当前显式绑定的项目空间 ID。

    Returns:
        可供 Root 定位主题的派生 ContextItem；没有有效来源时返回 None。

    Raises:
        Exception: 事实组读取失败时向调用方传播，避免发布不完整目录。
    """
    current_records = [
        record
        for record in records
        if _is_current(record)
        and _is_in_scope(record, session_id=session_id, project_id=project_id)
    ]
    if not current_records:
        return None

    overview_lines: list[str] = []
    checked_identities: set[tuple[str, str, str, str]] = set()
    for record in current_records:
        fact = record_fact(record)
        if fact is None or not _is_overview_fact(
            record, fact.scope_kind, project_id=project_id
        ):
            continue
        if (
            fact.polarity != "affirmed"
            or fact.claim_intent == "retract"
            or fact.fact_value is None
            or fact.valid_until is not None
            or record.expires_at is not None
        ):
            continue
        identity = (
            fact.scope_kind,
            fact.scope_id,
            fact.subject_key,
            fact.fact_key,
        )
        if identity in checked_identities:
            continue
        checked_identities.add(identity)
        if len(overview_lines) >= 3:
            break
        related = await repository.list_fact_group_by_identity(
            MemoryFactIdentity(
                scope_kind=fact.scope_kind,
                scope_id=fact.scope_id,
                subject_key=fact.subject_key,
                fact_key=fact.fact_key,
            )
        )
        related = [item for item in related if _is_current(item)]
        group = build_fact_context_group(record, related)
        if group is None or group.conflicted or not group.complete:
            continue
        label = "个人" if fact.scope_kind == "global" else "当前项目"
        overview_lines.append(f"- {label}：{group.content[:100]}")

    topic_lines: list[str] = []
    seen_topics: set[tuple[str, str, str]] = set()
    for record in current_records[:8]:
        fact = record_fact(record)
        category = str(
            record.metadata.get("category") or record.metadata.get("type") or "memory"
        )[:40]
        if fact is None:
            topic = str(record.metadata.get("title") or category)[:80]
            scope = str(record.metadata.get("scope_kind") or (
                "session" if record.metadata.get("session_id") == session_id else "global"
            ))
        else:
            topic = f"{fact.subject_key}/{fact.fact_key}"
            scope = fact.scope_kind
        topic_key = (scope, category, topic)
        if topic_key in seen_topics:
            continue
        seen_topics.add(topic_key)
        topic_lines.append(f"- {scope} · {category} · {topic}")

    sections = ["L0 概览（只列无冲突的稳定事实）："]
    sections.extend(overview_lines or ["- 暂无可安全概览的稳定事实。"])
    sections.append("L1 主题目录（入口索引，需按主题检索正文）：")
    sections.extend(topic_lines)
    content = "\n".join(sections)[:1400]
    return ContextItem(
        provider="memory_index",
        content=content,
        source_id=f"memory-index:{session_id}",
        metadata={
            "memory_index": True,
            "session_id": session_id,
            "project_id": project_id,
            "memory_index_source_ids": [record.id for record in current_records[:8]],
            "memory_index_source_count": len(current_records),
            "level": "L0-L1",
        },
    )


def _is_overview_fact(record: MemoryRecord, scope_kind: str, *, project_id: str | None) -> bool:
    """只把个人偏好/背景与当前项目约束放进短概览。"""
    category = record.metadata.get("category") or record.metadata.get("type")
    if scope_kind == "global":
        return category in {"preference", "profile"}
    return (
        scope_kind == "project"
        and project_id is not None
        and record_fact(record) is not None
        and category in {"project", "technical_decision", "solution"}
    )


def _is_current(record: MemoryRecord) -> bool:
    """过滤已失效、待确认、到期或尚未生效的记忆。"""
    if record.status is not MemoryStatus.ACTIVE or record.validity_status != "valid":
        return False
    now = datetime.now(timezone.utc)
    for value in (record.expires_at, record.valid_until):
        if value is None:
            continue
        try:
            deadline = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return False
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if deadline <= now:
            return False
    fact = record_fact(record)
    if fact is None or fact.valid_from is None:
        return True
    try:
        valid_from = datetime.fromisoformat(fact.valid_from)
    except (TypeError, ValueError):
        return False
    if valid_from.tzinfo is None:
        valid_from = valid_from.replace(tzinfo=timezone.utc)
    return valid_from <= now


def _is_in_scope(
    record: MemoryRecord, *, session_id: str, project_id: str | None
) -> bool:
    """确保目录不包含当前会话和项目之外的记录。"""
    fact = record_fact(record)
    if fact is not None:
        return (
            (fact.scope_kind == "global" and fact.scope_id == "global")
            or (fact.scope_kind == "session" and fact.scope_id == session_id)
            or (
                project_id is not None
                and fact.scope_kind == "project"
                and fact.scope_id == project_id
            )
        )
    category = record.metadata.get("category", record.metadata.get("type"))
    scope_kind = record.metadata.get("scope_kind")
    scope_id = record.metadata.get("scope_id")
    return (
        category in {"preference", "profile"}
        or record.metadata.get("session_id") == session_id
        or (scope_kind == "global" and scope_id == "global")
        or (scope_kind == "session" and scope_id == session_id)
        or (project_id is not None and scope_kind == "project" and scope_id == project_id)
    )


__all__ = ["build_memory_index_item"]
