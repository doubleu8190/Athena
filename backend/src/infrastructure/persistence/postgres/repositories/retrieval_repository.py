"""PostgreSQL retrieval trace query and recording implementation."""
from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, cast, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from domain.retrieval.query_ports import RetrievalPort

from ..models import (
    RetrievalCandidateModel,
    RetrievalRunModel,
)

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return default
    return parsed

class PostgresRetrievalRepository(RetrievalPort):
    def __init__(
        self, session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    ) -> None:
        self._session_factory = session_factory

    async def list_runs(self, **filters):
        async with self._session_factory() as session:
            query = (
                select(RetrievalRunModel)
                .order_by(RetrievalRunModel.created_at.desc())
                .offset(filters.get("offset", 0))
                .limit(filters.get("limit", 30))
            )
            if filters.get("scope"):
                query = query.where(RetrievalRunModel.scope == filters["scope"])
            if filters.get("status"):
                query = query.where(RetrievalRunModel.status == filters["status"])
            rows = (await session.execute(query)).scalars().all()
            total = (
                await session.scalar(
                    select(func.count()).select_from(RetrievalRunModel)
                )
                or 0
            )
        return [self._row(row) for row in rows], int(total)

    async def get_run(self, run_id: str):
        async with self._session_factory() as session:
            row = await session.get(RetrievalRunModel, run_id)
            candidates = [] if row is None else (await session.execute(
                select(RetrievalCandidateModel)
                .where(RetrievalCandidateModel.run_id == run_id)
                .order_by(RetrievalCandidateModel.fused_rank, RetrievalCandidateModel.created_at)
            )).scalars().all()
        if row is None:
            return None
        value = self._row(row)
        value["candidates"] = [self._candidate_row(candidate) for candidate in candidates]
        return value

    async def record_context(self, *, session_id: str, agent_run_id: str | None, query: str, provider_results: list[Any], items: list[Any]) -> str:
        """记录一次上下文召回及最终注入条目。"""
        import uuid
        run_id = f"retrieval_{uuid.uuid4().hex}"
        now = _now()
        candidate_count = sum(int(getattr(item, "candidate_count", 0)) for item in provider_results)
        async with self._session_factory() as session:
            async with session.begin():
                session.add(RetrievalRunModel(
                    run_id=run_id, query=query, scope="context", status="succeeded",
                    config_json=json.dumps({"providers": [item.provider for item in provider_results]}),
                    session_id=session_id, agent_run_id=agent_run_id,
                    candidate_count=max(candidate_count, len(items)), selected_count=len(items),
                    injected_count=len(items), duration_ms=None, created_at=now, completed_at=now,
                ))
                for rank, item in enumerate(items, 1):
                    session.add(RetrievalCandidateModel(
                        candidate_id=f"candidate_{uuid.uuid4().hex}", run_id=run_id,
                        provider=item.provider, stage="context", source_type=item.provider,
                        source_id=item.source_id or f"inline_{rank}", fused_rank=rank,
                        fused_score=float(item.metadata.get("score", 0.0)),
                        selected_for_result=1, injected_into_context=1,
                        content_preview=item.content[:500], locator_json="{}",
                        metadata_json=json.dumps(item.metadata, ensure_ascii=False), created_at=now,
                    ))
        return run_id

    @staticmethod
    def _row(row):
        return {
            "run_id": row.run_id,
            "query": row.query,
            "scope": row.scope,
            "status": row.status,
            "config": _json(row.config_json, {}),
            "candidate_count": row.candidate_count,
            "selected_count": row.selected_count,
            "injected_count": row.injected_count,
            "created_at": row.created_at,
            "completed_at": row.completed_at,
        }

    @staticmethod
    def _candidate_row(row):
        return {
            "candidate_id": row.candidate_id,
            "provider": row.provider,
            "stage": row.stage,
            "source_type": row.source_type,
            "source_id": row.source_id,
            "native_rank": row.native_rank,
            "native_score": row.native_score,
            "fused_rank": row.fused_rank,
            "fused_score": row.fused_score,
            "selected_for_result": bool(row.selected_for_result),
            "injected_into_context": bool(row.injected_into_context),
            "content_preview": row.content_preview,
            "metadata": _json(row.metadata_json, {}),
        }

__all__ = ["PostgresRetrievalRepository"]
