"""PostgreSQL retrieval trace query and recording implementation."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from domain.retrieval.query_ports import RetrievalPort
from domain.retrieval.trace_contracts import (
    RetrievalCandidateSelection,
    RetrievalCandidateTrace,
    RetrievalProviderTrace,
    RetrievalRunTrace,
)
from application.contracts.retrieval import (
    RetrievalCandidateTraceSchema,
    RetrievalProviderTraceSchema,
    RetrievalRunTraceSchema,
)

from ..models import RetrievalCandidateModel, RetrievalRunModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PostgresRetrievalRepository(RetrievalPort):
    def __init__(
        self, session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    ) -> None:
        self._session_factory = session_factory

    async def list_runs(
        self,
        *,
        limit: int = 30,
        offset: int = 0,
        scope: str | None = None,
        status: str | None = None,
        session_id: str | None = None,
        agent_run_id: str | None = None,
        query: str | None = None,
        provider: str | None = None,
    ) -> tuple[list[dict[str, object]], int]:
        conditions = []
        if scope:
            conditions.append(RetrievalRunModel.scope == scope)
        if status:
            conditions.append(RetrievalRunModel.status == status)
        if session_id:
            conditions.append(RetrievalRunModel.session_id == session_id)
        if agent_run_id:
            conditions.append(RetrievalRunModel.agent_run_id == agent_run_id)
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append(
                RetrievalRunModel.query.ilike(f"%{escaped}%", escape="\\")
            )
        if provider:
            providers_json = RetrievalRunModel.config_json["providers"]
            conditions.append(providers_json.contains([{"provider": provider}]))

        async with self._session_factory() as session:
            filtered = select(RetrievalRunModel).where(*conditions)
            rows = (
                await session.execute(
                    filtered
                    .order_by(
                        RetrievalRunModel.created_at.desc(),
                        RetrievalRunModel.run_id.desc(),
                    )
                    .offset(offset)
                    .limit(limit)
                )
            ).scalars().all()
            total = await session.scalar(
                select(func.count()).select_from(RetrievalRunModel).where(*conditions)
            )
        return [self._row(row) for row in rows], int(total or 0)

    async def get_run(self, run_id: str) -> dict[str, object] | None:
        async with self._session_factory() as session:
            row = await session.get(RetrievalRunModel, run_id)
            if row is None:
                return None
            candidates = (
                await session.execute(
                    select(RetrievalCandidateModel)
                    .where(RetrievalCandidateModel.run_id == run_id)
                    .order_by(
                        RetrievalCandidateModel.provider,
                        RetrievalCandidateModel.fused_rank.asc().nulls_last(),
                        RetrievalCandidateModel.source_id,
                    )
                )
            ).scalars().all()
            value = self._row(row)
            value["candidates"] = [
                self._candidate_row(candidate) for candidate in candidates
            ]
        return value

    async def record_context(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_run_id: str | None,
        message_id: str | None,
        query: str,
        providers: tuple[RetrievalProviderTrace, ...],
        candidates: tuple[RetrievalCandidateTrace, ...],
        duration_ms: float,
        status: str,
        error_message: str | None,
    ) -> str:
        """同一事务写入召回父记录和全部模块候选。"""
        now = _now()
        raw_hit_count = sum(
            channel.hit_count for provider in providers for channel in provider.channels
        )
        run_trace = RetrievalRunTrace(
            raw_hit_count=raw_hit_count,
            providers=providers,
            selection_status="pending",
            context_count=None,
        )
        run_values = {
            "run_id": run_id,
            "query": query,
            "scope": "context",
            "status": status,
            "config_json": RetrievalRunTraceSchema.from_domain(run_trace).model_dump(
                mode="json"
            ),
            "session_id": session_id,
            "agent_run_id": agent_run_id,
            "message_id": message_id,
            "candidate_count": len(candidates),
            "selected_count": sum(provider.selected_count for provider in providers),
            "injected_count": 0,
            "duration_ms": duration_ms,
            "error_message": error_message,
            "created_at": now,
            "completed_at": now,
        }
        candidate_rows = [
            self._candidate_model(run_id, candidate, now) for candidate in candidates
        ]
        async with self._session_factory() as session:
            async with session.begin():
                inserted = await session.execute(
                    insert(RetrievalRunModel)
                    .values(**run_values)
                    .on_conflict_do_nothing(index_elements=[RetrievalRunModel.run_id])
                    .returning(RetrievalRunModel.run_id)
                )
                if inserted.scalar_one_or_none() is None:
                    return run_id
                # 先确保父记录已写入，候选的外键才会在同一事务中生效。
                await session.flush()
                session.add_all(candidate_rows)
                await session.flush()
        return run_id

    async def update_context_selection(
        self,
        *,
        run_id: str,
        selections: tuple[RetrievalCandidateSelection, ...],
    ) -> bool:
        """回写本次新候选在 Root 最终资料窗口中的选择结果。"""
        selection_by_key = {
            (selection.provider, source_id): selection
            for selection in selections
            for source_id in (selection.source_ids or (selection.source_id,))
        }
        async with self._session_factory() as session:
            async with session.begin():
                run = await session.get(RetrievalRunModel, run_id)
                if run is None:
                    return False
                candidates = (
                    await session.execute(
                        select(RetrievalCandidateModel).where(
                            RetrievalCandidateModel.run_id == run_id
                        )
                    )
                ).scalars().all()
                for row in candidates:
                    key = (row.provider, row.source_id)
                    selection = selection_by_key.get(key)
                    metadata = row.metadata_json
                    if not isinstance(metadata, dict):
                        metadata = {}
                    analysis = metadata.get("retrieval_analysis", {})
                    if not isinstance(analysis, dict):
                        analysis = {}
                    context_selected = bool(
                        selection and selection.context_selected
                    )
                    stage = analysis.get("selection_stage")
                    reason = row.filter_reason
                    if selection is not None and not context_selected:
                        stage = "window"
                        reason = reason or selection.filter_reason or "not_in_context_window"
                    elif context_selected:
                        stage = "window"
                    elif row.selected_for_result and not reason:
                        stage = "window"
                        reason = "not_in_context_window"
                    analysis.update(
                        {
                            "context_selected": context_selected,
                            "selection_stage": stage,
                        }
                    )
                    metadata["retrieval_analysis"] = analysis
                    row.metadata_json = metadata
                    row.filter_reason = reason
                    row.injected_into_context = False

                trace_data = run.config_json
                trace = RetrievalRunTraceSchema.model_validate(trace_data).to_domain()
                context_count = sum(
                    selection.context_selected for selection in selections
                )
                run.config_json = RetrievalRunTraceSchema.from_domain(
                    replace(
                        trace,
                        selection_status="complete",
                        context_count=context_count,
                    )
                ).model_dump(mode="json")
                await session.flush()
        return True

    @staticmethod
    def _candidate_model(
        run_id: str,
        candidate: RetrievalCandidateTrace,
        created_at: datetime,
    ) -> RetrievalCandidateModel:
        candidate_data = RetrievalCandidateTraceSchema.from_domain(
            candidate
        ).model_dump(mode="json")
        metadata = dict(candidate_data["metadata"])
        metadata["retrieval_analysis"] = {
            "hits": candidate_data["hits"],
            "context_selected": candidate_data["context_selected"],
            "selection_stage": candidate_data["selection_stage"],
        }
        return RetrievalCandidateModel(
            candidate_id=f"candidate_{uuid.uuid4().hex}",
            run_id=run_id,
            provider=candidate.provider,
            stage="context",
            source_type=candidate.source_type,
            source_id=candidate.source_id,
            logical_source_id=candidate.logical_source_id,
            revision_id=candidate.revision_id,
            native_rank=candidate.native_rank,
            native_score=candidate.native_score,
            fused_rank=candidate.fused_rank,
            fused_score=candidate.fused_score,
            rerank_rank=candidate.rerank_rank,
            rerank_score=candidate.rerank_score,
            filter_reason=candidate.filter_reason,
            selected_for_result=candidate.selected_for_result,
            injected_into_context=False,
            content_preview=candidate.content_preview,
            source_title=candidate.source_title,
            locator_json=candidate_data["locator"],
            metadata_json=metadata,
            created_at=created_at,
        )

    @staticmethod
    def _row(row: RetrievalRunModel) -> dict[str, object]:
        config = row.config_json
        trace: RetrievalRunTrace | None = None
        try:
            trace = RetrievalRunTraceSchema.model_validate(config).to_domain()
        except ValidationError:
            # 开发环境中的旧记录没有本版 JSON 结构，直接标为资料不完整。
            trace = None
        return {
            "run_id": row.run_id,
            "session_id": row.session_id,
            "agent_run_id": row.agent_run_id,
            "message_id": row.message_id,
            "query": row.query,
            "scope": row.scope,
            "status": row.status,
            "trace_status": (
                "complete"
                if trace is not None and trace.selection_status == "complete"
                else "incomplete"
            ),
            "raw_hit_count": None if trace is None else trace.raw_hit_count,
            "candidate_count": row.candidate_count,
            "selected_count": row.selected_count,
            "context_count": None if trace is None else trace.context_count,
            "selection_status": (
                "incomplete" if trace is None else trace.selection_status
            ),
            "providers": (
                []
                if trace is None
                else [
                    RetrievalProviderTraceSchema.from_domain(provider).model_dump(
                        mode="json"
                    )
                    for provider in trace.providers
                ]
            ),
            "config": config if isinstance(config, dict) else {},
            "index_generation": row.index_generation,
            "duration_ms": row.duration_ms,
            "error_message": row.error_message,
            "created_at": row.created_at.isoformat(),
            "completed_at": (
                row.completed_at.isoformat() if row.completed_at is not None else None
            ),
        }

    @staticmethod
    def _candidate_row(row: RetrievalCandidateModel) -> dict[str, object]:
        metadata = row.metadata_json
        if not isinstance(metadata, dict):
            metadata = {}
        analysis = metadata.get("retrieval_analysis", {})
        if not isinstance(analysis, dict):
            analysis = {}
        return {
            "candidate_id": row.candidate_id,
            "run_id": row.run_id,
            "provider": row.provider,
            "stage": row.stage,
            "source_type": row.source_type,
            "source_id": row.source_id,
            "logical_source_id": row.logical_source_id,
            "revision_id": row.revision_id,
            "native_rank": row.native_rank,
            "native_score": row.native_score,
            "fused_rank": row.fused_rank,
            "fused_score": row.fused_score,
            "rerank_rank": row.rerank_rank,
            "rerank_score": row.rerank_score,
            "selected_for_result": row.selected_for_result,
            "filter_reason": row.filter_reason,
            "content_preview": row.content_preview,
            "source_title": row.source_title,
            "locator": row.locator_json,
            "hits": analysis.get("hits", []),
            "context_selected": analysis.get("context_selected"),
            "selection_stage": analysis.get("selection_stage"),
            "metadata": metadata,
            "created_at": row.created_at.isoformat(),
        }


__all__ = ["PostgresRetrievalRepository"]
