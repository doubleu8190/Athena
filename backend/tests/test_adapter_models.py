"""共享 ORM metadata 聚合测试。"""

from adapter.models import Base


def test_metadata_contains_all_domains_and_owner_foreign_keys() -> None:
    assert {
        "auth_sessions",
        "conversation_messages",
        "execution_messages",
        "projects",
        "sessions",
        "user_memories",
        "user_work_profiles",
        "users",
    }.issubset(Base.metadata.tables)
    assert "memories" not in Base.metadata.tables

    project_foreign_keys = {
        foreign_key.target_fullname for foreign_key in Base.metadata.tables["projects"].foreign_keys
    }
    session_foreign_keys = {
        foreign_key.target_fullname for foreign_key in Base.metadata.tables["sessions"].foreign_keys
    }
    profile_foreign_keys = {
        foreign_key.target_fullname
        for foreign_key in Base.metadata.tables["user_work_profiles"].foreign_keys
    }
    memory_foreign_keys = {
        foreign_key.target_fullname
        for foreign_key in Base.metadata.tables["user_memories"].foreign_keys
    }
    assert "users.id" in project_foreign_keys
    assert "users.id" in session_foreign_keys
    assert "users.id" in profile_foreign_keys
    assert "users.id" in memory_foreign_keys


def test_metadata_declares_vector_hnsw_and_log_reclamation_indexes_compatible_with_sqlite() -> None:
    """ORM 元数据应声明知识库 HNSW、日志回收与偏好记忆索引，且兼容 SQLite 建表。"""
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    kc_indexes = {idx.name: idx for idx in Base.metadata.tables["knowledge_chunks"].indexes}
    um_indexes = {idx.name: idx for idx in Base.metadata.tables["user_memories"].indexes}
    exec_indexes = {idx.name: idx for idx in Base.metadata.tables["skill_executions"].indexes}

    assert "ix_knowledge_chunks_embedding_hnsw" in kc_indexes
    kc_hnsw = kc_indexes["ix_knowledge_chunks_embedding_hnsw"]
    assert kc_hnsw.dialect_options["postgresql"]["using"] == "hnsw"
    assert kc_hnsw.dialect_options["postgresql"]["ops"] == {"embedding": "vector_cosine_ops"}

    assert "ix_user_memories_user_updated" in um_indexes
    assert "ix_skill_executions_log_object_key" in exec_indexes

    async def create_sqlite_schema() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()

    asyncio.run(create_sqlite_schema())
