"""检索窗口中的原子事实组与未决矛盾。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from domain.memory import FactCardinality, MemoryFact, MemoryFactIdentity, MemoryRecord
from application.contracts.memory import MemoryFactSchema, memory_schema_to_domain


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
    """从持久化 metadata 中读取严格结构化事实。

    参数：
    - `record`（MemoryRecord）：当前领域记录，作为本次转换、筛选或渲染的输入；约束：无额外运行时限制。

    返回：
    - MemoryFact | None：从记忆记录 metadata 读取并校验的结构化事实；缺少或无效时返回 None。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
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
    """按事实身份和基数合并记忆，保留冲突立场并限制展示成员数量。

    参数：
    - `seed`（MemoryRecord）：触发当前事实组构建的命中记录；约束：无额外运行时限制。
    - `related`（list[MemoryRecord]）：与命中记录处于同一事实身份下的相关记录；约束：无额外运行时限制。

    返回：
    - MemoryFactContextGroup | None：按事实身份和冲突关系合并的上下文成员组。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
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

    cardinalities = {
        fact.cardinality
        for _, fact in matching
        if fact.cardinality is not FactCardinality.UNKNOWN
    }
    cardinality = (
        next(iter(cardinalities))
        if len(cardinalities) == 1
        else FactCardinality.UNKNOWN
    )
    members = matching
    key_discriminator: str | None = None
    if cardinality is FactCardinality.SET:
        members = [
            (record, fact)
            for record, fact in matching
            if fact.fact_value == seed_fact.fact_value
        ]
        key_discriminator = seed_fact.fact_value or ""
    elif cardinality is FactCardinality.UNKNOWN:
        members = [(seed, seed_fact)]
        key_discriminator = seed.id

    # 单值事实区分值与立场，集合事实只在同一成员的立场相反时冲突。
    conflicted = len(
        {_conflict_signature(fact, cardinality) for _, fact in members}
    ) > 1
    members.sort(key=lambda pair: pair[0].id)
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
    all_member_ids = (
        tuple(record.id for record, _ in members) if conflicted else member_ids
    )
    total_member_count = len(members) if conflicted else 1
    complete = not conflicted or len(shown_members) == len(members)
    anchor_id = seed.id if seed.id in member_ids else member_ids[0]
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
    """根据事实身份和区分值生成稳定分组键。

    参数：
    - `identity`（tuple[str, str, str, str]）：经校验的领域身份组合，用于确认操作范围；约束：无额外运行时限制。
    - `discriminator`（str | None）：参与稳定分组键计算的额外区分值；约束：允许为 None，按未提供值的分支处理。

    返回：
    - str：由事实身份和区分值确定的稳定分组键。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
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
    """将未解决的事实冲突渲染为带证据和完整度说明的文本。

    参数：
    - `fact_key`（str）：事实字段名称，用于标识事实主题；约束：无额外运行时限制。
    - `members`（list[tuple[MemoryRecord, MemoryFact]]）：处于同一事实身份下、参与冲突判断或渲染的记录集合；约束：无额外运行时限制。
    - `total_count`（int）：完整事实组中的成员总数；约束：无额外运行时限制。
    - `complete`（bool）：指示当前展示是否包含完整成员集合；约束：无额外运行时限制。

    返回：
    - str：描述事实组成员、冲突立场及完整度的上下文文本。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
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
    """优先保留命中来源和每个冲突立场，再按 ID 稳定补足。

    参数：
    - `seed_id`（str）：用于优先保留或锚定事实组的种子记录标识；约束：无额外运行时限制。
    - `members`（list[tuple[MemoryRecord, MemoryFact]]）：处于同一事实身份下、参与冲突判断或渲染的记录集合；约束：无额外运行时限制。
    - `cardinality`（FactCardinality）：事实的基数类型，用于判断成员关系和冲突语义；约束：无额外运行时限制。
    - `limit`（int）：本次读取或处理的数量上限；约束：无额外运行时限制。

    返回：
    - list[tuple[MemoryRecord, MemoryFact]]：符合条件的有序结果集合；集合成员。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    selected: list[tuple[MemoryRecord, MemoryFact]] = []
    seed = next((pair for pair in members if pair[0].id == seed_id), None)
    if seed is not None:
        selected.append(seed)
    # 先覆盖不同冲突签名，避免条数上限只保留同一立场的重复记录。
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
    """按事实基数生成用于冲突判断的值与立场签名。

    参数：
    - `fact`（MemoryFact）：从记忆记录中解析出的结构化事实；约束：无额外运行时限制。
    - `cardinality`（FactCardinality）：事实的基数类型，用于判断成员关系和冲突语义；约束：无额外运行时限制。

    返回：
    - tuple[str | None, str]：用于比较事实值和立场的二元签名。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    if cardinality is FactCardinality.SINGLE:
        return fact.fact_value, fact.polarity
    return None, fact.polarity


__all__ = ["MemoryFactContextGroup", "build_fact_context_group", "record_fact"]
