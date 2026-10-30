"""Coverage for the complete target ORM table inventory."""

from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import JSONB

from infrastructure.persistence.postgres.models import Base, MemoryModel


EXPECTED_TABLES = {
    "embedding_metadata", "agent_runs", "idempotency_receipts", "agent_plans", "agent_tasks",
    "agent_events", "approvals", "sessions", "messages", "steps", "tool_calls",
    "memories", "mcp_servers", "tools", "knowledge_bases", "attachments",
    "knowledge_document_jobs", "file_chunks", "file_artifacts", "adapter_registry",
    "retrieval_runs", "retrieval_candidates",
}


def test_target_orm_covers_all_deployed_tables() -> None:
    assert EXPECTED_TABLES <= set(Base.metadata.tables)


def test_memory_model_uses_jsonb_and_native_timestamps() -> None:
    columns = MemoryModel.__table__.c
    assert isinstance(columns.metadata_json.type, JSONB)
    time_columns = (
        "created_at",
        "expires_at",
        "last_accessed_at",
        "valid_until",
        "deleted_time",
    )
    for name in time_columns:
        assert isinstance(columns[name].type, DateTime)
        assert columns[name].type.timezone
    assert not {"memory_type", "source_kind", "last_observed_at"} & set(columns.keys())
