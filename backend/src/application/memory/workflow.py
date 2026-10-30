"""冻结提取结果，通过单事务提交事实，并独立保存任务回顾。"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import replace
from datetime import timezone
from typing import Literal, Protocol

from domain.leases import LeaseGrant, LeaseLostError
from domain.memory import (
    CompletedTurn,
    MemoryCandidate,
    MemoryEpisode,
    MemoryEpisodeRepositoryPort,
    MemoryEpisodeSummary,
    MemoryExecutionEvidence,
    MemoryRepositoryPort,
    MemorySuppressedError,
    ResolutionAction,
)
from application.memory.ports import (
    MemoryEpisodeSummarizer,
    MemoryFactExtractor,
    MemoryResolver,
)
from application.contracts.memory import memory_domain_to_schema

logger = logging.getLogger(__name__)


class MemoryEmbedder(Protocol):
    """生成待写入事实的检索向量。"""

    async def embed_query(self, text: str) -> list[float]:
        """为记忆检索查询生成向量表示。

        参数：
        - `text`（str）：待规范化、解析或展示的文本；约束：无额外运行时限制。

        返回：
        - list[float]：与输入文本对应的向量分量列表。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...


class MemoryWriteWorkflow:
    """协调规则决策、原子事实提交和独立的任务回顾。"""

    def __init__(
        self,
        memory: MemoryRepositoryPort,
        extractor: MemoryFactExtractor,
        resolver: MemoryResolver,
        embedder: MemoryEmbedder,
        *,
        episode_summarizer: MemoryEpisodeSummarizer,
        episodes: MemoryEpisodeRepositoryPort,
    ) -> None:
        """保存完整生产依赖；事实和向量必须共同提交，回顾使用独立仓储。

        参数：
        - `memory`（MemoryRepositoryPort）：读取、更新或持久化记忆事实的应用端口；约束：无额外运行时限制。
        - `extractor`（MemoryFactExtractor）：从对话中抽取事实候选的依赖；约束：无额外运行时限制。
        - `resolver`（MemoryResolver）：对候选事实进行去重和冲突处理的依赖；约束：无额外运行时限制。
        - `embedder`（MemoryEmbedder）：为文本生成向量表示的依赖；约束：无额外运行时限制。
        - `episode_summarizer`（MemoryEpisodeSummarizer）：将对话片段整理为回顾记录的依赖；约束：无额外运行时限制。
        - `episodes`（MemoryEpisodeRepositoryPort）：本次查询涉及的回顾记录集合；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._memory = memory
        self._extractor = extractor
        self._resolver = resolver
        self._embedder = embedder
        self._episode_summarizer = episode_summarizer
        self._episodes = episodes

    async def process_completed_turn(
        self, turn: CompletedTurn, *, lease: LeaseGrant
    ) -> int:
        """返回新写入事实数；任一分支失败会重试，但仍执行另一分支。

        参数：
        - `turn`（CompletedTurn）：当前已经完成的对话轮次及其消息；约束：须通过方法内校验；失败条件见下方异常说明。
        - `lease`（LeaseGrant）：当前后台作业持有的租约授权及执行代次；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - int：本轮成功创建或更新的记忆事实数量。

        异常：
        - ValueError：校验条件 `lease.key.resource_type != 'memory_processing' or lease.key.resource_id != turn.turn_id` 成立时（异常说明：memory_processing_lease_mismatch）。
        - ValueError：校验条件 `not turn.project_id` 成立时（异常说明：memory_turn_project_required）。
        - LeaseLostError（重新抛出捕获的异常）：校验条件 `捕获 LeaseLostError 后` 成立时。
        - ExceptionGroup：校验条件 `len(errors) > 1` 成立时（异常说明：memory_write_branches_failed）。
        - errors[0]：校验条件 `errors` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        if (
            lease.key.resource_type != "memory_processing"
            or lease.key.resource_id != turn.turn_id
        ):
            raise ValueError("memory_processing_lease_mismatch")
        if not turn.project_id:
            raise ValueError("memory_turn_project_required")
        # 两条分支分别完成自己的幂等提交，避免事实异常阻止任务回顾。
        errors: list[Exception] = []
        written = 0
        try:
            written = await self._write_facts(turn, lease=lease)
        except LeaseLostError:
            raise
        except MemorySuppressedError:
            pass
        except Exception as error:
            errors.append(error)
        try:
            await self._write_episode(turn, lease=lease)
        except Exception as error:
            errors.append(error)
        if len(errors) > 1:
            raise ExceptionGroup("memory_write_branches_failed", errors)
        if errors:
            raise errors[0]
        return written

    async def _write_facts(self, turn: CompletedTurn, *, lease: LeaseGrant) -> int:
        """逐条提交冻结候选；已提交 ID 在重新决策前跳过。

        参数：
        - `turn`（CompletedTurn）：当前已经完成的对话轮次及其消息；约束：无额外运行时限制。
        - `lease`（LeaseGrant）：当前后台作业持有的租约授权及执行代次；约束：无额外运行时限制。

        返回：
        - int：本次提交成功写入的事实数量。

        异常：
        - RuntimeError：校验条件 `len(resolutions) != 1` 成立时（异常说明：memory_resolver_result_count_mismatch）。
        - 未列出的依赖异常按当前实现传播。
        """
        written = 0
        for candidate in await self._extractor.extract(turn, lease=lease):
            operation_id = _operation_id(turn.turn_id, candidate)
            if await self._memory.get(operation_id) is not None:
                logger.info(
                    "memory_candidate_already_written",
                    extra={
                        "turn_id": turn.turn_id,
                        "operation_id": operation_id,
                    },
                )
                continue
            resolutions = await self._resolver.resolve([candidate])
            if len(resolutions) != 1:
                raise RuntimeError("memory_resolver_result_count_mismatch")
            resolution = resolutions[0]
            if resolution.action in {
                ResolutionAction.IGNORE,
                ResolutionAction.SKIP,
            }:
                logger.info(
                    "memory_candidate_skipped",
                    extra={
                        "turn_id": turn.turn_id,
                        "operation_id": operation_id,
                        "action": resolution.action.value,
                        "reason_code": resolution.reason_code,
                    },
                )
                continue
            # 模型和 embedding 调用均发生在数据库事务外，避免持锁等待远端。
            embedding = await self._embedder.embed_query(candidate.content)
            try:
                applied = await self._memory.apply_resolution(
                    resolution,
                    operation_id=operation_id,
                    embedding=embedding,
                    lease=lease,
                )
            except MemorySuppressedError as exc:
                logger.info(
                    "memory_candidate_suppressed_error",
                    extra={
                        "turn_id": turn.turn_id,
                        "operation_id": operation_id,
                        "reason": str(exc),
                    },
                )
                continue
            written += int(applied)
        return written

    async def _write_episode(self, turn: CompletedTurn, *, lease: LeaseGrant) -> None:
        """仅用冻结 Run 证据创建同轮唯一的任务回顾。

        参数：
        - `turn`（CompletedTurn）：当前已经完成的对话轮次及其消息；约束：无额外运行时限制。
        - `lease`（LeaseGrant）：当前后台作业持有的租约授权及执行代次；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - ValueError：校验条件 `existing is not None；existing.source_fingerprint != evidence.source_fingerprint` 成立时（异常说明：memory_episode_source_fingerprint_conflict）。
        - ValueError：校验条件 `not (existing is not None)；proposal.summary is None` 成立时（异常说明：memory_episode_summary_missing）。
        - ValueError：校验条件 `not (existing is not None)；unknown_source_ids` 成立时（异常说明：memory_episode_source_reference_invalid）。
        - 未列出的依赖异常按当前实现传播。
        """
        evidence = turn.execution_evidence
        if evidence is None:
            return
        # 已有回顾只允许在来源指纹一致时复用，避免同一 Turn 的证据被静默替换。
        existing = await self._episodes.get_by_turn(turn.turn_id)
        if existing is not None:
            if existing.source_fingerprint != evidence.source_fingerprint:
                raise ValueError("memory_episode_source_fingerprint_conflict")
            episode = existing
            logger.info(
                "memory_episode_already_exists",
                extra={
                    "turn_id": turn.turn_id,
                    "episode_id": episode.episode_id,
                },
            )
        else:
            project_id = turn.project_id

            proposal = await self._episode_summarizer.summarize(turn)
            if not proposal.retain:
                return
            if proposal.summary is None:
                raise ValueError("memory_episode_summary_missing")
            source_refs_by_id = {
                reference.source_id: reference for reference in evidence.source_refs
            }
            # 提案只能引用冻结快照中的来源，拒绝模型或旧结果引入的未知引用。
            unknown_source_ids = set(proposal.source_ref_ids).difference(
                source_refs_by_id
            )
            if unknown_source_ids:
                raise ValueError("memory_episode_source_reference_invalid")

            outcome = _episode_outcome(evidence)
            summary = replace(
                proposal.summary,
                outcome=outcome,
            )
            source_refs = tuple(
                source_refs_by_id[source_id] for source_id in proposal.source_ref_ids
            )
            # 从 Turn ID 派生稳定主键，使并发或重试提交收敛到同一条回顾记录。
            proposed_episode = MemoryEpisode(
                episode_id="episode_"
                + hashlib.sha256(turn.turn_id.encode()).hexdigest()[:32],
                turn_id=turn.turn_id,
                session_id=turn.session_id,
                project_id=project_id,
                content=_episode_content(summary),
                summary=summary,
                source_refs=source_refs,
                source_fingerprint=evidence.source_fingerprint,
                created_at=turn.completed_at.astimezone(timezone.utc),
            )
            episode = await self._episodes.create_if_absent(
                proposed_episode, lease=lease
            )
            if episode is None:
                return


def _operation_id(turn_id: str, candidate: MemoryCandidate) -> str:
    """根据轮次和规范候选内容生成与数组顺序无关的 ID。

    参数：
    - `turn_id`（str）：已完成对话轮次的稳定标识；约束：无额外运行时限制。
    - `candidate`（MemoryCandidate）：当前待融合、筛选或追踪的候选项；约束：无额外运行时限制。

    返回：
    - str：由租约身份、会话和 Run 稳定计算出的记忆作业操作标识。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
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

    参数：
    - `evidence`（MemoryExecutionEvidence）：支持当前事实判断的来源证据；约束：无额外运行时限制。

    返回：
    - Literal['completed', 'partial', 'failed', 'cancelled', 'unknown']：由完成、失败、取消和工具结果推导出的回顾状态枚举值。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
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

    参数：
    - `summary`（MemoryEpisodeSummary）：面向用户或后续流程的简要结果说明；约束：无额外运行时限制。

    返回：
    - str：最多 8000 字符的稳定展示正文。 本次操作生成、读取、规范化或渲染后的文本。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    lines = [
        f"任务回顾：{summary.title}",
        f"目标：{summary.goal}",
        f"结果状态：{summary.outcome}",
        f"实际结果：{summary.result}",
    ]
    if summary.attempts:
        lines.append(f"关键尝试：{summary.attempts}")
    if summary.reusable_learnings:
        lines.append("可复用经验：" + "；".join(summary.reusable_learnings))
    if summary.unresolved:
        lines.append("遗留问题：" + "；".join(summary.unresolved))
    return "\n".join(lines)[:8000]


__all__ = ["MemoryWriteWorkflow"]
