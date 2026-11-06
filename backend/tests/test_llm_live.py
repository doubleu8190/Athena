"""显式启用时才调用真实模型的联调检查。"""

from __future__ import annotations

import asyncio
import os

import pytest

from domain.llm.model import LlmMessage, LlmRequest, LlmRole
from main import create_app

pytestmark = [
    pytest.mark.llm_live,
    pytest.mark.skipif(
        os.getenv("ATHENA_LLM_LIVE_TESTS") != "1",
        reason="set ATHENA_LLM_LIVE_TESTS=1 to enable real LLM calls",
    ),
]


def test_real_responses_profile_returns_completed_output() -> None:
    """使用部署配置的 Profile 验证实际服务连通性。"""
    profile = os.getenv("ATHENA_LLM_LIVE_PROFILE", "assistant")
    service = create_app().state.llm_service
    if service is None:
        pytest.skip("ATHENA_LLM_PROFILES is not configured")
    request = LlmRequest(
        profile,
        (LlmMessage(LlmRole.USER, "Reply with the single word ready."),),
        max_output_tokens=8,
    )
    response = asyncio.run(service.complete(request))
    assert response.status == "completed"
    assert response.output
