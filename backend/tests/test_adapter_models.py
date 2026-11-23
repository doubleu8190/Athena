"""共享 ORM metadata 聚合测试。"""

from adapter.models import Base


def test_metadata_contains_all_domains_and_owner_foreign_keys() -> None:
    assert {
        "auth_sessions",
        "conversation_messages",
        "execution_messages",
        "projects",
        "sessions",
        "users",
    }.issubset(Base.metadata.tables)

    project_foreign_keys = {
        foreign_key.target_fullname for foreign_key in Base.metadata.tables["projects"].foreign_keys
    }
    session_foreign_keys = {
        foreign_key.target_fullname for foreign_key in Base.metadata.tables["sessions"].foreign_keys
    }
    assert "users.id" in project_foreign_keys
    assert "users.id" in session_foreign_keys


def test_metadata_declares_vector_hnsw_and_log_reclamation_indexes_compatible_with_sqlite() -> None:
    """ORM 元数据应声明 HNSW 向量索引与日志回收索引，且 SQLite 测试建表不受影响。"""
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    kc_indexes = {idx.name: idx for idx in Base.metadata.tables["knowledge_chunks"].indexes}
    mem_indexes = {idx.name: idx for idx in Base.metadata.tables["memories"].indexes}
    exec_indexes = {idx.name: idx for idx in Base.metadata.tables["skill_executions"].indexes}

    assert "ix_knowledge_chunks_embedding_hnsw" in kc_indexes
    kc_hnsw = kc_indexes["ix_knowledge_chunks_embedding_hnsw"]
    assert kc_hnsw.dialect_options["postgresql"]["using"] == "hnsw"
    assert kc_hnsw.dialect_options["postgresql"]["ops"] == {"embedding": "vector_cosine_ops"}

    assert "ix_memories_embedding_hnsw" in mem_indexes
    mem_hnsw = mem_indexes["ix_memories_embedding_hnsw"]
    assert mem_hnsw.dialect_options["postgresql"]["using"] == "hnsw"
    assert mem_hnsw.dialect_options["postgresql"]["ops"] == {"embedding": "vector_cosine_ops"}

    assert "ix_skill_executions_log_object_key" in exec_indexes

    async def create_sqlite_schema() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()

    asyncio.run(create_sqlite_schema())
