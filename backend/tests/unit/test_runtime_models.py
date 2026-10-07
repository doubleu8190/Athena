"""Coverage for the complete target ORM table inventory."""

from infrastructure.persistence.postgres.models import Base


EXPECTED_TABLES = {
    "embedding_metadata", "agent_runs", "agent_commands", "agent_plans", "agent_tasks",
    "agent_events", "approvals", "sessions", "messages", "steps", "tool_calls",
    "memories", "mcp_servers", "tools", "knowledge_bases", "attachments",
    "knowledge_document_jobs", "file_chunks", "file_artifacts", "adapter_registry",
    "code_symbols", "code_dependencies", "retrieval_runs", "retrieval_candidates",
}


def test_target_orm_covers_all_deployed_tables() -> None:
    assert EXPECTED_TABLES <= set(Base.metadata.tables)
