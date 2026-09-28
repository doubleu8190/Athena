"""Graph path and evidence context provider."""

from __future__ import annotations

import asyncio
import time

from athena.core.graph.contracts import GraphPathCandidate
from athena.core.graph.ports import GraphStore
from athena.core.retrieval.contracts import RetrievalCandidate, RetrievalRunRequest
from athena.core.retrieval.ports import RetrievalTraceWriter
from athena.infrastructure.postgre.repositories.file_repository import FileRepository
from athena.runtime.context.contracts import ContextItem, ContextPlan, ProviderResult
from athena.runtime.task_understanding.contracts import UserTaskSpec
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class GraphContextProvider:
    """Retrieve bounded Neo4j paths and resolve them to live chunk evidence."""

    name = "graph"

    def __init__(
        self,
        *,
        graph_store: GraphStore,
        file_repository: FileRepository,
        trace_writer: RetrievalTraceWriter | None = None,
        timeout_seconds: float = 5.0,
        min_confidence: float = 0.65,
    ) -> None:
        self._graph_store = graph_store
        self._file_repository = file_repository
        self._trace_writer = trace_writer
        self._timeout_seconds = timeout_seconds
        self._min_confidence = min_confidence

    async def acquire(
        self,
        *,
        session_id: str,
        agent_run_id: str | None = None,
        message_id: str | None = None,
        task: UserTaskSpec,
        plan: ContextPlan,
    ) -> ProviderResult:
        started = time.perf_counter()
        query = (plan.graph_query or task.goal).strip()
        if not query:
            return ProviderResult(provider=self.name, status="succeeded")
        run_id: str | None = None
        try:
            if self._trace_writer is not None:
                run_id = await self._trace_writer.start_run(
                    RetrievalRunRequest(
                        query=query,
                        scope="graph",
                        config={
                            "max_hops": plan.graph_max_hops,
                            "entity_limit": plan.graph_entity_limit,
                            "path_limit": plan.graph_path_limit,
                        },
                        index_generation="neo4j/graph-v1",
                        session_id=session_id,
                        agent_run_id=agent_run_id,
                        message_id=message_id,
                    )
                )
            async with asyncio.timeout(self._timeout_seconds):
                paths = await self._graph_store.search_paths(
                    query=query,
                    knowledge_base_ids=plan.knowledge_base_ids or None,
                    max_hops=plan.graph_max_hops,
                    entity_limit=plan.graph_entity_limit,
                    path_limit=plan.graph_path_limit,
                    min_confidence=self._min_confidence,
                )
                chunks = await self._file_repository.get_chunks_by_ids(
                    [chunk_id for path in paths for chunk_id in path.evidence_chunk_ids],
                    knowledge_base_ids=plan.knowledge_base_ids or None,
                )
            chunks_by_id = {chunk.id: chunk for chunk in chunks}
            items = [
                item
                for path in paths
                for item in [self._to_context_item(path, chunks_by_id, run_id)]
                if item is not None
            ]
            await self._record_trace(run_id, paths, started, len(items), None)
            return ProviderResult(
                provider=self.name,
                status="succeeded",
                items=items,
                candidate_count=len(paths),
                result_count=len(items),
                duration_ms=round((time.perf_counter() - started) * 1000),
            )
        except TimeoutError:
            await self._record_trace(run_id, [], started, 0, "graph retrieval timed out")
            return ProviderResult(
                provider=self.name,
                status="timeout",
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_message="graph retrieval timed out",
            )
        except Exception as exc:
            logger.warning("graph_context_provider_failed", error=str(exc))
            await self._record_trace(run_id, [], started, 0, str(exc))
            return ProviderResult(
                provider=self.name,
                status="failed",
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_message=str(exc),
            )

    @staticmethod
    def _to_context_item(
        path: GraphPathCandidate,
        chunks_by_id: dict[str, object],
        run_id: str | None,
    ) -> ContextItem | None:
        path_text = " -> ".join(path.entity_names)
        relation_text = " -> ".join(path.relation_types)
        evidence_parts: list[str] = []
        locator: dict[str, object] = {}
        for chunk_id in path.evidence_chunk_ids:
            chunk = chunks_by_id.get(chunk_id)
            if chunk is None:
                continue
            evidence_parts.append(f"[{chunk_id}] {getattr(chunk, 'content', '')}")
            if not locator:
                raw_locator = getattr(chunk, "locator", {})
                locator = (
                    raw_locator.model_dump(mode="json", exclude_none=True)
                    if hasattr(raw_locator, "model_dump")
                    else dict(raw_locator)
                )
        if not evidence_parts:
            return None
        content = f"[Graph Path]\n{path_text}\n[Relations]\n{relation_text}"
        if evidence_parts:
            content += "\n[Evidence]\n" + "\n".join(evidence_parts)
        return ContextItem(
            provider="graph",
            content=content,
            source_id=path.path_id,
            retrieval_run_id=run_id,
            locator=locator,
            score=path.score,
            metadata={
                "knowledge_base_id": path.knowledge_base_id,
                "entity_keys": path.entity_keys,
                "relation_types": path.relation_types,
                "evidence_chunk_ids": path.evidence_chunk_ids,
                "hop_count": path.hop_count,
            },
        )

    async def _record_trace(
        self,
        run_id: str | None,
        paths: list[GraphPathCandidate],
        started: float,
        selected_count: int,
        error_message: str | None,
    ) -> None:
        if self._trace_writer is None or run_id is None:
            return
        candidates = [
            RetrievalCandidate(
                provider="graph",
                stage="fused",
                source_type="path",
                source_id=path.path_id,
                native_score=path.score,
                native_rank=index,
                selected_for_result=True,
                content_preview=" -> ".join(path.entity_names),
                metadata={
                    "knowledge_base_id": path.knowledge_base_id,
                    "evidence_chunk_ids": path.evidence_chunk_ids,
                    "relation_types": path.relation_types,
                    "hop_count": path.hop_count,
                },
            )
            for index, path in enumerate(paths, 1)
        ]
        try:
            await self._trace_writer.record_candidates(run_id, candidates)
            await self._trace_writer.complete_run(
                run_id,
                candidate_count=len(paths),
                selected_count=selected_count,
                status="partial" if error_message else "succeeded",
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_message=error_message,
            )
        except Exception as exc:
            logger.warning("graph_retrieval_trace_failed", error=str(exc))
