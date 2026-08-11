"""LLM 提供商路由 — 只读列出配置 + 测试连接."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from athena.config.settings import LLMProviderConfig
from athena.core.llm.provider import _create_chat_model
from athena.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/providers", tags=["providers"])


class ProviderInfo(BaseModel):
    """提供商只读信息 — api_key 仅以掩码形式暴露."""

    name: str
    provider: str
    model: str
    base_url: str
    api_key_configured: bool
    api_key_masked: str = ""
    temperature: float
    max_tokens: int


class TestProviderRequest(BaseModel):
    provider: str
    model: str
    api_key: str = ""
    base_url: str = ""


class TestProviderResult(BaseModel):
    ok: bool
    latency_ms: int | None = None
    error: str | None = None


@router.get("")
async def list_providers() -> list[ProviderInfo]:
    """列出 LLM 提供商配置（只读，密钥掩码）. 绝不写入配置."""
    from athena.config.settings import get_settings
    settings = get_settings()
    result: list[ProviderInfo] = []
    for cfg in settings.llm_providers:
        has_key = bool(cfg.api_key)
        masked = f"••••{cfg.api_key[-4:]}" if has_key else ""
        result.append(
            ProviderInfo(
                name=cfg.name,
                provider=cfg.provider,
                model=cfg.model,
                base_url=cfg.base_url,
                api_key_configured=has_key,
                api_key_masked=masked,
                temperature=cfg.temperature,
                max_tokens=cfg.max_tokens,
            )
        )
    return result


@router.post("/test")
async def test_provider(req: TestProviderRequest) -> TestProviderResult:
    """测试提供商连通性（构造一次性配置，不写入 settings）."""
    from athena.config.settings import get_settings
    settings = get_settings()
    config = LLMProviderConfig(
        name="test",
        provider=req.provider,
        model=req.model,
        api_key=req.api_key,
        base_url=req.base_url,
    )
    try:
        model = _create_chat_model(config, settings)
        start = time.monotonic()
        await asyncio.wait_for(model.ainvoke([HumanMessage("ping")]), timeout=10)
        latency_ms = int((time.monotonic() - start) * 1000)
        return TestProviderResult(ok=True, latency_ms=latency_ms)
    except Exception as e:
        logger.warning("provider_test_failed", provider=req.provider, error=str(e))
        return TestProviderResult(ok=False, error=str(e))
