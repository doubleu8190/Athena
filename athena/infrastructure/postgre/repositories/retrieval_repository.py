"""检索运行和候选轨迹的 SQLite 仓库。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select, update

from athena.core.retrieval.contracts import RetrievalCandidate, RetrievalRunRequest
from athena.infrastructure.postgre.engine import get_session
from athena.infrastructure.postgre.models import (
    RetrievalCandidateModel,
    RetrievalRunModel,
)
from athena.utils.id_generation import generate_time_id

from .repository_utils import _json_dumps, _json_loads, _now_iso


class RetrievalTraceRepository:
    """持久化检索运行、候选阶段和最终注入状态。"""

    async def start_run(self, request: RetrievalRunRequest) -> str:
        """创建检索运行记录。

        参数：
            request: 查询、检索范围及用于回放的配置。

        返回值：
            新建的检索运行 ID。

        异常：
            数据库写入失败时传播底层异常。
        """
        run_id = generate_time_id()
        now = _now_iso()
        row = RetrievalRunModel(
            run_id=run_id,
            query=request.query,
            scope=request.scope,
            status="running",
            config_json=_json_dumps(request.config),
            index_generation=request.index_generation,
            session_id=request.session_id,
            agent_run_id=request.agent_run_id,
            message_id=request.message_id,
            created_at=now,
        )
        async with get_session() as session:
            async with session.begin():
                session.add(row)
        return run_id

    async def record_candidates(
        self, run_id: str, candidates: Sequence[RetrievalCandidate]
    ) -> None:
        """追加一次检索运行的候选轨迹。

        参数：
            run_id: 所属检索运行 ID。
            candidates: 按 provider 或融合阶段排列的候选记录。

        返回值：
            None。

        异常：
            数据库写入失败时传播底层异常。
        """
        if not candidates:
            return
        now = _now_iso()
        rows = [
            RetrievalCandidateModel(
                candidate_id=generate_time_id(),
                run_id=run_id,
                provider=candidate.provider,
                stage=candidate.stage,
                source_type=candidate.source_type,
                source_id=candidate.source_id,
                logical_source_id=candidate.logical_source_id,
                revision_id=candidate.revision_id,
                document_version_id=candidate.document_version_id,
                native_rank=candidate.native_rank,
                native_score=candidate.native_score,
                fused_rank=candidate.fused_rank,
                fused_score=candidate.fused_score,
                filter_reason=candidate.filter_reason,
                selected_for_result=1 if candidate.selected_for_result else 0,
                injected_into_context=1 if candidate.injected_into_context else 0,
                content_preview=candidate.content_preview,
                source_title=candidate.source_title,
                locator_json=_json_dumps(candidate.locator),
                metadata_json=_json_dumps(candidate.metadata),
                created_at=now,
            )
            for candidate in candidates
        ]
        async with get_session() as session:
            async with session.begin():
                session.add_all(rows)

    async def complete_run(
        self,
        run_id: str,
        *,
        candidate_count: int,
        selected_count: int = 0,
        injected_count: int = 0,
        status: str = "succeeded",
        duration_ms: float | None = None,
        error_message: str | None = None,
    ) -> None:
        """写入检索运行的完成状态和统计。

        参数：
            run_id: 检索运行 ID。
            candidate_count: 融合候选数量。
            selected_count: 进入 provider 结果的候选数量。
            injected_count: 最终注入上下文的候选数量。
            status: ``succeeded``、``failed`` 或其他运行状态。
            duration_ms: 从启动到结束的耗时；不可用时为空。
            error_message: 失败、超时或部分成功时的错误说明。

        返回值：
            None。

        异常：
            数据库更新失败时传播底层异常。
        """
        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    update(RetrievalRunModel)
                    .where(RetrievalRunModel.run_id == run_id)
                    .values(
                        status=status,
                        candidate_count=candidate_count,
                        selected_count=selected_count,
                        injected_count=injected_count,
                        duration_ms=duration_ms,
                        error_message=error_message,
                        completed_at=_now_iso(),
                    )
                )

    async def mark_injected(self, run_id: str, source_ids: Sequence[str]) -> None:
        """标记最终进入上下文的融合候选。

        参数：
            run_id: 检索运行 ID。
            source_ids: 已通过最终上下文预算的不可变来源 ID。

        返回值：
            None。

        异常：
            数据库更新失败时传播底层异常。
        """
        ids = list(dict.fromkeys(source_ids))
        if not ids:
            return
        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    update(RetrievalCandidateModel)
                    .where(
                        RetrievalCandidateModel.run_id == run_id,
                        RetrievalCandidateModel.stage == "fused",
                        RetrievalCandidateModel.source_id.in_(ids),
                    )
                    .values(injected_into_context=1)
                )
                await session.execute(
                    update(RetrievalRunModel)
                    .where(RetrievalRunModel.run_id == run_id)
                    .values(injected_count=len(ids))
                )

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        """读取一次检索运行及其统计。

        参数：
            run_id: 检索运行 ID。

        返回值：
            运行字段字典；不存在时返回 ``None``。

        异常：
            数据库读取失败时传播底层异常。
        """
        async with get_session() as session:
            row = await session.get(RetrievalRunModel, run_id)
        if row is None:
            return None
        return self._run_payload(row)

    async def list_candidates(self, run_id: str) -> list[dict[str, Any]]:
        """按阶段和 rank 读取一次检索的全部候选轨迹。

        参数：
            run_id: 检索运行 ID。

        返回值：
            候选字段字典列表。

        异常：
            数据库读取失败时传播底层异常。
        """
        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        select(RetrievalCandidateModel).where(
                            RetrievalCandidateModel.run_id == run_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [
            {
                "candidate_id": row.candidate_id,
                "run_id": row.run_id,
                "provider": row.provider,
                "stage": row.stage,
                "source_type": row.source_type,
                "source_id": row.source_id,
                "logical_source_id": row.logical_source_id,
                "revision_id": row.revision_id,
                "document_version_id": row.document_version_id,
                "native_rank": row.native_rank,
                "native_score": row.native_score,
                "fused_rank": row.fused_rank,
                "fused_score": row.fused_score,
                "filter_reason": row.filter_reason,
                "selected_for_result": bool(row.selected_for_result),
                "injected_into_context": bool(row.injected_into_context),
                "content_preview": row.content_preview,
                "source_title": row.source_title,
                "locator": _json_loads(row.locator_json, {}),
                "metadata": _json_loads(row.metadata_json, {}),
                "created_at": row.created_at,
            }
            for row in sorted(
                rows,
                key=lambda item: (
                    item.stage,
                    item.fused_rank if item.fused_rank is not None else 10**9,
                    item.native_rank if item.native_rank is not None else 10**9,
                ),
            )
        ]

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
    ) -> tuple[list[dict[str, Any]], int]:
        """按筛选条件分页读取召回运行摘要。

        参数：
            limit: 返回数量，范围会被限制在 1 到 100。
            offset: 从第几条记录开始读取。
            scope: 可选的召回范围。
            status: 可选的运行状态。
            session_id: 可选的会话标识。
            agent_run_id: 可选的 Agent 运行标识。
            query: 可选的查询文本模糊匹配。

        返回值：
            ``(items, total)``，其中 items 按创建时间倒序排列。

        异常：
            数据库读取失败时传播底层异常。
        """
        limit = min(max(int(limit), 1), 100)
        offset = max(int(offset), 0)
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
            conditions.append(RetrievalRunModel.query.contains(query))

        async with get_session() as session:
            count_stmt = select(func.count()).select_from(RetrievalRunModel)
            if conditions:
                count_stmt = count_stmt.where(*conditions)
            total = int((await session.execute(count_stmt)).scalar_one())

            stmt = select(RetrievalRunModel).order_by(
                RetrievalRunModel.created_at.desc(), RetrievalRunModel.run_id.desc()
            )
            if conditions:
                stmt = stmt.where(*conditions)
            rows = (await session.execute(stmt.offset(offset).limit(limit))).scalars().all()

        return [self._run_payload(row) for row in rows], total

    @staticmethod
    def _run_payload(row: RetrievalRunModel) -> dict[str, Any]:
        """把 ORM 运行行转换成 API 使用的字典。"""
        return {
            "run_id": row.run_id,
            "query": row.query,
            "scope": row.scope,
            "status": row.status,
            "config": _json_loads(row.config_json, {}),
            "index_generation": row.index_generation,
            "session_id": row.session_id,
            "agent_run_id": row.agent_run_id,
            "message_id": row.message_id,
            "candidate_count": row.candidate_count,
            "selected_count": row.selected_count,
            "injected_count": row.injected_count,
            "duration_ms": row.duration_ms,
            "error_message": row.error_message,
            "created_at": row.created_at,
            "completed_at": row.completed_at,
        }

    async def ranked_source_ids(self, run_id: str, limit: int | None = None) -> list[str]:
        """读取融合阶段的候选 ID，供离线评估或回放使用。

        参数：
            run_id: 检索运行 ID。
            limit: 可选的最大候选数量。

        返回值：
            按融合 rank 排列的不可变来源 ID。

        异常：
            数据库读取失败时传播底层异常。
        """
        candidates = [
            item
            for item in await self.list_candidates(run_id)
            if item["stage"] == "fused" and not item["filter_reason"]
        ]
        candidates.sort(
            key=lambda item: item["fused_rank"]
            if item["fused_rank"] is not None
            else 10**9
        )
        source_ids = [item["source_id"] for item in candidates]
        return source_ids if limit is None else source_ids[:limit]
