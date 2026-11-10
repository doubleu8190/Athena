"""显式启用时从管理员数据库模型版本调用真实模型。"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from adapter.llm import OpenAIChatCompletionsAdapter
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from domain.llm.model import LlmMessage, LlmRequest, LlmRole
from infrastructure.security.mcp_secrets import McpSecretCipher

pytestmark = [
    pytest.mark.llm_live,
    pytest.mark.skipif(
        os.getenv("ATHENA_LLM_LIVE_TESTS") != "1",
        reason="set ATHENA_LLM_LIVE_TESTS=1 to enable real LLM calls",
    ),
]


def test_admin_configured_profile_returns_completed_output() -> None:
    """用管理员已保存的模型版本执行真实请求，不读取供应商配置环境变量。"""
    database_url = os.getenv("ATHENA_TEST_DATABASE_URL")
    profile_id = os.getenv("ATHENA_LLM_LIVE_PROFILE_ID")
    if not database_url or not profile_id:
        pytest.skip("set ATHENA_TEST_DATABASE_URL and ATHENA_LLM_LIVE_PROFILE_ID")

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        provider = OpenAIChatCompletionsAdapter()
        try:
            repository = SqlAlchemyModelProfileRepository(
                async_sessionmaker(engine, expire_on_commit=False),
                McpSecretCipher.from_environment(),
            )
            runtime = await repository.get_runtime(profile_id)
            if runtime is None:
                pytest.skip("the selected administrator model profile is unavailable")
            response = await provider.complete(
                runtime,
                LlmRequest(
                    runtime.version.name,
                    (LlmMessage(LlmRole.USER, "Reply with the single word ready."),),
                    max_output_tokens=8,
                ),
            )
            assert response.status == "completed"
            assert response.output
        finally:
            await provider.aclose()
            await engine.dispose()

    asyncio.run(scenario())
