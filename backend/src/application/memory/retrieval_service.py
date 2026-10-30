"""记忆上下文检索用例。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from domain.memory import (
    MemoryRepositoryPort,
    MemoryFact,
    MemoryRecord,
    MemorySearchRequest,
    MemoryStatus,
    MemoryVectorSearchPort,
    SessionMemorySettingsPort,
)
from domain.retrieval import RetrievalCandidate, RetrievalRankFusion
from domain.retrieval.trace_contracts import (
    RetrievalCandidateTrace,
    RetrievalChannelTrace,
    RetrievalHitTrace,
)
from application.contracts.memory import memory_domain_to_schema
from application.contracts.retrieval import RetrievalHitTraceSchema

from application.runs.context.contracts import ContextItem, ContextPlan, ProviderResult
from application.runs.context.protocols import ContextProvider
from application.memory.fact_groups import (
    MemoryFactContextGroup,
    build_fact_context_group,
    record_fact,
)
from application.memory.index_views import build_memory_index_item
from application.memory.query_terms import extract_memory_query_terms

logger = logging.getLogger(__name__)


def _keyword_score(content: str, query: str) -> float:
    """计算查询词在候选文本中的匹配比例。

    参数：
    - `content`（str）：待处理或返回的正文内容；约束：无额外运行时限制。
    - `query`（str）：用于检索、匹配或解析的原始查询文本；约束：无额外运行时限制。

    返回：
    - float：查询词在文本中的命中比例；查询词集合为空时返回 0.0。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    terms = {term.lower() for term in query.split() if term.strip()}
    if not terms:
        return 0.0
    lowered = content.lower()
    return sum(term in lowered for term in terms) / len(terms)


@dataclass(slots=True)
class _RetrievalState:
    """保存一次记忆检索的输入、阶段状态和原始召回结果。"""

    started: float
    query: str
    session_id: str
    project_id: str | None
    limit: int
    keyword_rows: list[MemoryRecord] = field(default_factory=list)
    vector_rows: list[dict[str, Any]] = field(default_factory=list)
    keyword_status: str = "skipped"
    vector_status: str = "skipped"
    processing_stage: str = "keyword"
    directory_item: ContextItem | None = None
    directory_status: str = "skipped"


@dataclass(slots=True)
class _CandidateState:
    """保存各召回通道候选、融合结果及其可用记忆记录。"""

    active_keyword: list[tuple[int, MemoryRecord]] = field(default_factory=list)
    keyword_candidates: list[RetrievalCandidate] = field(default_factory=list)
    vector_candidates: list[RetrievalCandidate] = field(default_factory=list)
    fused: list[RetrievalCandidate] = field(default_factory=list)
    stale_candidates: list[RetrievalCandidate] = field(default_factory=list)
    record_by_id: dict[str, MemoryRecord] = field(default_factory=dict)


@dataclass(slots=True)
class _FactContextState:
    """缓存候选事实的完整事实组和最终渲染分组。"""

    groups_by_candidate: dict[str, MemoryFactContextGroup] = field(
        default_factory=dict
    )
    group_records: dict[str, MemoryRecord] = field(default_factory=dict)


@dataclass(slots=True)
class _SelectionState:
    """记录本轮上下文裁剪结果及 trace 所需的选择信息。"""

    selected_items: list[ContextItem]
    selected_limit: int
    seen_group_keys: set[str]
    selected_group_members: dict[str, set[str]]
    selected_plain_ids: set[str]


class MemoryContextProvider(ContextProvider):
    """从记忆端口执行关键词与向量混合召回。"""

    name = "memory"

    def __init__(
        self,
        repository: MemoryRepositoryPort,
        vector_indexer: MemoryVectorSearchPort | None = None,
        *,
        limit: int = 8,
        timeout_seconds: float = 3.0,
        settings: SessionMemorySettingsPort | None = None,
    ) -> None:
        """保存记忆仓库、向量索引及会话读取配置。

        参数：
        - `repository`（MemoryRepositoryPort）：负责读取或持久化领域数据的仓储依赖；约束：无额外运行时限制。
        - `vector_indexer`（MemoryVectorSearchPort | None）：执行向量检索或索引操作的可选依赖；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `limit`（int）：本次读取或处理的数量上限；约束：可省略，默认值为 `8`。
        - `timeout_seconds`（float）：本次操作允许使用的最长秒数；约束：可省略，默认值为 `3.0`。
        - `settings`（SessionMemorySettingsPort | None）：当前会话的记忆读取设置；None 表示未提供会话级覆盖；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._repository = repository
        self._vector_indexer = vector_indexer
        self._limit = limit
        self._timeout_seconds = timeout_seconds
        self._settings = settings

    async def retrieve_context(
        self,
        *,
        session_id: str,
        run_id: str,
        plan: ContextPlan,
    ) -> ProviderResult:
        """按计划召回记忆并组装可供后续采集的上下文结果。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。
        - `plan`（ContextPlan）：当前计划对象，提供请求的检索或执行安排；约束：无额外运行时限制。

        返回：
        - ProviderResult：包含检索状态、候选内容和追踪信息的提供方结果。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        started = time.perf_counter()
        query = plan.memory_query
        if not query:
            return ProviderResult(self.name, "succeeded", query=query)
        settings = (
            await self._settings.get(session_id)
            if self._settings is not None
            else None
        )
        if settings is not None and not settings.use_memories:
            return ProviderResult(
                self.name,
                "skipped",
                query=query,
                resource_scope={
                    "memory_suppressed": True,
                    "reason": "session_memory_read_disabled",
                },
            )

        state = _RetrievalState(
            started=started,
            query=query,
            session_id=session_id,
            project_id=None if settings is None else settings.project_id,
            limit=max(self._limit, plan.recall_limit),
        )
        try:
            candidates = await self._retrieve_candidates(state)
            await self._retrieve_directory(state)
            await self._resolve_candidate_records(state, candidates)
            facts = await self._load_fact_context(state, candidates)
            selection = self._select_context_items(plan, state, candidates, facts)
            traces = self._build_candidate_traces(
                state, candidates, facts, selection
            )
            return self._success_result(plan, state, selection, traces)
        except TimeoutError:
            return self._failure_result(state, "timeout", "memory retrieval timed out")
        except Exception as exc:
            return self._failure_result(state, "failed", str(exc))

    async def _retrieve_candidates(
        self, state: _RetrievalState
    ) -> _CandidateState:
        """在共享时限内读取关键词和向量通道，并生成初始候选。

        参数：
        - `state`（_RetrievalState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - _CandidateState：记录当前流程数据和阶段信息的 `_CandidateState` 状态对象。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        candidates = _CandidateState()
        async with asyncio.timeout(self._timeout_seconds):
            state.keyword_rows = await self._repository.search(
                MemorySearchRequest(
                    query=state.query,
                    limit=state.limit,
                    terms=extract_memory_query_terms(state.query),
                    filters={
                        "memory_scope": {
                            "session_id": state.session_id,
                            "project_id": state.project_id,
                        }
                    },
                )
            )
            state.keyword_status = "succeeded"
            candidates.active_keyword = [
                (rank, record)
                for rank, record in enumerate(state.keyword_rows, 1)
                if _is_active_record(record)
                and _is_in_scope(
                    record,
                    session_id=state.session_id,
                    project_id=state.project_id,
                )
            ]
            candidates.keyword_candidates = [
                self._candidate(
                    record.id,
                    record.content,
                    record.score or _keyword_score(record.content, state.query),
                    {
                        **record.metadata,
                        "channel_rank": rank,
                        "channel_score": (
                            record.score
                            if record.score is not None
                            else _keyword_score(record.content, state.query)
                        ),
                    },
                )
                for rank, record in candidates.active_keyword
            ]
            state.processing_stage = "vector"
            if self._vector_indexer is not None:
                state.vector_rows = await self._vector_indexer.query(
                    state.query, state.limit
                )
                state.vector_status = "succeeded"
            candidates.vector_candidates = self._vector_candidates(state.vector_rows)
        return candidates

    async def _retrieve_directory(self, state: _RetrievalState) -> None:
        """读取并构建记忆目录；超时和普通异常记录状态，任务取消自然向上传播。

        参数：
        - `state`（_RetrievalState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        try:
            async with asyncio.timeout(
                _remaining_timeout(state.started, self._timeout_seconds)
            ):
                directory_records = await self._repository.list_scope_index(
                    session_id=state.session_id,
                    project_id=state.project_id,
                    limit=80,
                )
                state.directory_item = await build_memory_index_item(
                    self._repository,
                    directory_records,
                    session_id=state.session_id,
                    project_id=state.project_id,
                )
            state.directory_status = "succeeded" if state.directory_item else "skipped"
        except TimeoutError:
            state.directory_status = "timeout"
        except Exception:
            logger.exception(
                "Memory topic directory retrieval failed; session_id=%s",
                state.session_id,
            )
            state.directory_status = "failed"

    async def _resolve_candidate_records(
        self, state: _RetrievalState, candidates: _CandidateState
    ) -> None:
        """融合通道候选，并复核向量命中记录的有效性与作用域。

        参数：
        - `state`（_RetrievalState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。
        - `candidates`（_CandidateState）：各检索或计算阶段产生的候选项集合；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        inactive_ids = {
            record.id
            for record in state.keyword_rows
            if not _is_active_record(record)
        }
        candidates.vector_candidates = [
            candidate
            for candidate in candidates.vector_candidates
            if candidate.source_id not in inactive_ids
        ]
        candidates.fused = RetrievalRankFusion().fuse_ranked_candidates(
            candidates.keyword_candidates,
            candidates.vector_candidates,
            limit=None,
        )
        scoped_keyword_ids = {record.id for record in state.keyword_rows}
        candidates.record_by_id = {
            record.id: record for _, record in candidates.active_keyword
        }
        missing_records = [
            candidate.source_id
            for candidate in candidates.fused
            if candidate.source_id not in candidates.record_by_id
        ]
        if missing_records:
            record_query_limit = asyncio.Semaphore(4)

            async def fetch_record(memory_id: str) -> MemoryRecord | None:
                """按稳定标识读取记忆记录。

                参数：
                - `memory_id`（str）：目标记忆记录的稳定标识；约束：无额外运行时限制。

                返回：
                - MemoryRecord | None：具有状态、作用域、有效期和来源证据的记忆记录。

                异常：
                - 无预期业务异常；依赖调用产生的异常按当前实现传播。
                """
                async with record_query_limit:
                    return await self._repository.get(memory_id)

            async with asyncio.timeout(
                _remaining_timeout(state.started, self._timeout_seconds)
            ):
                fetched = await asyncio.gather(
                    *(fetch_record(memory_id) for memory_id in missing_records)
                )
            candidates.record_by_id.update(
                (record.id, record)
                for record in fetched
                if record is not None
                and _is_active_record(record)
                and _is_in_scope(
                    record,
                    session_id=state.session_id,
                    project_id=state.project_id,
                )
            )
        candidates.stale_candidates = [
            candidate
            for candidate in candidates.fused
            if candidate.source_id in scoped_keyword_ids
            and candidate.source_id not in candidates.record_by_id
        ]
        candidates.fused = [
            candidate
            for candidate in candidates.fused
            if candidate.source_id in candidates.record_by_id
        ]

    async def _load_fact_context(
        self, state: _RetrievalState, candidates: _CandidateState
    ) -> _FactContextState:
        """批量加载候选事实的相关记录，并合并相同身份的事实组。

        参数：
        - `state`（_RetrievalState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。
        - `candidates`（_CandidateState）：各检索或计算阶段产生的候选项集合；约束：无额外运行时限制。

        返回：
        - _FactContextState：记录当前流程数据和阶段信息的 `_FactContextState` 状态对象。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        facts = _FactContextState()
        facts_by_candidate: dict[str, MemoryFact] = {}
        fact_group_cache: dict[tuple[str, str, str, str], list[MemoryRecord]] = {}
        fact_requests: dict[tuple[str, str, str, str], MemoryFact] = {}
        for candidate in candidates.fused:
            fact = record_fact(candidates.record_by_id[candidate.source_id])
            if fact is None:
                continue
            identity = (
                fact.scope_kind,
                fact.scope_id,
                fact.subject_key,
                fact.fact_key,
            )
            facts_by_candidate[candidate.source_id] = fact
            fact_requests.setdefault(identity, fact)

        if fact_requests:
            fact_identities = tuple(fact_requests)
            group_query_limit = asyncio.Semaphore(4)

            async def fetch_fact_group(
                identity: tuple[str, str, str, str]
            ) -> list[MemoryRecord]:
                """按已校验的事实身份读取同一事实组中的记忆记录。

                参数：
                - `identity`（tuple[str, str, str, str]）：经校验的领域身份组合，用于确认操作范围；约束：无额外运行时限制。

                返回：
                - list[MemoryRecord]：符合条件的有序结果集合；集合成员。

                异常：
                - 无预期业务异常；依赖调用产生的异常按当前实现传播。
                """
                fact = fact_requests[identity]
                async with group_query_limit:
                    return await self._repository.list_fact_group(fact)

            async with asyncio.timeout(
                _remaining_timeout(state.started, self._timeout_seconds)
            ):
                related_groups = await asyncio.gather(
                    *(fetch_fact_group(identity) for identity in fact_identities)
                )
            fact_group_cache = dict(
                zip(fact_identities, related_groups, strict=True)
            )

        # 相同分组复用首个候选的渲染结果，扩展记录保留给 trace。
        canonical_groups: dict[str, MemoryFactContextGroup] = {}
        for memory_id, fact in facts_by_candidate.items():
            identity = (
                fact.scope_kind,
                fact.scope_id,
                fact.subject_key,
                fact.fact_key,
            )
            group = build_fact_context_group(
                candidates.record_by_id[memory_id],
                fact_group_cache[identity],
            )
            if group is not None:
                facts.groups_by_candidate[memory_id] = canonical_groups.setdefault(
                    group.group_key, group
                )
                for member in fact_group_cache[identity]:
                    facts.group_records[member.id] = member
        return facts

    def _select_context_items(
        self,
        plan: ContextPlan,
        state: _RetrievalState,
        candidates: _CandidateState,
        facts: _FactContextState,
    ) -> _SelectionState:
        """按融合顺序渲染记忆、合并重复事实组，并按条目上限选取结果。

        参数：
        - `plan`（ContextPlan）：当前计划对象，提供请求的检索或执行安排；约束：无额外运行时限制。
        - `state`（_RetrievalState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。
        - `candidates`（_CandidateState）：各检索或计算阶段产生的候选项集合；约束：无额外运行时限制。
        - `facts`（_FactContextState）：候选记忆对应的结构化事实及分组信息；约束：无额外运行时限制。

        返回：
        - _SelectionState：记录当前流程数据和阶段信息的 `_SelectionState` 状态对象。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        items: list[ContextItem] = []
        if state.directory_item is not None:
            items.append(state.directory_item)
        seen_group_keys: set[str] = set()
        for candidate in candidates.fused:
            record = candidates.record_by_id[candidate.source_id]
            group = facts.groups_by_candidate.get(candidate.source_id)
            if group is None:
                scope_metadata = {
                    key: record.metadata[key]
                    for key in ("category", "type", "session_id", "project_id")
                    if key in record.metadata
                }
                items.append(
                    ContextItem(
                        self.name,
                        candidate.content,
                        candidate.source_id,
                        {"fused_score": candidate.score, **scope_metadata},
                    )
                )
                continue

            if group.group_key in seen_group_keys:
                continue
            seen_group_keys.add(group.group_key)
            items.append(
                ContextItem(
                    self.name,
                    group.content,
                    group.anchor_id,
                    {
                        "fused_score": candidate.score,
                        "memory_group_key": group.group_key,
                        "memory_member_ids": list(group.member_ids),
                        "memory_conflict": group.conflicted,
                        "memory_total_count": group.total_member_count,
                        "memory_complete": group.complete,
                        "fact_cardinality": group.cardinality.value,
                        "memory_fact_identity": memory_domain_to_schema(
                            group.identity
                        ).model_dump(mode="json"),
                        "memory_group_member_value": group.member_value,
                    },
                )
            )
        # 目录占一个额外名额，事实组作为整体参与裁剪。
        selected_limit = min(
            max(0, self._limit) + (1 if state.directory_item is not None else 0),
            max(0, plan.max_items),
        )
        selected_items = items[:selected_limit]
        selected_group_members = {
            str(item.metadata["memory_group_key"]): set(
                item.metadata.get("memory_member_ids", ())
            )
            for item in selected_items
            if item.metadata.get("memory_group_key")
        }
        selected_plain_ids = {
            item.source_id
            for item in selected_items
            if item.source_id and not item.metadata.get("memory_group_key")
        }
        return _SelectionState(
            selected_items=selected_items,
            selected_limit=selected_limit,
            seen_group_keys=seen_group_keys,
            selected_group_members=selected_group_members,
            selected_plain_ids=selected_plain_ids,
        )

    def _build_candidate_traces(
        self,
        state: _RetrievalState,
        candidates: _CandidateState,
        facts: _FactContextState,
        selection: _SelectionState,
    ) -> dict[str, RetrievalCandidateTrace]:
        """汇总有效、失效、扩展事实组及目录候选的检索轨迹。

        参数：
        - `state`（_RetrievalState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。
        - `candidates`（_CandidateState）：各检索或计算阶段产生的候选项集合；约束：无额外运行时限制。
        - `facts`（_FactContextState）：候选记忆对应的结构化事实及分组信息；约束：无额外运行时限制。
        - `selection`（_SelectionState）：按上下文预算确定的选取结果及其依据；约束：无额外运行时限制。

        返回：
        - dict[str, RetrievalCandidateTrace]：统一格式的检索候选内容及其来源信息。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        trace_by_id: dict[str, RetrievalCandidateTrace] = {}
        self._add_fused_traces(trace_by_id, candidates, facts, selection)
        self._add_stale_traces(trace_by_id, candidates.stale_candidates)
        self._add_group_member_traces(trace_by_id, facts, selection)
        self._add_inactive_keyword_traces(trace_by_id, state)
        self._add_missing_vector_traces(trace_by_id, state.vector_rows)
        self._add_directory_trace(trace_by_id, state, selection.selected_items)
        return trace_by_id

    def _add_fused_traces(
        self,
        traces: dict[str, RetrievalCandidateTrace],
        candidates: _CandidateState,
        facts: _FactContextState,
        selection: _SelectionState,
    ) -> None:
        """为融合候选记录排名、有效元数据和上下文筛选原因。

        参数：
        - `traces`（dict[str, RetrievalCandidateTrace]）：记录候选来源、筛选过程及结果的追踪信息；约束：无额外运行时限制。
        - `candidates`（_CandidateState）：各检索或计算阶段产生的候选项集合；约束：无额外运行时限制。
        - `facts`（_FactContextState）：候选记忆对应的结构化事实及分组信息；约束：无额外运行时限制。
        - `selection`（_SelectionState）：按上下文预算确定的选取结果及其依据；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        for rank, candidate in enumerate(candidates.fused, 1):
            record = candidates.record_by_id[candidate.source_id]
            group = facts.groups_by_candidate.get(candidate.source_id)
            metadata = {
                **record.metadata,
                **{
                    key: value
                    for key, value in candidate.metadata.items()
                    if key not in {"hits", "rrf_score", "fused_rank"}
                },
            }
            if group is not None:
                metadata.update(
                    {
                        "memory_group_key": group.group_key,
                        "memory_member_ids": list(group.member_ids),
                        "memory_conflict": group.conflicted,
                        "memory_total_count": group.total_member_count,
                        "memory_complete": group.complete,
                    }
                )
            selected, filter_reason = self._fused_selection(
                candidate, group, selection
            )
            hits = self._candidate_hits(candidate)
            single_hit = hits[0] if len(hits) == 1 else None
            traces[candidate.source_id] = RetrievalCandidateTrace(
                provider=self.name,
                source_type="memory",
                source_id=candidate.source_id,
                revision_id=_optional_text(metadata.get("revision_id")),
                native_rank=single_hit.rank if single_hit else None,
                native_score=single_hit.score if single_hit else None,
                fused_rank=rank,
                fused_score=candidate.score,
                selected_for_result=selected,
                filter_reason=filter_reason,
                content_preview=candidate.content[:500],
                source_title=_optional_text(metadata.get("title")),
                metadata={
                    key: value
                    for key, value in metadata.items()
                    if key not in {"channel_rank", "channel_score"}
                },
                hits=hits,
                selection_stage=None if selected else "provider",
            )

    def _fused_selection(
        self,
        candidate: RetrievalCandidate,
        group: MemoryFactContextGroup | None,
        selection: _SelectionState,
    ) -> tuple[bool, str | None]:
        """判断融合候选是否已进入结果，并给出其组内筛选原因。

        参数：
        - `candidate`（RetrievalCandidate）：当前待融合、筛选或追踪的候选项；约束：无额外运行时限制。
        - `group`（MemoryFactContextGroup | None）：同一事实身份下合并得到的上下文分组；约束：允许为 None，按未提供值的分支处理。
        - `selection`（_SelectionState）：按上下文预算确定的选取结果及其依据；约束：无额外运行时限制。

        返回：
        - tuple[bool, str | None]：候选是否进入最终上下文及对应的筛选原因。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        if group is None:
            selected = candidate.source_id in selection.selected_plain_ids
            return selected, None if selected else "provider_top_k"
        if candidate.source_id in selection.selected_group_members.get(
            group.group_key, set()
        ):
            return True, None
        if candidate.source_id not in group.member_ids:
            return False, (
                "fact_group_limit" if group.conflicted else "duplicate_equivalent_fact"
            )
        return False, "provider_top_k"

    @staticmethod
    def _candidate_hits(
        candidate: RetrievalCandidate,
    ) -> tuple[RetrievalHitTrace, ...]:
        """将融合候选转换为检索命中追踪记录。

        参数：
        - `candidate`（RetrievalCandidate）：当前待融合、筛选或追踪的候选项；约束：无额外运行时限制。

        返回：
        - tuple[RetrievalHitTrace, ...]：记录单条检索命中的通道、名次和分数。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return tuple(
            RetrievalHitTraceSchema.model_validate(hit).to_domain()
            for hit in candidate.metadata.get("hits", [])
        )

    def _add_stale_traces(
        self,
        traces: dict[str, RetrievalCandidateTrace],
        stale_candidates: list[RetrievalCandidate],
    ) -> None:
        """保留已失效关键词记录的轨迹，说明它们被排除的原因。

        参数：
        - `traces`（dict[str, RetrievalCandidateTrace]）：记录候选来源、筛选过程及结果的追踪信息；约束：无额外运行时限制。
        - `stale_candidates`（list[RetrievalCandidate]）：已失效或未通过当前状态校验的候选集合；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        for rank, candidate in enumerate(stale_candidates, 1):
            if candidate.source_id in traces:
                continue
            hits = self._candidate_hits(candidate)
            traces[candidate.source_id] = RetrievalCandidateTrace(
                provider=self.name,
                source_type="memory",
                source_id=candidate.source_id,
                native_rank=hits[0].rank if len(hits) == 1 else None,
                native_score=hits[0].score if len(hits) == 1 else None,
                fused_rank=rank,
                fused_score=candidate.score,
                selected_for_result=False,
                filter_reason="inactive_memory",
                content_preview=candidate.content[:500],
                metadata=candidate.metadata,
                hits=hits,
                selection_stage="provider",
            )

    def _add_group_member_traces(
        self,
        traces: dict[str, RetrievalCandidateTrace],
        facts: _FactContextState,
        selection: _SelectionState,
    ) -> None:
        """为事实组展开带入的非融合成员补齐来源和筛选状态。

        参数：
        - `traces`（dict[str, RetrievalCandidateTrace]）：记录候选来源、筛选过程及结果的追踪信息；约束：无额外运行时限制。
        - `facts`（_FactContextState）：候选记忆对应的结构化事实及分组信息；约束：无额外运行时限制。
        - `selection`（_SelectionState）：按上下文预算确定的选取结果及其依据；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        for memory_id, record in facts.group_records.items():
            if memory_id in traces:
                continue
            group = next(
                (
                    item
                    for item in facts.groups_by_candidate.values()
                    if memory_id in item.all_member_ids
                ),
                None,
            )
            if group is None:
                continue
            selected = memory_id in selection.selected_group_members.get(
                group.group_key, set()
            )
            filter_reason = None if selected else (
                "fact_group_limit"
                if group.conflicted and memory_id not in group.member_ids
                else "provider_top_k"
            )
            traces[memory_id] = RetrievalCandidateTrace(
                provider=self.name,
                source_type="memory",
                source_id=memory_id,
                revision_id=_optional_text(record.metadata.get("revision_id")),
                selected_for_result=selected,
                filter_reason=filter_reason,
                content_preview=record.content[:500],
                source_title=_optional_text(record.metadata.get("title")),
                metadata={
                    **record.metadata,
                    "memory_group_key": group.group_key,
                    "memory_member_ids": list(group.member_ids),
                    "memory_conflict": group.conflicted,
                    "memory_total_count": group.total_member_count,
                    "memory_complete": group.complete,
                    "expanded_for_conflict": group.conflicted,
                },
                selection_stage=None if selected else "provider",
            )

    def _add_inactive_keyword_traces(
        self,
        traces: dict[str, RetrievalCandidateTrace],
        state: _RetrievalState,
    ) -> None:
        """记录关键词命中的失效记忆，并附上同源向量命中信息。

        参数：
        - `traces`（dict[str, RetrievalCandidateTrace]）：记录候选来源、筛选过程及结果的追踪信息；约束：无额外运行时限制。
        - `state`（_RetrievalState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        for rank, record in enumerate(state.keyword_rows, 1):
            if _is_active_record(record) or record.id in traces:
                continue
            vector_hit = next(
                (
                    RetrievalHitTrace(
                        channel="vector",
                        rank=vector_rank,
                        score=_score(row.get("score")),
                    )
                    for vector_rank, row in enumerate(state.vector_rows, 1)
                    if str(row.get("memory_id") or "") == record.id
                ),
                None,
            )
            hits = [
                RetrievalHitTrace(
                    channel="keyword",
                    rank=rank,
                    score=record.score or _keyword_score(record.content, state.query),
                )
            ]
            if vector_hit is not None:
                hits.append(vector_hit)
            traces[record.id] = RetrievalCandidateTrace(
                provider=self.name,
                source_type="memory",
                source_id=record.id,
                revision_id=_optional_text(record.metadata.get("revision_id")),
                native_rank=rank,
                native_score=hits[0].score,
                selected_for_result=False,
                filter_reason="inactive_memory",
                content_preview=record.content[:500],
                source_title=_optional_text(record.metadata.get("title")),
                metadata=record.metadata,
                hits=tuple(hits),
                selection_stage="provider",
            )

    def _add_missing_vector_traces(
        self,
        traces: dict[str, RetrievalCandidateTrace],
        vector_rows: list[dict[str, Any]],
    ) -> None:
        """为缺少记忆 ID 的向量命中生成可追踪的占位来源。

        参数：
        - `traces`（dict[str, RetrievalCandidateTrace]）：记录候选来源、筛选过程及结果的追踪信息；约束：无额外运行时限制。
        - `vector_rows`（list[dict[str, Any]]）：向量检索通道返回的原始行集合；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        for rank, row in enumerate(vector_rows, 1):
            if str(row.get("memory_id") or "").strip():
                continue
            source_id = f"missing_vector_id_{rank}"
            traces[source_id] = RetrievalCandidateTrace(
                provider=self.name,
                source_type="memory",
                source_id=source_id,
                revision_id=_optional_text(row.get("revision_id")),
                selected_for_result=False,
                filter_reason="missing_source_id",
                content_preview=str(row.get("content", ""))[:500] or None,
                hits=(
                    RetrievalHitTrace(
                        channel="vector",
                        rank=rank,
                        score=_score(row.get("score")),
                    ),
                ),
                selection_stage="provider",
            )

    def _add_directory_trace(
        self,
        traces: dict[str, RetrievalCandidateTrace],
        state: _RetrievalState,
        selected_items: list[ContextItem],
    ) -> None:
        """将记忆目录候选加入追踪并标记其最终选择状态。

        参数：
        - `traces`（dict[str, RetrievalCandidateTrace]）：记录候选来源、筛选过程及结果的追踪信息；约束：无额外运行时限制。
        - `state`（_RetrievalState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。
        - `selected_items`（list[ContextItem]）：已按上下文预算选取并准备返回的条目；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        directory_item = state.directory_item
        if directory_item is None:
            return
        selected = any(
            item.provider == directory_item.provider
            and item.source_id == directory_item.source_id
            for item in selected_items
        )
        source_id = directory_item.source_id or "memory-index"
        traces[source_id] = RetrievalCandidateTrace(
            provider="memory_index",
            source_type="memory_view",
            source_id=source_id,
            native_rank=1,
            fused_rank=1,
            fused_score=1.0,
            selected_for_result=selected,
            filter_reason=None if selected else "provider_top_k",
            content_preview=directory_item.content[:500],
            source_title="记忆概览与主题目录",
            metadata=directory_item.metadata,
            selection_stage=None if selected else "provider",
        )

    def _success_result(
        self,
        plan: ContextPlan,
        state: _RetrievalState,
        selection: _SelectionState,
        traces: dict[str, RetrievalCandidateTrace],
    ) -> ProviderResult:
        """根据选中条目和检索轨迹构造成功的提供方结果。

        参数：
        - `plan`（ContextPlan）：当前计划对象，提供请求的检索或执行安排；约束：无额外运行时限制。
        - `state`（_RetrievalState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。
        - `selection`（_SelectionState）：按上下文预算确定的选取结果及其依据；约束：无额外运行时限制。
        - `traces`（dict[str, RetrievalCandidateTrace]）：记录候选来源、筛选过程及结果的追踪信息；约束：无额外运行时限制。

        返回：
        - ProviderResult：包含检索状态、候选内容和追踪信息的提供方结果。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        items = tuple(selection.selected_items)
        conflict_count = sum(
            bool(item.metadata.get("memory_conflict")) for item in items
        )
        channels = (
            RetrievalChannelTrace(
                channel="keyword",
                status="succeeded",
                hit_count=len(state.keyword_rows),
            ),
            RetrievalChannelTrace(
                channel="vector",
                status=state.vector_status,
                hit_count=len(state.vector_rows),
            ),
            RetrievalChannelTrace(
                channel="memory_directory",
                status=state.directory_status,
                hit_count=(
                    int(
                        state.directory_item.metadata.get(
                            "memory_index_source_count", 0
                        )
                    )
                    if state.directory_item is not None
                    else 0
                ),
            ),
        )
        return ProviderResult(
            self.name,
            "succeeded",
            items,
            candidate_count=len(traces),
            result_count=sum(item.provider == "memory" for item in items),
            duration_ms=round((time.perf_counter() - state.started) * 1000),
            candidates=tuple(traces.values()),
            query=state.query,
            resource_scope={
                "memory_status": "active",
                "fact_group_count": len(selection.seen_group_keys),
                "conflict_group_count": conflict_count,
                "directory_status": state.directory_status,
            },
            effective_params={
                "recall_limit": state.limit,
                "result_limit": selection.selected_limit,
                "memory_token_budget": plan.memory_token_budget,
                "fact_group_count": len(selection.seen_group_keys),
                "conflict_group_count": conflict_count,
            },
            channels=channels,
        )

    def _failure_result(
        self, state: _RetrievalState, status: str, error: str
    ) -> ProviderResult:
        """按失败阶段修正通道状态，并返回已收集候选的诊断信息。

        参数：
        - `state`（_RetrievalState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。
        - `status`（str）：当前操作或记录的状态值；约束：无额外运行时限制。
        - `error`（str）：操作失败时捕获的异常或错误说明；约束：无额外运行时限制。

        返回：
        - ProviderResult：包含检索状态、候选内容和追踪信息的提供方结果。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        keyword_status = state.keyword_status
        vector_status = state.vector_status
        if state.processing_stage == "keyword":
            keyword_status = status
        elif self._vector_indexer is not None and state.vector_status != "succeeded":
            vector_status = status
        return self._failed_result(
            status=status,
            error=error,
            query=state.query,
            keyword_rows=state.keyword_rows,
            vector_rows=state.vector_rows,
            keyword_status=keyword_status,
            vector_status=vector_status,
            limit=state.limit,
            started=state.started,
        )

    def _vector_candidates(self, rows: list[dict[str, Any]]) -> list[RetrievalCandidate]:
        """把带记忆 ID 的向量结果转换为统一召回候选。

        参数：
        - `rows`（list[dict[str, Any]]）：从持久层或检索通道读取的原始记录；约束：无额外运行时限制。

        返回：
        - list[RetrievalCandidate]：符合条件的有序结果集合；集合成员。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        candidates: list[RetrievalCandidate] = []
        for rank, item in enumerate(rows, 1):
            memory_id = str(item.get("memory_id") or "").strip()
            if not memory_id:
                continue
            candidates.append(
                self._candidate(
                    memory_id,
                    str(item.get("content", "")),
                    _score(item.get("score")) or 0.0,
                    {
                        "channel_rank": rank,
                        "channel_score": _score(item.get("score")),
                    },
                )
            )
        return candidates

    def _failed_result(
        self,
        *,
        status: str,
        error: str,
        query: str,
        keyword_rows: list[Any],
        vector_rows: list[dict[str, Any]],
        keyword_status: str,
        vector_status: str,
        limit: int,
        started: float,
    ) -> ProviderResult:
        """在检索异常时返回失败状态及已取得的关键词、向量候选。

        参数：
        - `status`（str）：当前操作或记录的状态值；约束：无额外运行时限制。
        - `error`（str）：操作失败时捕获的异常或错误说明；约束：无额外运行时限制。
        - `query`（str）：用于检索、匹配或解析的原始查询文本；约束：无额外运行时限制。
        - `keyword_rows`（list[Any]）：关键词检索通道返回的原始候选记录；约束：无额外运行时限制。
        - `vector_rows`（list[dict[str, Any]]）：向量检索通道返回的原始行集合；约束：无额外运行时限制。
        - `keyword_status`（str）：关键词检索阶段的状态；约束：无额外运行时限制。
        - `vector_status`（str）：向量检索阶段的状态；约束：无额外运行时限制。
        - `limit`（int）：本次读取或处理的数量上限；约束：无额外运行时限制。
        - `started`（float）：本阶段开始时记录的单调时钟值，用于计算剩余时限；约束：无额外运行时限制。

        返回：
        - ProviderResult：包含检索状态、候选内容和追踪信息的提供方结果。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        candidates: dict[str, RetrievalCandidateTrace] = {}
        for rank, record in enumerate(keyword_rows, 1):
            vector_hits = [
                RetrievalHitTrace(
                    channel="vector",
                    rank=vector_rank,
                    score=_score(row.get("score")),
                )
                for vector_rank, row in enumerate(vector_rows, 1)
                if str(row.get("memory_id") or "") == record.id
            ]
            hits = [
                RetrievalHitTrace(
                    channel="keyword",
                    rank=rank,
                    score=record.score or _keyword_score(record.content, query),
                ),
                *vector_hits,
            ]
            candidates[record.id] = RetrievalCandidateTrace(
                provider=self.name,
                source_type="memory",
                source_id=record.id,
                revision_id=_optional_text(record.metadata.get("revision_id")),
                native_rank=rank,
                native_score=hits[0].score,
                selected_for_result=False,
                filter_reason=(
                    "inactive_memory"
                    if record.status != MemoryStatus.ACTIVE
                    else "provider_failed_before_selection"
                ),
                content_preview=record.content[:500],
                source_title=_optional_text(record.metadata.get("title")),
                metadata=record.metadata,
                hits=tuple(hits),
                selection_stage="provider",
            )
        known_ids = set(candidates)
        for rank, row in enumerate(vector_rows, 1):
            memory_id = str(row.get("memory_id") or "").strip()
            if not memory_id:
                source_id = f"missing_vector_id_{rank}"
                candidates[source_id] = RetrievalCandidateTrace(
                    provider=self.name,
                    source_type="memory",
                    source_id=source_id,
                    selected_for_result=False,
                    filter_reason="missing_source_id",
                    content_preview=str(row.get("content", ""))[:500] or None,
                    source_title=_optional_text(row.get("title")),
                    hits=(
                        RetrievalHitTrace(
                            channel="vector",
                            rank=rank,
                            score=_score(row.get("score")),
                        ),
                    ),
                    selection_stage="provider",
                )
                continue
            if memory_id in known_ids:
                continue
            candidates[memory_id] = RetrievalCandidateTrace(
                provider=self.name,
                source_type="memory",
                source_id=memory_id,
                revision_id=_optional_text(row.get("revision_id")),
                native_rank=rank,
                native_score=_score(row.get("score")),
                selected_for_result=False,
                filter_reason="provider_failed_before_selection",
                content_preview=str(row.get("content", ""))[:500],
                source_title=_optional_text(row.get("title")),
                hits=(
                    RetrievalHitTrace(
                        channel="vector",
                        rank=rank,
                        score=_score(row.get("score")),
                    ),
                ),
                selection_stage="provider",
            )
        channels = [
            RetrievalChannelTrace(
                channel="keyword",
                status=keyword_status,
                hit_count=len(keyword_rows),
            ),
        ]
        if self._vector_indexer is not None:
            channels.append(
                RetrievalChannelTrace(
                    channel="vector",
                    status=vector_status,
                    hit_count=len(vector_rows),
                    error_message=error if vector_status in {"failed", "timeout"} else None,
                )
            )
        return ProviderResult(
            self.name,
            status,
            error_message=error,
            candidate_count=len(candidates),
            result_count=0,
            duration_ms=round((time.perf_counter() - started) * 1000),
            candidates=tuple(candidates.values()),
            query=query,
            resource_scope={"memory_status": "active"},
            effective_params={"recall_limit": limit},
            channels=tuple(channels),
        )

    @staticmethod
    def _candidate(
        source_id: str, content: str, score: float, metadata: dict[str, Any]
    ) -> RetrievalCandidate:
        """创建带记忆来源标记的融合候选。

        参数：
        - `source_id`（str）：当前候选或记录的来源标识；约束：无额外运行时限制。
        - `content`（str）：待处理或返回的正文内容；约束：无额外运行时限制。
        - `score`（float）：候选项在当前检索通道中的相关性分数；约束：无额外运行时限制。
        - `metadata`（dict[str, Any]）：随领域记录保存的结构化附加字段；约束：无额外运行时限制。

        返回：
        - RetrievalCandidate：统一格式的检索候选内容及其来源信息。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return RetrievalCandidate(source_id, content, "memory", score, metadata)


def _score(value: Any) -> float | None:
    """将输入分数转换为有限、可比较的数值。

    参数：
    - `value`（Any）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - float | None：归一化后的有限相关性分数；输入缺失或非数值时返回 None。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_text(value: Any) -> str | None:
    """将可选输入规范化为非空文本或 None。

    参数：
    - `value`（Any）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - str | None：去除首尾空白的非空文本；输入不是字符串或为空时返回 None。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    text = str(value).strip() if value is not None else ""
    return text or None


def _remaining_timeout(started: float, timeout_seconds: float) -> float:
    """保持全文检索、向量检索和事实组展开共享一个总时限。

    参数：
    - `started`（float）：本阶段开始时记录的单调时钟值，用于计算剩余时限；约束：无额外运行时限制。
    - `timeout_seconds`（float）：本次操作允许使用的最长秒数；约束：无额外运行时限制。

    返回：
    - float：总时限扣除已用时间后的剩余秒数，最小为 0。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return max(0.0, timeout_seconds - (time.perf_counter() - started))


def _is_active_record(record: MemoryRecord) -> bool:
    """只允许当前有效且未到期的事实进入读取上下文。

    参数：
    - `record`（MemoryRecord）：当前领域记录，作为本次转换、筛选或渲染的输入；约束：无额外运行时限制。

    返回：
    - bool：操作是否成功或条件是否成立。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    if record.status != MemoryStatus.ACTIVE or record.validity_status != "valid":
        return False
    now = datetime.now(timezone.utc)
    # 将无时区时间按 UTC 解释；无法解析的时间按失效处理，避免过期事实被召回。
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
    if fact is not None and fact.valid_from is not None:
        try:
            valid_from = datetime.fromisoformat(fact.valid_from)
        except (TypeError, ValueError):
            return False
        if valid_from.tzinfo is None:
            valid_from = valid_from.replace(tzinfo=timezone.utc)
        if valid_from > now:
            return False
    return True


def _is_in_scope(
    record: MemoryRecord, *, session_id: str, project_id: str | None
) -> bool:
    """只允许全局、当前会话或明确绑定项目的记忆参与召回。

    参数：
    - `record`（MemoryRecord）：当前领域记录，作为本次转换、筛选或渲染的输入；约束：无额外运行时限制。
    - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。
    - `project_id`（str | None）：项目空间的稳定标识；None 表示未绑定项目；约束：允许为 None，按未提供值的分支处理。

    返回：
    - bool：操作是否成功或条件是否成立。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    fact = record_fact(record)
    if fact is not None:
        if fact.scope_kind == "global":
            return fact.scope_id == "global"
        if fact.scope_kind == "session":
            return fact.scope_id == session_id
        return project_id is not None and fact.scope_id == project_id
    category = record.metadata.get("category", record.metadata.get("type"))
    if category in {"preference", "profile"}:
        return True
    scope_kind = record.metadata.get("scope_kind")
    scope_id = record.metadata.get("scope_id")
    return (
        record.metadata.get("session_id") == session_id
        or (scope_kind == "global" and scope_id == "global")
        or (scope_kind == "session" and scope_id == session_id)
        or (project_id is not None and scope_kind == "project" and scope_id == project_id)
    )


__all__ = ["MemoryContextProvider"]
