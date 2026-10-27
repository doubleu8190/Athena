"""顺序提交冻结候选、修订关系与向量投影。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import timezone
from typing import Literal

from domain.memory import (
    CompletedTurn,
    MemoryCandidateSnapshot,
    MemoryEpisode,
    MemoryEpisodeRepositoryPort,
    MemoryEpisodeSummary,
    MemoryExecutionEvidence,
    MemoryOperationsPort,
    MemoryRecordMetadata,
    MemoryResolution,
    MemoryRepositoryPort,
    MemoryResolutionProposal,
    MemorySuppressedError,
    MemoryStateChangedError,
    SessionMemorySettingsPort,
    MemoryVectorIndexPort,
    MemoryWriteCommand,
    ResolutionAction,
)

from application.memory.ports import (
    MemoryEpisodeSummarizer,
    MemoryFactExtractor,
    MemoryResolver,
    MemoryWriteTrigger,
)
from shared.contracts.memory import memory_domain_to_schema
from domain.leases import LeaseGrant


class MemoryWriteWorkflow:
    """执行记忆触发、解析、幂等提交与向量索引。"""

    def __init__(
        self,
        memory: MemoryRepositoryPort,
        trigger: MemoryWriteTrigger,
        extractor: MemoryFactExtractor,
        resolver: MemoryResolver,
        vector_index: MemoryVectorIndexPort | None = None,
        *,
        operations: MemoryOperationsPort | None = None,
        settings: SessionMemorySettingsPort | None = None,
        episode_summarizer: MemoryEpisodeSummarizer | None = None,
        episodes: MemoryEpisodeRepositoryPort | None = None,
    ) -> None:
        """保存记忆写入依赖。

        参数：``memory``、``trigger``、``extractor``、``resolver`` 是事实写入主链；
        ``vector_index``、``operations``、``settings``、``episode_summarizer``、
        ``episodes`` 分别控制向量投影、写入回执、会话开关及任务回顾。
        返回：无。
        异常：不主动抛出业务异常。
        """
        self._memory = memory
        self._trigger = trigger
        self._extractor = extractor
        self._resolver = resolver
        self._vector_index = vector_index
        self._operations = operations
        self._settings = settings
        self._episode_summarizer = episode_summarizer
        self._episodes = episodes

    async def process_completed_turn(
        self, turn: CompletedTurn, *, lease: LeaseGrant
    ) -> int:
        """顺序提交事实候选并保存可追溯任务回顾。

        参数：turn 是已完成轮次及可选的冻结执行证据。
        返回：本次新建或修订的事实记忆数量；episode 单独持久化，不计入该数量。
        异常：提取、决策、并发复核或 episode 保存失败时向上传播。
        """
        if lease.key.resource_type != "memory_processing" or lease.key.resource_id != turn.turn_id:
            raise ValueError("memory_processing_lease_mismatch")
        if not await self._generation_enabled(turn.session_id):
            if self._operations is not None:
                await self._operations.skip_pending_turn(turn.turn_id, lease=lease)
            return 0
        if not self._trigger.should_process(turn):
            return 0
        candidates = await self._extractor.extract(turn, lease=lease)
        if not await self._generation_enabled(turn.session_id):
            if self._operations is not None:
                await self._operations.skip_pending_turn(turn.turn_id, lease=lease)
            return 0
        written = 0
        for candidate in candidates:
            candidate_snapshot = MemoryCandidateSnapshot.from_candidate(candidate)
            operation_id = _operation_id(turn.turn_id, candidate_snapshot)
            saved_decision: MemoryResolutionProposal | None = None
            if self._operations is not None:
                operation = await self._operations.begin_operation(
                    operation_id, turn.turn_id, candidate_snapshot, lease=lease
                )
                if operation.status != "pending":
                    continue
                candidate = operation.candidate.to_candidate()
                saved_decision = operation.decision

            if not await self._generation_enabled(turn.session_id):
                if self._operations is not None:
                    await self._operations.complete_operation(
                        operation_id,
                        status="skipped",
                        decision=(
                            saved_decision
                            or MemoryResolutionProposal(
                                action="DEFER",
                                reason_code="memory_generation_disabled",
                            )
                        ),
                        result_memory_id=None,
                        projection_status="not_required",
                        lease=lease,
                    )
                continue

            if saved_decision is None:
                resolutions = await self._resolver.resolve([candidate])
                if len(resolutions) != 1:
                    raise RuntimeError("memory_resolver_result_count_mismatch")
                resolution = resolutions[0]
                decision = _decision_contract(resolution)
                if self._operations is not None:
                    operation = await self._operations.record_decision(
                        operation_id, decision, lease=lease
                    )
                    if operation.decision is None:
                        raise RuntimeError("memory_write_decision_not_saved")
                    decision = operation.decision
                    resolution = _resolution_from(decision, operation.candidate)
            else:
                decision = saved_decision
                resolution = _resolution_from(decision, operation.candidate)
            if not await self._generation_enabled(turn.session_id):
                if self._operations is not None:
                    await self._operations.complete_operation(
                        operation_id,
                        status="skipped",
                        decision=decision,
                        result_memory_id=None,
                        projection_status="not_required",
                        lease=lease,
                    )
                continue
            try:
                status, memory_id, projection_status = await self._apply(
                    resolution,
                    turn=turn,
                    operation_id=operation_id,
                    lease=lease,
                )
            except MemorySuppressedError:
                status, memory_id, projection_status = "skipped", None, "not_required"
            except MemoryStateChangedError:
                if self._operations is not None:
                    await self._operations.clear_pending_decision(operation_id, lease=lease)
                raise
            if self._operations is not None:
                await self._operations.complete_operation(
                    operation_id,
                    status=status,
                    decision=decision,
                    result_memory_id=memory_id,
                    projection_status=projection_status,
                    lease=lease,
                )
            if status == "applied":
                written += 1
        await self._write_episode(turn, lease=lease)
        return written

    async def _write_episode(self, turn: CompletedTurn, *, lease: LeaseGrant) -> None:
        """仅用冻结 Run 证据创建同轮唯一的任务回顾。

        参数：turn 是记忆作业领取时恢复的完成轮次。
        返回：无；无执行证据、依赖未装配或提案不保留时跳过。
        异常：模型、来源约束或持久化失败时向上传播以触发作业重试。
        """
        evidence = turn.execution_evidence
        if (
            evidence is None
            or self._episode_summarizer is None
            or self._episodes is None
        ):
            return
        if not await self._generation_enabled(turn.session_id):
            return
        existing = await self._episodes.get_by_turn(turn.turn_id)
        if existing is not None:
            if existing.source_fingerprint != evidence.source_fingerprint:
                raise ValueError("memory_episode_source_fingerprint_conflict")
            episode = existing
        else:
            project_id = turn.project_id
            if project_id is None and self._settings is not None:
                project_id = (await self._settings.get(turn.session_id)).project_id

            proposal = await self._episode_summarizer.summarize(turn)
            if not proposal.retain:
                return
            if proposal.summary is None:
                raise ValueError("memory_episode_summary_missing")
            source_refs_by_id = {
                reference.source_id: reference for reference in evidence.source_refs
            }
            unknown_source_ids = set(proposal.source_ref_ids).difference(source_refs_by_id)
            if unknown_source_ids:
                raise ValueError("memory_episode_source_reference_invalid")

            outcome = _episode_outcome(evidence)
            summary = replace(
                proposal.summary,
                outcome=outcome,
                verification_scope="",
            )
            source_refs = tuple(
                source_refs_by_id[source_id] for source_id in proposal.source_ref_ids
            )
            proposed_episode = MemoryEpisode(
                episode_id="episode_" + hashlib.sha256(turn.turn_id.encode()).hexdigest()[:32],
                turn_id=turn.turn_id,
                session_id=turn.session_id,
                project_id=project_id,
                content=_episode_content(summary),
                summary=summary,
                source_refs=source_refs,
                source_fingerprint=evidence.source_fingerprint,
                created_at=turn.completed_at.astimezone(timezone.utc).isoformat(),
            )
            episode = await self._episodes.create_if_absent(proposed_episode, lease=lease)
            if episode is None:
                return
        await self._write_reusable_learnings(episode, lease=lease)

    async def _write_reusable_learnings(self, episode: MemoryEpisode, *, lease: LeaseGrant) -> None:
        """Persist evidence-linked reusable procedures through idempotent write receipts."""
        for learning in episode.summary.reusable_learnings:
            content = f"procedure: {learning.strip()}"
            if content == "procedure:":
                continue
            candidate = MemoryCandidateSnapshot(
                content=content,
                source_turn_id=episode.turn_id,
                memory_type="procedure",
                category="solution",
                confidence=0.7,
                session_id=episode.session_id,
            )
            operation_id = "memproc_" + hashlib.sha256(
                f"{episode.episode_id}:{content.casefold()}".encode()
            ).hexdigest()
            decision = MemoryResolutionProposal(
                action="CREATE", reason_code="episode_reusable_learning"
            )
            saved_decision = decision
            if self._operations is not None:
                operation = await self._operations.begin_operation(
                    operation_id, episode.turn_id, candidate, lease=lease
                )
                if operation.status != "pending":
                    continue
                candidate = operation.candidate
                saved_decision = operation.decision or decision

            scope_kind = "project" if episode.project_id is not None else "session"
            scope_id = episode.project_id or episode.session_id
            metadata = {
                "session_id": episode.session_id,
                "source_turn_id": episode.turn_id,
                "type": "procedure",
                "category": "solution",
                "confidence": 0.7,
                "source": "episode_reusable_learning",
                "source_kind": "episode",
                "episode_id": episode.episode_id,
                "scope_kind": scope_kind,
                "scope_id": scope_id,
                "outcome": episode.summary.outcome,
                "verification_scope": episode.summary.verification_scope,
                "source_refs": [
                    memory_domain_to_schema(reference).model_dump(mode="json")
                    for reference in episode.source_refs
                ],
            }
            try:
                memory_id = await self._memory.create_memory_from_worker(
                    MemoryWriteCommand(
                        content=candidate.content,
                        metadata=metadata,
                        operation_key=operation_id,
                    ),
                    lease=lease,
                )
            except MemorySuppressedError:
                if self._operations is not None:
                    await self._operations.complete_operation(
                        operation_id,
                        status="skipped",
                        decision=saved_decision,
                        result_memory_id=None,
                        projection_status="not_required",
                        lease=lease,
                    )
                continue
            indexed = await self._index(memory_id, candidate.content, lease=lease)
            projection_status = (
                "not_required"
                if self._vector_index is None
                else ("ready" if indexed else "failed")
            )
            if self._operations is not None:
                await self._operations.complete_operation(
                    operation_id,
                    status="applied",
                    decision=saved_decision,
                    result_memory_id=memory_id,
                    projection_status=projection_status,
                    lease=lease,
                )

    async def _generation_enabled(self, session_id: str) -> bool:
        """读取本轮提交前的最新生成设置。"""
        if self._settings is None:
            return True
        return (await self._settings.get(session_id)).generate_memories

    async def _apply(
        self,
        resolution: MemoryResolution,
        *,
        turn: CompletedTurn,
        operation_id: str,
        lease: LeaseGrant,
    ) -> tuple[str, str | None, str]:
        """提交单条决策，随后建立关系并更新检索投影。"""
        candidate = resolution.candidate
        metadata = memory_domain_to_schema(
            MemoryRecordMetadata(
                extra_fields={
                    "session_id": turn.session_id,
                    "source_turn_id": turn.turn_id,
                    "type": candidate.memory_type,
                    "category": candidate.category,
                    "confidence": candidate.confidence,
                    "source": "extraction",
                },
                fact=candidate.fact,
            )
        ).model_dump(mode="json", exclude_none=True)

        if resolution.action is ResolutionAction.CREATE:
            memory_id = await self._memory.create_memory_from_worker(
                MemoryWriteCommand(
                    content=candidate.content,
                    metadata=metadata,
                    operation_key=operation_id,
                    expected_memory_ids=resolution.expected_memory_ids,
                ),
                lease=lease,
            )
        elif resolution.action is ResolutionAction.REVISE:
            if not resolution.target_memory_id:
                raise ValueError("memory_revision_target_required")
            memory_id = await self._memory.revise_from_worker(
                resolution.target_memory_id,
                resolution.final_content or candidate.content,
                metadata=metadata,
                operation_key=operation_id,
                expected_memory_ids=resolution.expected_memory_ids,
                lease=lease,
            )
            if memory_id is None:
                if self._operations is not None:
                    await self._operations.clear_pending_decision(operation_id, lease=lease)
                raise RuntimeError("memory_revision_target_changed")
        elif resolution.action is ResolutionAction.IGNORE:
            if resolution.target_memory_id is None:
                raise ValueError("memory_ignore_target_required")
            return "ignored", resolution.target_memory_id, "not_required"
        elif resolution.action is ResolutionAction.DEFER:
            return "deferred", None, "not_required"
        else:
            raise ValueError("memory_resolution_action_unsupported")

        if resolution.relation is not None:
            if resolution.target_memory_id is None:
                raise ValueError("memory_relation_target_required")
            await self._memory.add_relation_from_worker(
                memory_id,
                resolution.target_memory_id,
                resolution.relation,
                operation_id=operation_id,
                lease=lease,
            )
        indexed = await self._index(memory_id, candidate.content, lease=lease)
        projection_status = (
            "not_required"
            if self._vector_index is None
            else ("ready" if indexed else "failed")
        )
        return "applied", memory_id, projection_status

    async def _index(self, memory_id: str, content: str, *, lease: LeaseGrant) -> bool:
        """构建向量投影；失败会留在写入回执中以便修复。"""
        if self._vector_index is None:
            return True
        try:
            await self._vector_index.index(memory_id, content, lease=lease)
        except Exception:
            return False
        return True


def _operation_id(turn_id: str, candidate: MemoryCandidateSnapshot) -> str:
    """根据轮次和规范候选内容生成与数组顺序无关的 ID。"""
    canonical = json.dumps(
        memory_domain_to_schema(candidate).model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(f"{turn_id}:{canonical}".encode()).hexdigest()
    return f"memop_{digest}"


def _episode_outcome(
    evidence: MemoryExecutionEvidence,
) -> Literal["completed", "partial", "failed", "cancelled", "unknown"]:
    """从冻结任务终态确定回顾状态，避免模型把失败说成完成。

    参数：``evidence`` 是同一 Run 首次领取时冻结的任务状态集合。
    返回：只由任务终态决定的回顾 outcome。
    异常：不主动抛出业务异常。
    """
    statuses = {task.status.casefold() for task in evidence.tasks}
    if not statuses:
        return "unknown"
    if evidence.truncated or len(evidence.tasks) < evidence.total_task_count:
        return "partial"
    if statuses == {"completed"}:
        return "completed"
    if statuses == {"failed"}:
        return "failed"
    if statuses == {"cancelled"}:
        return "cancelled"
    if statuses == {"unknown"}:
        return "unknown"
    return "partial"


def _episode_content(summary: MemoryEpisodeSummary) -> str:
    """把结构化回顾整理成后续检索可读的纯文本。

    参数：``summary`` 是已按字段长度约束的任务回顾。
    返回：最多 8000 字符的稳定展示正文。
    异常：不主动抛出业务异常。
    """
    lines = [
        f"任务回顾：{summary.title}",
        f"目标：{summary.goal}",
        f"结果状态：{summary.outcome}",
        f"实际结果：{summary.result}",
    ]
    if summary.attempts:
        lines.append(f"关键尝试：{summary.attempts}")
    if summary.verification_scope:
        lines.append(f"验证范围：{summary.verification_scope}")
    if summary.reusable_learnings:
        lines.append("可复用经验：" + "；".join(summary.reusable_learnings))
    if summary.unresolved:
        lines.append("遗留问题：" + "；".join(summary.unresolved))
    return "\n".join(lines)[:8000]


def _decision_contract(resolution: MemoryResolution) -> MemoryResolutionProposal:
    """将已校验的领域决策转成持久化回执契约。"""
    relation = resolution.relation
    if relation not in {None, "supersedes", "contradicts"}:
        raise ValueError("memory_relation_kind_invalid")
    return MemoryResolutionProposal(
        action=resolution.action.value.upper(),
        target_memory_id=resolution.target_memory_id,
        relation=relation,
        reason_code=resolution.reason_code,
        expected_memory_ids=resolution.expected_memory_ids,
    )


def _resolution_from(
    decision: MemoryResolutionProposal,
    candidate: MemoryCandidateSnapshot,
) -> MemoryResolution:
    """从持久决策恢复领域动作，避免重试时重新解释候选。"""
    action = ResolutionAction(decision.action.casefold())
    domain_candidate = candidate.to_candidate()
    return MemoryResolution(
        action=action,
        candidate=domain_candidate,
        target_memory_id=decision.target_memory_id,
        final_content=(
            domain_candidate.content if action is ResolutionAction.REVISE else None
        ),
        relation=decision.relation,
        reason_code=decision.reason_code,
        expected_memory_ids=decision.expected_memory_ids,
    )


__all__ = ["MemoryWriteWorkflow"]
