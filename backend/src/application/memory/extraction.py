"""事实候选提取、范围过滤和冲突决策。"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from domain.integrations import LLMPort
from domain.memory import (
    CompletedTurn,
    MemoryCandidate,
    MemoryCandidateSnapshot,
    MemoryFact,
    MemoryFactExtractionSnapshot,
    MemoryListRequest,
    MemoryOperationsPort,
    MemoryRecord,
    MemoryRepositoryPort,
    MemoryResolution,
    MemoryResolutionProposal,
    MemorySourceReference,
    ResolutionAction,
    SessionMemorySettingsPort,
)

from application.memory.fact_policy import FactCardinality, cardinality_for
from application.memory.ports import MemoryFactExtractor, MemoryResolver, MemoryWriteTrigger
from shared.contracts.memory import (
    MemoryFactExtractionOutputSchema,
    MemoryFactSchema,
    MemoryResolutionProposalSchema,
    memory_domain_to_schema,
    memory_schema_to_domain,
)

_PROMPT_ROOT = Path(__file__).resolve().parents[3] / "prompt"
_PERSONAL_CATEGORIES = {"preference", "profile"}
_PROJECT_CATEGORIES = {"project", "technical_decision", "solution"}
_CANDIDATE_PATTERN = re.compile(r"^([a-z][a-z0-9_]{0,29}):\s*(.+)$", re.DOTALL)
_Model = TypeVar("_Model", bound=BaseModel)


def _response_text(value: Any) -> str:
    """提取模型响应中的 JSON 正文。"""
    if isinstance(value, dict):
        value = value.get("content", value.get("result", value))
    else:
        value = getattr(value, "content", value)
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False)


def _typed_response(value: Any, schema: type[_Model]) -> _Model:
    """将模型输出按严格 JSON 契约解析，非法结果交由作业重试。"""
    if isinstance(value, schema):
        return value
    text = _response_text(value)
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    return schema.model_validate_json(text)


def _fill(template: str, **values: str) -> str:
    """只替换指定占位符，保留 prompt 示例中的 JSON 大括号。"""
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


def _fact_identity(fact: MemoryFact) -> tuple[str, str, str, str]:
    """返回同一事实可比较的适用范围和规范属性。"""
    return fact.scope_kind, fact.scope_id, fact.subject_key, fact.fact_key


def _same_fact_value(record: MemoryRecord, candidate: MemoryCandidate) -> bool:
    """比较事实值和肯定/否定极性，不比较正文格式或时间。"""
    candidate_fact = candidate.fact
    record_fact = _record_fact(record)
    if candidate_fact is not None and record_fact is not None:
        return (
            record_fact.fact_value == candidate_fact.fact_value
            and record_fact.polarity == candidate_fact.polarity
        )
    if candidate_fact is not None and (
        candidate_fact.polarity != "affirmed"
        or candidate_fact.claim_intent == "retract"
    ):
        return False
    return record.content.strip().casefold() == candidate.content.strip().casefold()


def _retraction_target_is_in_source(fact: MemoryFact) -> bool:
    """确认撤回目标出现在已保存的用户原文片段中。"""
    if not fact.retracts_value:
        return False
    target = fact.retracts_value.casefold()
    return any(target in source.excerpt.casefold() for source in fact.source_refs)


def _matches_retraction_target(record: MemoryRecord, fact: MemoryFact) -> bool:
    """核对被撤回的具体事实值，而不是只核对事实 key。"""
    if not fact.retracts_value:
        return False
    stored = _record_fact(record)
    if stored is not None:
        return stored.fact_value == fact.retracts_value
    return record.content.strip().casefold() == (
        f"{fact.fact_key}: {fact.retracts_value}".casefold()
    )


def _candidate_key(content: str) -> str:
    """从展示正文中读取受限格式的事实键。"""
    match = _CANDIDATE_PATTERN.match(content.strip())
    return "" if match is None else match.group(1)


def _record_fact(record: MemoryRecord) -> MemoryFact | None:
    """读取已写入的结构化事实；旧记录没有 fact 字段时返回 None。"""
    value = record.metadata.get("fact")
    if value is None:
        return None
    if isinstance(value, MemoryFact):
        return value
    return memory_schema_to_domain(MemoryFactSchema.model_validate(value))


def _is_active_and_valid(record: MemoryRecord, *, now: datetime) -> bool:
    """排除已替代、无效或超过任一截止时间的记忆。"""
    if record.status.value != "active" or record.validity_status != "valid":
        return False
    for value in (record.expires_at, record.valid_until):
        if value is None:
            continue
        try:
            deadline = datetime.fromisoformat(value)
        except ValueError:
            return False
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if deadline <= now:
            return False
    fact = _record_fact(record)
    if fact is not None and fact.valid_from is not None:
        try:
            valid_from = datetime.fromisoformat(fact.valid_from)
        except ValueError:
            return False
        if valid_from.tzinfo is None:
            valid_from = valid_from.replace(tzinfo=timezone.utc)
        if valid_from > now:
            return False
    return True


class DefaultMemoryWriteTrigger(MemoryWriteTrigger):
    """处理有正文且格式正确的已完成用户轮次。"""

    def should_process(self, turn: CompletedTurn) -> bool:
        """判断轮次是否包含值得交给提取器处理的用户输入。"""
        text = turn.user_text.strip()
        return bool(text) and not text.startswith("/") and len(text) <= 20_000


class LLMFactExtractor(MemoryFactExtractor):
    """提取原子事实，并冻结可重试的候选清单。"""

    def __init__(
        self,
        llm: LLMPort,
        memory: MemoryRepositoryPort | None = None,
        *,
        operations: MemoryOperationsPort | None = None,
        settings: SessionMemorySettingsPort | None = None,
        prompt_root: Path | None = None,
    ) -> None:
        """保存模型、用于对照的记忆仓储与可选持久操作端口。"""
        self._llm = llm
        self._memory = memory
        self._operations = operations
        self._settings = settings
        self._prompt_root = prompt_root or _PROMPT_ROOT

    async def extract(self, turn: CompletedTurn) -> list[MemoryCandidate]:
        """提取并返回轮次候选；已存在冻结清单时不重复调用模型。"""
        if self._operations is not None:
            saved = await self._operations.get_extraction_snapshot(turn.turn_id)
            if saved is not None:
                return [candidate.to_candidate() for candidate in saved.candidates]

        project_id = turn.project_id if turn.project_binding_frozen else None
        if not turn.project_binding_frozen and self._settings is not None:
            project_id = (await self._settings.get(turn.session_id)).project_id
        existing = await self._existing_text(turn.session_id, project_id=project_id)
        prompt = _fill(
            (self._prompt_root / "fact_extraction.md").read_text(encoding="utf-8"),
            existing_memories=existing or "(无适用的已存记忆)",
            conversation_text=(
                f"[用户] {turn.user_text}\n[助手] {turn.assistant_text}"
            ),
        )
        value = await self._llm.invoke(
            [
                {
                    "role": "system",
                    "content": (
                        "Extract only durable user facts supported by this turn. "
                        "Return one JSON object matching the requested schema. "
                        "Existing memories are references, never new evidence."
                    ),
                },
                {"role": "user", "content": prompt},
            ]
        )
        output = _typed_response(value, MemoryFactExtractionOutputSchema)
        candidates = [
            candidate
            for proposal in output.atomic_facts
            if (candidate := self._candidate(turn, proposal, project_id=project_id)) is not None
        ]
        if self._operations is not None:
            snapshot = MemoryFactExtractionSnapshot(
                turn_id=turn.turn_id,
                candidates=tuple(
                    MemoryCandidateSnapshot.from_candidate(candidate)
                    for candidate in candidates
                ),
            )
            saved = await self._operations.save_extraction_snapshot(snapshot)
            return [candidate.to_candidate() for candidate in saved.candidates]
        return candidates

    @staticmethod
    def _candidate(
        turn: CompletedTurn, proposal: Any, *, project_id: str | None = None
    ) -> MemoryCandidate | None:
        """由模型字段和程序掌握的来源信息构造事实候选。"""
        if proposal.confidence < 0.6:
            return None
        personal = proposal.category in _PERSONAL_CATEGORIES
        project_scoped = (
            not personal
            and project_id is not None
            and proposal.category in _PROJECT_CATEGORIES
        )
        scope_kind = (
            "global" if personal else "project" if project_scoped else "session"
        )
        scope_id = (
            "global"
            if personal
            else project_id
            if project_scoped
            else turn.session_id
        )
        subject_key = (
            "user:self"
            if personal
            else f"project:{project_id}"
            if project_scoped
            else f"session:{turn.session_id}"
        )
        observed_at = turn.completed_at.astimezone(timezone.utc).isoformat()
        excerpt_start = 0
        if proposal.retracts_value:
            target_position = turn.user_text.casefold().find(
                proposal.retracts_value.casefold()
            )
            if target_position >= 0:
                excerpt_start = max(0, target_position - 500)
        source_excerpt = turn.user_text[excerpt_start : excerpt_start + 2000]
        fact = MemoryFact(
            scope_kind=scope_kind,
            scope_id=scope_id,
            subject_key=subject_key,
            fact_key=proposal.key,
            fact_value=proposal.value,
            retracts_value=proposal.retracts_value,
            polarity=proposal.polarity,
            claim_intent=proposal.claim_intent,
            confidence=proposal.confidence,
            asserted_at=observed_at,
            source_refs=(
                MemorySourceReference(
                    source_type="turn",
                    source_id=turn.turn_id,
                    excerpt=source_excerpt,
                    observed_at=observed_at,
                ),
            ),
        )
        return MemoryCandidate(
            content=(
                f"{proposal.key}: [已撤回]"
                if proposal.claim_intent == "retract"
                else f"{proposal.key}: {proposal.value}"
            ),
            source_turn_id=turn.turn_id,
            memory_type=proposal.category,
            category=proposal.category,
            confidence=proposal.confidence,
            session_id=turn.session_id,
            fact=fact,
        )

    async def _existing_text(
        self, session_id: str, *, project_id: str | None = None
    ) -> str:
        """只向模型提供全局、当前会话和显式项目记忆作为去重参考。"""
        if self._memory is None or not session_id:
            return ""
        page = await self._memory.list_memories(MemoryListRequest(limit=500))
        values: list[str] = []
        now = datetime.now(timezone.utc)
        for item in page.items:
            if not _is_active_and_valid(item, now=now):
                continue
            fact = _record_fact(item)
            if fact is None:
                scope_kind = item.metadata.get("scope_kind")
                scope_id = item.metadata.get("scope_id")
                if (
                    item.metadata.get("session_id") == session_id
                    or (scope_kind == "global" and scope_id == "global")
                    or (scope_kind == "session" and scope_id == session_id)
                    or (project_id is not None and scope_kind == "project" and scope_id == project_id)
                    or item.metadata.get("category") in {"preference", "profile"}
                ):
                    values.append(item.content)
                continue
            if fact.scope_kind == "global" or (
                fact.scope_kind == "session" and fact.scope_id == session_id
            ) or (
                project_id is not None
                and fact.scope_kind == "project"
                and fact.scope_id == project_id
            ):
                values.append(item.content)
        return "\n".join(f"- {content}" for content in values[:500])


class LLMMemoryResolver(MemoryResolver):
    """按范围与事实键查重，并校验模型提出的修订关系。"""

    def __init__(
        self,
        llm: LLMPort,
        memory: MemoryRepositoryPort,
        *,
        prompt_root: Path | None = None,
    ) -> None:
        """保存解析模型、记忆仓储及冲突决策提示词目录。"""
        self._llm = llm
        self._memory = memory
        self._prompt_root = prompt_root or _PROMPT_ROOT

    async def resolve(
        self, candidates: list[MemoryCandidate]
    ) -> list[MemoryResolution]:
        """逐条检查候选；技术错误向上抛出以便作业重试。"""
        resolutions: list[MemoryResolution] = []
        snapshots: list[tuple[str, ...]] = []
        for candidate in candidates:
            if (
                candidate.fact is not None
                and candidate.fact.claim_intent == "retract"
                and not _retraction_target_is_in_source(candidate.fact)
            ):
                resolutions.append(
                    MemoryResolution(
                        ResolutionAction.DEFER,
                        candidate,
                        reason_code="retraction_target_not_in_source",
                    )
                )
                snapshots.append(())
                continue
            existing = await self._existing(candidate)
            snapshots.append(tuple(sorted(item.id for item in existing)))
            fact = candidate.fact
            if fact is None:
                cardinality = FactCardinality.UNKNOWN
            else:
                cardinality = cardinality_for(fact.fact_key)

            if fact is not None and fact.claim_intent == "retract":
                if len(existing) == 1:
                    resolutions.append(await self._decide(candidate, existing))
                else:
                    resolutions.append(
                        MemoryResolution(
                            ResolutionAction.DEFER,
                            candidate,
                            reason_code=(
                                "retraction_target_missing"
                                if not existing
                                else "retraction_target_ambiguous"
                            ),
                        )
                    )
                continue

            if cardinality is FactCardinality.SET and fact is not None:
                same_value = [
                    item
                    for item in existing
                    if (
                        (
                            (stored := _record_fact(item)) is not None
                            and stored.fact_value == fact.fact_value
                        )
                        or _same_fact_value(item, candidate)
                    )
                ]
                equivalent = next(
                    (item for item in same_value if _same_fact_value(item, candidate)),
                    None,
                )
                opposite = [
                    item
                    for item in same_value
                    if (stored := _record_fact(item)) is not None
                    and stored.polarity != fact.polarity
                ]
                if opposite:
                    if equivalent is not None:
                        resolutions.append(
                            MemoryResolution(
                                ResolutionAction.DEFER,
                                candidate,
                                reason_code="set_member_conflict_requires_review",
                            )
                        )
                    elif fact.claim_intent == "correct" and len(opposite) == 1:
                        resolutions.append(await self._decide(candidate, opposite))
                    elif fact.claim_intent == "assert" and len(opposite) == 1:
                        resolutions.append(
                            MemoryResolution(
                                ResolutionAction.CREATE,
                                candidate,
                                target_memory_id=opposite[0].id,
                                relation="contradicts",
                            )
                        )
                    else:
                        resolutions.append(
                            MemoryResolution(
                                ResolutionAction.DEFER,
                                candidate,
                                reason_code="set_member_conflict_ambiguous",
                            )
                        )
                elif equivalent is not None:
                    resolutions.append(
                        MemoryResolution(
                            ResolutionAction.IGNORE,
                            candidate,
                            target_memory_id=equivalent.id,
                            final_content=equivalent.content,
                        )
                    )
                elif fact.claim_intent == "correct":
                    resolutions.append(
                        MemoryResolution(
                            ResolutionAction.DEFER,
                            candidate,
                            reason_code="correction_target_missing",
                        )
                    )
                else:
                    resolutions.append(MemoryResolution(ResolutionAction.CREATE, candidate))
                continue

            equivalent_records = [
                item for item in existing if _same_fact_value(item, candidate)
            ]
            distinct_records = [
                item for item in existing if not _same_fact_value(item, candidate)
            ]
            if cardinality is FactCardinality.SINGLE and len(distinct_records) > 0:
                if len(existing) > 1:
                    resolutions.append(
                        MemoryResolution(
                            ResolutionAction.DEFER,
                            candidate,
                            reason_code="conflict_group_requires_review",
                        )
                    )
                else:
                    resolutions.append(await self._decide(candidate, existing))
            elif equivalent_records:
                if (
                    cardinality
                    in {FactCardinality.SINGLE, FactCardinality.UNKNOWN}
                    and distinct_records
                ):
                    resolutions.append(
                        MemoryResolution(
                            ResolutionAction.DEFER,
                            candidate,
                            reason_code="conflict_group_requires_review",
                        )
                    )
                else:
                    resolutions.append(
                        MemoryResolution(
                            ResolutionAction.IGNORE,
                            candidate,
                            target_memory_id=equivalent_records[0].id,
                            final_content=equivalent_records[0].content,
                        )
                    )
            elif not existing:
                resolutions.append(MemoryResolution(ResolutionAction.CREATE, candidate))
            elif (
                cardinality is FactCardinality.UNKNOWN
                and (fact is None or fact.claim_intent == "assert")
            ):
                resolutions.append(
                    MemoryResolution(
                        ResolutionAction.DEFER,
                        candidate,
                        reason_code="fact_cardinality_unknown",
                    )
                )
            elif len(existing) == 1:
                resolutions.append(await self._decide(candidate, existing))
            else:
                resolutions.append(
                    MemoryResolution(
                        ResolutionAction.DEFER,
                        candidate,
                        reason_code="conflict_group_requires_review",
                    )
                )
        return [
            replace(resolution, expected_memory_ids=expected_ids)
            for resolution, expected_ids in zip(resolutions, snapshots, strict=True)
        ]

    async def _existing(self, candidate: MemoryCandidate) -> list[MemoryRecord]:
        """跨会话查找同一事实键，只保留相同范围和主体的记录。"""
        key = candidate.fact.fact_key if candidate.fact else (
            _candidate_key(candidate.content)
        )
        if not key:
            raise ValueError("memory_candidate_fact_key_missing")
        result: list[MemoryRecord] = []
        offset = 0
        while True:
            page = await self._memory.list_memories(
                MemoryListRequest(limit=500, offset=offset)
            )
            result.extend(page.items)
            offset += len(page.items)
            if not page.items or offset >= page.total:
                break
        matching: list[MemoryRecord] = []
        now = datetime.now(timezone.utc)
        for item in result:
            if not _is_active_and_valid(item, now=now):
                continue
            if _candidate_key(item.content) != key:
                continue
            fact = _record_fact(item)
            if candidate.fact is not None:
                if fact is not None and _fact_identity(fact) == _fact_identity(
                    candidate.fact
                ):
                    if (
                        candidate.fact.claim_intent != "retract"
                        or fact.fact_value == candidate.fact.retracts_value
                    ):
                        matching.append(item)
                elif (
                    fact is None
                    and item.metadata.get("session_id") == candidate.session_id
                    and (
                        item.content.casefold() == candidate.content.casefold()
                        or (
                            candidate.fact.claim_intent == "retract"
                            and item.content.casefold()
                            == f"{key}: {candidate.fact.retracts_value}".casefold()
                        )
                    )
                ):
                    matching.append(item)
            elif item.metadata.get("session_id") == candidate.session_id:
                matching.append(item)
        matching.sort(key=lambda item: item.id)
        return matching

    async def _decide(
        self, candidate: MemoryCandidate, existing: list[MemoryRecord]
    ) -> MemoryResolution:
        """解析同一事实键的不同值，并拒绝越界或不完整目标。"""
        fact = candidate.fact
        structured_candidate = (
            "(无结构化事实)"
            if fact is None
            else json.dumps(
                memory_domain_to_schema(fact).model_dump(mode="json"),
                ensure_ascii=False,
            )
        )
        cardinality = (
            FactCardinality.UNKNOWN.value
            if fact is None
            else cardinality_for(fact.fact_key).value
        )
        prompt = _fill(
            (self._prompt_root / "memory_resolution.md").read_text(encoding="utf-8"),
            existing_memory="\n".join(
                f"- {item.id}: {item.content}" for item in existing
            ),
            candidate=(
                f"{candidate.content}\n事实基数：{cardinality}\n"
                f"结构化字段：{structured_candidate}"
            ),
        )
        value = await self._llm.invoke(
            [
                {
                    "role": "system",
                    "content": (
                        "Resolve one typed memory candidate. Return one JSON object "
                        "matching the requested schema. Never invent a target ID."
                    ),
                },
                {"role": "user", "content": prompt},
            ]
        )
        proposal_schema = _typed_response(value, MemoryResolutionProposalSchema)
        proposal = memory_schema_to_domain(proposal_schema)
        targets = {item.id: item for item in existing}
        target = targets.get(proposal.target_memory_id or "")
        if proposal.action == "DEFER":
            return MemoryResolution(
                ResolutionAction.DEFER,
                candidate,
                reason_code=proposal.reason_code or "semantic_uncertainty",
            )
        if proposal.action == "IGNORE":
            if (
                target is None
                or candidate.fact is None
                or candidate.fact.claim_intent == "retract"
                or not _same_fact_value(target, candidate)
            ):
                return MemoryResolution(
                    ResolutionAction.DEFER,
                    candidate,
                    reason_code="equivalent_target_not_proven",
                )
            return MemoryResolution(
                ResolutionAction.IGNORE,
                candidate,
                target_memory_id=target.id,
                final_content=target.content,
            )
        if proposal.action == "REVISE":
            if (
                target is None
                or proposal.relation != "supersedes"
                or candidate.fact is None
                or candidate.fact.claim_intent not in {"correct", "retract"}
                or (
                    candidate.fact.claim_intent == "retract"
                    and not _matches_retraction_target(target, candidate.fact)
                )
            ):
                return MemoryResolution(
                    ResolutionAction.DEFER,
                    candidate,
                    reason_code="revision_evidence_or_target_missing",
                )
            return MemoryResolution(
                ResolutionAction.REVISE,
                candidate,
                target_memory_id=target.id,
                final_content=candidate.content,
                relation="supersedes",
            )
        if proposal.action == "CREATE":
            if (
                target is not None
                and proposal.relation == "contradicts"
                and candidate.fact is not None
                and candidate.fact.claim_intent == "assert"
            ):
                return MemoryResolution(
                    ResolutionAction.CREATE,
                    candidate,
                    target_memory_id=target.id,
                    relation="contradicts",
                )
            return MemoryResolution(
                ResolutionAction.DEFER,
                candidate,
                reason_code="contradiction_target_missing",
            )
        return MemoryResolution(
            ResolutionAction.DEFER,
            candidate,
            reason_code="resolution_action_invalid",
        )


__all__ = [
    "DefaultMemoryWriteTrigger",
    "LLMFactExtractor",
    "LLMMemoryResolver",
]
