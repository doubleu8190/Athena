"""使用独立 schema 验证两阶段检索的持久轨迹和访问统计。"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from application.memory.retrieval_service import MemoryContextProvider
from application.orchestration import RootWorkflow
from application.orchestration.root_decision import ContextRequest
from application.runs.context import ContextAcquisitionService, MemoryQuery
from domain.memory import MemoryWriteCommand
from infrastructure.persistence.postgres.models import Base, MemoryModel, RetrievalCandidateModel, RetrievalRunModel
from infrastructure.persistence.postgres.repositories.memory_repository import PostgresMemoryRepository
from infrastructure.persistence.postgres.repositories.retrieval_repository import PostgresRetrievalRepository
from tests.unit.test_root_graph import _Agent, _Events, _dependencies


@pytest.mark.integration
@pytest.mark.asyncio
async def test_prefetch_and_directed_trace_stage_survive_postgres_round_trip() -> None:
    """真实 PostgreSQL 保存各批 query/阶段/选择，旧窗口不重复计访问；无参数。

    返回：无。异常：数据库连接、SQL 或断言错误使测试失败。
    未提供 ATHENA_TEST_DATABASE_URL 时明确跳过。
    """
    database_url = os.getenv("ATHENA_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"context_prefetch_{uuid4().hex}"
    url = make_url(database_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(url)
    engine = create_async_engine(url.update_query_dict({"options": f"-c search_path={schema},public"}))
    created = False
    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[MemoryModel.__table__, RetrievalRunModel.__table__, RetrievalCandidateModel.__table__],
                checkfirst=False,
            ))
            # public 只用于解析 vector/chinese 类型；禁止 checkfirst 将同名业务表误认成已创建。
            actual_schema = await connection.scalar(text(
                "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE c.oid='memories'::regclass"
            ))
            assert actual_schema == schema
        factory = async_sessionmaker(engine, expire_on_commit=False)
        memory = PostgresMemoryRepository(factory)
        traces = PostgresRetrievalRepository(factory)
        await memory.create_memory(MemoryWriteCommand(
            content="用户喜欢吃苹果", metadata={"session_id": "old-session"}, operation_key="fruit"
        ))
        deps = _dependencies(_Agent([]))
        deps.context_acquisition = ContextAcquisitionService(
            {"memory": MemoryContextProvider(memory)}, traces, _Events(), memory_repository=memory
        )
        workflow = RootWorkflow(deps)
        state = {
            "request": {"session_id": "new-session", "run_id": "round-trip", "message_id": "m", "user_message": "苹果"},
            "available_context_providers": ["memory"], "knowledge_bases": [],
        }
        state.update(await workflow.prefetch_context(state))
        state["root_decision"] = ContextRequest(action="context", queries=(MemoryQuery(provider="memory", query="火星基地编号"),)).model_dump(mode="json")
        state.update(await workflow.acquire_context(state))
        rows, total = await traces.list_runs(agent_run_id="round-trip")
        assert total == 2
        by_stage = {row["providers"][0]["effective_params"]["retrieval_stage"]: row for row in rows}
        assert by_stage["prefetch"]["providers"][0]["query"] == "苹果"
        assert by_stage["directed"]["providers"][0]["query"] == "火星基地编号"
        detail = await traces.get_run(by_stage["prefetch"]["run_id"])
        assert detail["candidates"][0]["context_selected"] is True
        assert (await memory.get("fruit")).access_count == 1
        assert "苹果" in state["context_bundle"]["items"][0]["content"]
    finally:
        await engine.dispose()
        if created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()
