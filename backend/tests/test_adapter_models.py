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


def test_metadata_declares_vector_hnsw_and_log_reclamation_indexes_on_postgresql() -> None:
    """ORM 元数据应声明知识库 HNSW、日志回收与偏好记忆索引，并可在真实 PostgreSQL 完整建表。"""
    import asyncio

    import sqlalchemy as sa
    from pg_unit_harness import temp_pg_schema

    kc_indexes = {idx.name: idx for idx in Base.metadata.tables["knowledge_chunks"].indexes}
    um_indexes = {idx.name: idx for idx in Base.metadata.tables["user_memories"].indexes}
    exec_indexes = {idx.name: idx for idx in Base.metadata.tables["skill_executions"].indexes}

    assert "ix_knowledge_chunks_embedding_hnsw" in kc_indexes
    kc_hnsw = kc_indexes["ix_knowledge_chunks_embedding_hnsw"]
    assert kc_hnsw.dialect_options["postgresql"]["using"] == "hnsw"
    assert kc_hnsw.dialect_options["postgresql"]["ops"] == {"embedding": "vector_cosine_ops"}

    assert "ix_user_memories_user_updated" in um_indexes
    assert "ix_skill_executions_log_object_key" in exec_indexes

    async def create_postgresql_schema() -> None:
        # 在一次性隔离 Schema 上执行全量 DDL 建表并读取真实 PG 索引元数据，退出自动级联清理。
        async with temp_pg_schema(prefix="unit_ddl_models") as (engine, schema):
            async with engine.begin() as conn:
                await conn.run_sync(
                    lambda sync_conn: Base.metadata.create_all(sync_conn, checkfirst=False)
                )

                def _inspect_indexes(sync_conn: sa.Connection) -> dict[str, set[str]]:
                    inspector = sa.inspect(sync_conn)
                    return {
                        table: {
                            idx["name"]
                            for idx in inspector.get_indexes(table, schema=schema)
                            if idx["name"] is not None
                        }
                        for table in ("knowledge_chunks", "user_memories", "skill_executions")
                    }

                pg_indexes = await conn.run_sync(_inspect_indexes)
            assert "ix_knowledge_chunks_embedding_hnsw" in pg_indexes["knowledge_chunks"]
            assert "ix_user_memories_user_updated" in pg_indexes["user_memories"]
            assert "ix_skill_executions_log_object_key" in pg_indexes["skill_executions"]

    asyncio.run(create_postgresql_schema())
