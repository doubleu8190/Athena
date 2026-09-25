from __future__ import annotations

import pytest

from athena.core.retrieval import (
    RetrievalCandidate,
    RetrievalEvaluationCase,
    RetrievalRunRequest,
    evaluate_rankings,
)
from athena.infrastructure.postgre.database import Database
from athena.models.file import FileChunk


@pytest.mark.asyncio
async def test_retrieval_trace_persists_candidates_and_injection(tmp_path):
    db = Database(str(tmp_path / "trace.db"))
    await db.connect()
    try:
        run_id = await db.retrieval.start_run(
            RetrievalRunRequest(
                query="偏好什么语言",
                scope="memory",
                config={"candidate_k": 10},
                index_generation="memory/current",
                session_id="session-1",
                agent_run_id="agent-run-1",
                message_id="message-1",
            )
        )
        await db.retrieval.record_candidates(
            run_id,
            [
                RetrievalCandidate(
                    provider="fusion",
                    stage="fused",
                    source_type="memory_revision",
                    source_id="memory-r2",
                    logical_source_id="memory-1",
                    revision_id="memory-r2",
                    fused_rank=1,
                    fused_score=0.42,
                    rerank_rank=1,
                    rerank_score=0.91,
                    selected_for_result=True,
                    content_preview="用户偏好 Python",
                    locator={"turn": 3},
                )
            ],
        )
        await db.retrieval.complete_run(
            run_id, candidate_count=1, selected_count=1
        )
        await db.retrieval.mark_injected(run_id, ["memory-r2"])

        # The retrieval trace has its own ID.  A single Agent run can produce
        # multiple traces when it queries more than one provider.
        second_run_id = await db.retrieval.start_run(
            RetrievalRunRequest(
                query="知识库中的相关内容",
                scope="knowledge",
                session_id="session-1",
                agent_run_id="agent-run-1",
                message_id="message-1",
            )
        )
        assert second_run_id != run_id
        agent_runs, agent_total = await db.retrieval.list_runs(
            agent_run_id="agent-run-1"
        )
        assert agent_total == 2
        assert {item["run_id"] for item in agent_runs} == {run_id, second_run_id}

        run = await db.retrieval.get_run(run_id)
        candidates = await db.retrieval.list_candidates(run_id)
        assert run is not None
        assert run["status"] == "succeeded"
        assert run["injected_count"] == 1
        assert run["session_id"] == "session-1"
        assert run["agent_run_id"] == "agent-run-1"
        assert run["message_id"] == "message-1"
        assert candidates[0]["revision_id"] == "memory-r2"
        assert candidates[0]["rerank_rank"] == 1
        assert candidates[0]["rerank_score"] == pytest.approx(0.91)
        assert candidates[0]["content_preview"] == "用户偏好 Python"
        assert candidates[0]["locator"] == {"turn": 3}
        assert candidates[0]["injected_into_context"] is True

        runs, total = await db.retrieval.list_runs(
            session_id="session-1", scope="memory", query="偏好"
        )
        assert total == 1
        assert runs[0]["run_id"] == run_id
    finally:
        await db.close()


def test_retrieval_metrics_use_immutable_evidence_ids():
    metrics = evaluate_rankings(
        [
            RetrievalEvaluationCase.from_ids("q1", ["memory-r2"]),
            RetrievalEvaluationCase.from_ids("q2", ["chunk-v3"]),
        ],
        {
            "q1": ["memory-r1", "memory-r2"],
            "q2": ["chunk-v3"],
        },
        ks=(1, 2),
    )

    assert metrics.recall_at_k[1] == 0.5
    assert metrics.recall_at_k[2] == 1.0
    assert metrics.mrr == 0.75


@pytest.mark.asyncio
async def test_file_chunk_replacement_soft_deletes_previous_chunks(tmp_path):
    db = Database(str(tmp_path / "versions.db"))
    await db.connect()
    try:
        attachment = await db.files.create_attachment(
            knowledge_base_id="kb-1",
            filename="guide.txt",
            mime_type="text/plain",
            size_bytes=10,
            sha256="a" * 64,
            storage_key="blobs/a",
        )
        await db.files.replace_chunks(
            attachment.id,
            [
                FileChunk(
                    id="chunk-v1",
                    attachment_id=attachment.id,
                    ordinal=0,
                    content="旧内容",
                )
            ],
        )
        await db.files.replace_chunks(
            attachment.id,
            [
                FileChunk(
                    id="chunk-v2",
                    attachment_id=attachment.id,
                    ordinal=0,
                    content="新内容",
                )
            ],
        )

        current = await db.files.get_chunks(attachment.id)
        assert [chunk.id for chunk in current] == ["chunk-v2"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_same_knowledge_filename_creates_a_new_logical_document_version(tmp_path):
    db = Database(str(tmp_path / "document-versions.db"))
    await db.connect()
    try:
        first = await db.files.create_attachment(
            knowledge_base_id="kb-1",
            filename="guide.txt",
            mime_type="text/plain",
            size_bytes=3,
            sha256="b" * 64,
            storage_key="blobs/b1",
        )
        second = await db.files.create_attachment(
            knowledge_base_id="kb-1",
            filename="guide.txt",
            mime_type="text/plain",
            size_bytes=3,
            sha256="c" * 64,
            storage_key="blobs/c1",
            logical_document_id=first.logical_document_id,
        )
        await db.files.replace_chunks(
            first.id,
            [
                FileChunk(
                    id="old-guide-chunk",
                    attachment_id=first.id,
                    ordinal=0,
                    content="old guide content",
                )
            ],
        )
        await db.files.update_attachment(first.id, status="ready")
        await db.files.replace_chunks(
            second.id,
            [
                FileChunk(
                    id="new-guide-chunk",
                    attachment_id=second.id,
                    ordinal=0,
                    content="new guide content",
                )
            ],
        )
        await db.files.update_attachment(second.id, status="ready")

        current = await db.files.list_global_knowledge_documents()
        assert [item.id for item in current] == [second.id]
        assert second.document_version == first.document_version + 1
        assert second.logical_document_id == first.logical_document_id
        search = await db.files.search_knowledge_chunks(
            "guide", {second.id}, limit=10
        )
        assert [chunk.id for chunk in search] == ["new-guide-chunk"]
        assert search[0].native_score is not None
        assert search[0].native_score < 0
    finally:
        await db.close()
