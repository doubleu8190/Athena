"""Neo4j schema statements owned by the Athena graph adapter."""

from __future__ import annotations

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE CONSTRAINT athena_kb_id_unique IF NOT EXISTS
    FOR (n:KnowledgeBase) REQUIRE n.id IS UNIQUE
    """,
    """
    CREATE CONSTRAINT athena_document_id_unique IF NOT EXISTS
    FOR (n:Document) REQUIRE n.attachment_id IS UNIQUE
    """,
    """
    CREATE CONSTRAINT athena_chunk_id_unique IF NOT EXISTS
    FOR (n:Chunk) REQUIRE n.id IS UNIQUE
    """,
    """
    CREATE INDEX athena_entity_key_index IF NOT EXISTS
    FOR (n:Entity) ON (n.entity_key)
    """,
    """
    CREATE FULLTEXT INDEX athena_entity_search IF NOT EXISTS
    FOR (n:Entity)
    ON EACH [n.canonical_name, n.aliases, n.description]
    """,
    """
    CREATE CONSTRAINT athena_memory_id_unique IF NOT EXISTS
    FOR (n:Memory) REQUIRE n.id IS UNIQUE
    """,
    """
    CREATE INDEX athena_memory_logical_id_index IF NOT EXISTS
    FOR (n:Memory) ON (n.logical_memory_id)
    """,
    """
    CREATE INDEX athena_memory_session_id_index IF NOT EXISTS
    FOR (n:Memory) ON (n.session_id)
    """,
)


__all__ = ["SCHEMA_STATEMENTS"]
