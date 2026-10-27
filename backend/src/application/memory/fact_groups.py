"""检索窗口中的原子事实组与未决矛盾。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from domain.memory import MemoryFact, MemoryFactIdentity, MemoryRecord

from application.memory.fact_policy import FactCardinality, cardinality_for
from shared.contracts.memory import MemoryFactSchema, memory_schema_to_domain


@dataclass(frozen=True, slots=True)
class MemoryFactContextGroup:
    """一条可整体放入上下文窗口的事实组。"""

    group_key: str
    anchor_id: str
    member_ids: tuple[str, ...]
    all_member_ids: tuple[str, ...]
    total_member_count: int
    complete: bool
    content: str
    conflicted: bool
    cardinality: FactCardinality
    identity: MemoryFactIdentity
    member_value: str | None


def record_fact(record: MemoryRecord) -> MemoryFact | None:
    """从持久化 metadata 中读取严格结构化事实。"""
    value = record.metadata.get("fact")
    if value is None:
        return None
    if isinstance(value, MemoryFact):
        return value
    try:
        return memory_schema_to_domain(MemoryFactSchema.model_validate(value))
    except ValueError:
        return None


def build_fact_context_group(
    seed: MemoryRecord,
    related: list[MemoryRecord],
) -> MemoryFactContextGroup | None:
    """把同一事实范围内需要一起理解的成员合并为一个上下文项。"""
    seed_fact = record_fact(seed)
    if seed_fact is None:
        return None
    identity = (
        seed_fact.scope_kind,
        seed_fact.scope_id,
        seed_fact.subject_key,
        seed_fact.fact_key,
    )
    matching: list[tuple[MemoryRecord, MemoryFact]] = []
    for record in related:
        fact = record_fact(record)
        if fact is None:
            continue
        if (
            fact.scope_kind,
            fact.scope_id,
            fact.subject_key,
            fact.fact_key,
        ) == identity:
            matching.append((record, fact))
    if not matching:
        matching = [(seed, seed_fact)]

    cardinality = cardinality_for(seed_fact.fact_key)
    if cardinality is FactCardinality.SINGLE:
        members = matching
        conflicted = len(
            {(fact.fact_value, fact.polarity) for _, fact in members}
        ) > 1
        key_discriminator: str | None = None
    elif cardinality is FactCardinality.SET:
        members = [
            (record, fact)
            for record, fact in matching
            if fact.fact_value == seed_fact.fact_value
        ]
        conflicted = len({fact.polarity for _, fact in members}) > 1
        key_discriminator = seed_fact.fact_value or ""
    else:
        members = [(seed, seed_fact)]
        conflicted = False
        key_discriminator = seed.id

    members.sort(key=lambda pair: pair[0].id)
    all_member_ids = tuple(record.id for record, _ in members)
    if conflicted:
        shown_members = _select_members(
            seed.id,
            members,
            cardinality=cardinality,
            limit=6,
        )
    else:
        seed_member = next((pair for pair in members if pair[0].id == seed.id), None)
        shown_members = [seed_member] if seed_member is not None else members[:1]
    member_ids = tuple(record.id for record, _ in shown_members)
    if not conflicted:
        all_member_ids = member_ids
    total_member_count = len(members) if conflicted else 1
    complete = not conflicted or len(shown_members) == len(members)
    anchor_id = (
        seed.id
        if any(record.id == seed.id for record, _ in shown_members)
        else shown_members[0][0].id
    )
    group_key = _group_key(identity, key_discriminator)
    content = (
        _render_conflict(
            seed_fact.fact_key,
            shown_members,
            total_count=total_member_count,
            complete=complete,
        )
        if conflicted
        else shown_members[0][0].content
    )
    return MemoryFactContextGroup(
        group_key=group_key,
        anchor_id=anchor_id,
        member_ids=member_ids,
        all_member_ids=all_member_ids,
        total_member_count=total_member_count,
        complete=complete,
        content=content,
        conflicted=conflicted,
        cardinality=cardinality,
        identity=MemoryFactIdentity(
            scope_kind=seed_fact.scope_kind,
            scope_id=seed_fact.scope_id,
            subject_key=seed_fact.subject_key,
            fact_key=seed_fact.fact_key,
        ),
        member_value=(
            seed_fact.fact_value
            if cardinality is FactCardinality.SET
            else None
        ),
    )


def _group_key(
    identity: tuple[str, str, str, str], discriminator: str | None
) -> str:
    raw = json.dumps(
        [*identity, discriminator],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "memory-group-" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _render_conflict(
    fact_key: str,
    members: list[tuple[MemoryRecord, MemoryFact]],
    *,
    total_count: int,
    complete: bool,
) -> str:
    if complete:
        heading = f"未解决的记忆冲突（{fact_key}；共 {total_count} 条事实）："
    else:
        heading = (
            f"未解决的记忆冲突（{fact_key}；展示 {len(members)}/{total_count} 条；"
            "未完整展开，不能据此判断没有其他值）："
        )
    lines = [heading]
    for record, fact in members:
        observed_at = fact.asserted_at or "时间未知"
        polarity = "肯定" if fact.polarity == "affirmed" else "否定"
        excerpt = (
            fact.source_refs[0].excerpt.strip()[:200]
            if fact.source_refs
            else ""
        )
        evidence = f"；原话依据：「{excerpt}」" if excerpt else ""
        lines.append(
            f"- {record.content}；立场：{polarity}；陈述时间：{observed_at}{evidence}"
        )
    return "\n".join(lines)


def _select_members(
    seed_id: str,
    members: list[tuple[MemoryRecord, MemoryFact]],
    *,
    cardinality: FactCardinality,
    limit: int,
) -> list[tuple[MemoryRecord, MemoryFact]]:
    """优先保留命中来源和每个冲突立场，再按 ID 稳定补足。"""
    selected: list[tuple[MemoryRecord, MemoryFact]] = []
    seed = next((pair for pair in members if pair[0].id == seed_id), None)
    if seed is not None:
        selected.append(seed)
    represented = {
        _conflict_signature(fact, cardinality) for _, fact in selected
    }
    for pair in members:
        signature = _conflict_signature(pair[1], cardinality)
        if signature in represented:
            continue
        if pair not in selected:
            selected.append(pair)
            represented.add(signature)
        if len(selected) >= limit:
            return selected[:limit]
    for pair in members:
        if pair not in selected:
            selected.append(pair)
        if len(selected) >= limit:
            break
    return selected[:limit]


def _conflict_signature(
    fact: MemoryFact, cardinality: FactCardinality
) -> tuple[str | None, str]:
    if cardinality is FactCardinality.SINGLE:
        return fact.fact_value, fact.polarity
    return None, fact.polarity


__all__ = ["MemoryFactContextGroup", "build_fact_context_group", "record_fact"]
