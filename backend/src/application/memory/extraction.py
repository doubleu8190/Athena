"""事实候选提取、范围过滤和冲突决策。"""

from __future__ import annotations

from dataclasses import replace
from datetime import timezone
from pathlib import Path

from backend.src.shared.logging import get_logger
from domain.leases import LeaseGrant

from domain.integrations import LLMPort
from domain.memory import (
    CompletedTurn,
    FactCardinality,
    MemoryCandidate,
    MemoryFact,
    MemoryFactExtractionSnapshot,
    MemorySnapshotPort,
    MemoryRecord,
    MemoryRepositoryPort,
    MemoryResolution,
    MemorySourceReference,
    ResolutionAction,
)

from application.memory.ports import MemoryFactExtractor, MemoryResolver
from shared import read_system_prompt, render_prompt_template
from shared.contracts.memory import (
    MemoryFactExtractionOutputSchema,
    MemoryFactSchema,
    MemoryFactProposalSchema,
    memory_schema_to_domain,
)

logger = get_logger(__name__)

_PERSONAL_CATEGORIES = {"preference", "profile"}


def _same_fact_value(record: MemoryRecord, candidate: MemoryCandidate) -> bool:
    """比较事实值和肯定/否定极性，不比较正文格式或时间。"""
    candidate_fact = candidate.fact
    record_fact = _record_fact(record)
    if record_fact is None:
        return False
    return (
        record_fact.fact_value == candidate_fact.fact_value
        and record_fact.polarity == candidate_fact.polarity
    )


def _retraction_target_is_in_source(fact: MemoryFact) -> bool:
    """确认撤回目标出现在已保存的用户原文片段中。"""
    if not fact.retracts_value:
        return False
    target = fact.retracts_value.casefold()
    return any(target in source.excerpt.casefold() for source in fact.source_refs)


def _record_fact(record: MemoryRecord) -> MemoryFact | None:
    """读取结构化事实；非事实记录不参与事实组决策。"""
    value = record.metadata.get("fact")
    if value is None:
        return None
    if isinstance(value, MemoryFact):
        return value
    return memory_schema_to_domain(MemoryFactSchema.model_validate(value))


class LLMFactExtractor(MemoryFactExtractor):
    """提取原子事实，并冻结可重试的候选清单。"""

    def __init__(
        self,
        llm: LLMPort,
        *,
        snapshots: MemorySnapshotPort,
        prompt_root: Path | None = None,
    ) -> None:
        """保存提取模型、冻结候选仓储和可选提示词目录。"""
        self._llm = llm
        self._snapshots = snapshots
        self._prompt_root = prompt_root

    async def extract(
        self, turn: CompletedTurn, *, lease: LeaseGrant
    ) -> list[MemoryCandidate]:
        """提取并返回轮次候选；已存在冻结清单时不重复调用模型。"""
        saved = await self._snapshots.get_extraction_snapshot(turn.turn_id)
        if saved is not None:
            logger.info(
                "发现已有冻结事实候选，使用缓存数据。", extra={"turn_id": turn.turn_id}
            )
            return list(saved.candidates)
        if not turn.project_id:
            raise ValueError("memory_turn_project_required")
        project_id = turn.project_id
        prompt_template = (
            read_system_prompt("fact_extraction.md")
            if self._prompt_root is None
            else (self._prompt_root / "fact_extraction.md").read_text(encoding="utf-8")
        )
        prompt = render_prompt_template(
            prompt_template,
            {
                "conversation_text": (
                    f"[用户] {turn.user_text}\n[助手] {turn.assistant_text}"
                ),
            },
        )
        output = await self._llm.invoke_structured(
            [{"role": "system", "content": prompt}],
            MemoryFactExtractionOutputSchema,
        )
        if not isinstance(output, MemoryFactExtractionOutputSchema):
            raise TypeError("memory_fact_extraction_schema_required")
        logger.info(
            "已从模型获取事实提取结果。", extra={"turn_id": turn.turn_id}, output=output
        )
        candidates = [
            candidate
            for proposal in output.atomic_facts
            if (candidate := self._candidate(turn, proposal, project_id=project_id))
            is not None
        ]
        saved = await self._snapshots.save_extraction_snapshot(
            MemoryFactExtractionSnapshot(
                turn_id=turn.turn_id, candidates=tuple(candidates)
            ),
            lease=lease,
        )
        logger.info("已保存冻结事实候选快照。")
        return list(saved.candidates)

    @staticmethod
    def _candidate(
        turn: CompletedTurn, proposal: MemoryFactProposalSchema, *, project_id: str
    ) -> MemoryCandidate | None:
        """由模型字段和程序掌握的来源信息构造事实候选。"""
        if proposal.confidence < 0.6:
            return None
        personal = (
            proposal.scope_kind == "global"
            and proposal.category in _PERSONAL_CATEGORIES
        )
        scope_kind = "global" if personal else "project"
        scope_id = "global" if personal else project_id
        subject_key = "user:self" if personal else f"project:{project_id}"
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
            cardinality=proposal.cardinality,
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
            category=proposal.category,
            confidence=proposal.confidence,
            session_id=turn.session_id,
            fact=fact,
        )


class MemoryFactResolver(MemoryResolver):
    """按明确意图、事实基数和唯一目标执行确定性决策。"""

    def __init__(self, memory: MemoryRepositoryPort) -> None:
        """保存按事实身份查询的仓储。"""
        self._memory = memory

    async def resolve(
        self, candidates: list[MemoryCandidate]
    ) -> list[MemoryResolution]:
        """读取每条候选的有效事实组，并附上提交时需要复核的 ID 集合。"""
        results: list[MemoryResolution] = []
        for candidate in candidates:
            fact = candidate.fact
            if fact.claim_intent == "retract" and not _retraction_target_is_in_source(
                fact
            ):
                results.append(
                    MemoryResolution(
                        ResolutionAction.SKIP,
                        candidate,
                        reason_code="retraction_target_not_in_source",
                    )
                )
                logger.info(
                    "retraction_target_not_in_source",
                    extra={
                        "turn_id": candidate.source_turn_id,
                        "fact_key": fact.fact_key,
                        "fact_value": fact.fact_value,
                    },
                )
                continue
            existing = await self._memory.list_fact_group(fact)
            logger.info(
                "existing_facts_for_candidate",
                extra={
                    "turn_id": candidate.source_turn_id,
                    "fact_key": fact.fact_key,
                    "fact_value": fact.fact_value,
                    "existing": existing,
                },
            )
            decision = self._decide(candidate, existing)
            results.append(
                replace(
                    decision,
                    expected_memory_ids=tuple(sorted(item.id for item in existing)),
                )
            )
        return results

    @staticmethod
    def _decide(
        candidate: MemoryCandidate, existing: list[MemoryRecord]
    ) -> MemoryResolution:
        """根据候选意图和已存事实选择动作；歧义只跳过，不覆盖。"""
        fact = candidate.fact
        stored_facts = [(item, _record_fact(item)) for item in existing]
        known_cardinalities = {
            stored.cardinality
            for _, stored in stored_facts
            if stored is not None and stored.cardinality is not FactCardinality.UNKNOWN
        }
        if fact.cardinality is not FactCardinality.UNKNOWN:
            known_cardinalities.add(fact.cardinality)
        cardinality_consistent = len(known_cardinalities) <= 1
        cardinality = (
            next(iter(known_cardinalities))
            if len(known_cardinalities) == 1
            else FactCardinality.UNKNOWN
        )
        equivalent = [item for item in existing if _same_fact_value(item, candidate)]

        if fact.claim_intent == "retract":
            if len(existing) != 1:
                return MemoryResolution(
                    ResolutionAction.SKIP,
                    candidate,
                    reason_code="retraction_target_not_unique",
                )
            target = existing[0]
            return MemoryResolution(
                ResolutionAction.REVISE,
                candidate,
                target_memory_id=target.id,
                relation="supersedes",
            )

        if not cardinality_consistent:
            return MemoryResolution(
                ResolutionAction.SKIP,
                candidate,
                reason_code="fact_cardinality_disagreement",
            )

        # 唯一的未知记录可以由后续明确的同值或更正证据替代。
        if (
            len(stored_facts) == 1
            and stored_facts[0][1] is not None
            and stored_facts[0][1].cardinality is FactCardinality.UNKNOWN
            and fact.cardinality is not FactCardinality.UNKNOWN
            and (equivalent or fact.claim_intent == "correct")
        ):
            return MemoryResolution(
                ResolutionAction.REVISE,
                candidate,
                target_memory_id=stored_facts[0][0].id,
                relation="supersedes",
            )

        # 集合中的不同成员可以共存，只处理同成员的相反极性。
        targets = existing
        if cardinality is FactCardinality.SET:
            targets = [
                item
                for item in existing
                if (stored := _record_fact(item)) is not None
                and stored.fact_value == fact.fact_value
                and stored.polarity != fact.polarity
            ]
            if not targets:
                if equivalent:
                    # 如果集合中已经存在相同记忆了，就忽略新候选
                    return MemoryResolution(
                        ResolutionAction.IGNORE,
                        candidate,
                        target_memory_id=equivalent[0].id,
                    )
                if fact.claim_intent == "correct":
                    # 如果集合中没有相同ID的记忆需要纠正，就跳过新候选
                    return MemoryResolution(
                        ResolutionAction.SKIP,
                        candidate,
                        reason_code="correction_target_missing",
                    )
                return MemoryResolution(ResolutionAction.CREATE, candidate)
            if equivalent:
                # 如果集合中同时存在相同记忆和不同极性的记忆，就跳过新候选
                return MemoryResolution(
                    ResolutionAction.SKIP,
                    candidate,
                    reason_code="set_member_conflict_requires_review",
                )
        else:
            if not existing:
                return MemoryResolution(ResolutionAction.CREATE, candidate)
            if len(equivalent) == len(existing):
                return MemoryResolution(
                    ResolutionAction.IGNORE,
                    candidate,
                    target_memory_id=equivalent[0].id,
                )

        # 多个冲突值不能只修订其中一条，避免留下新的分叉。
        if len(targets) != 1:
            return MemoryResolution(
                ResolutionAction.SKIP,
                candidate,
                reason_code="conflict_group_requires_review",
            )
        target = targets[0]
        if fact.claim_intent == "correct":
            return MemoryResolution(
                ResolutionAction.REVISE,
                candidate,
                target_memory_id=target.id,
                relation="supersedes",
            )
        if cardinality is FactCardinality.UNKNOWN:
            return MemoryResolution(
                ResolutionAction.SKIP, candidate, reason_code="fact_cardinality_unknown"
            )
        return MemoryResolution(
            ResolutionAction.CREATE,
            candidate,
            target_memory_id=target.id,
            relation="contradicts",
        )


__all__ = ["LLMFactExtractor", "MemoryFactResolver"]
