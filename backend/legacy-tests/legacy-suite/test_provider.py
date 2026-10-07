"""LLM Provider 参数映射测试 — 验证各 Provider 构造参数名正确."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import langchain_ollama
import pytest
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from athena.config.settings import LLMProviderConfig, LLMRetrySettings, Settings
from athena.core.llm.provider import LLMProvider, _create_chat_model, _to_retry_config
from athena.core.llm.retry import LLMRetryManager, RetryConfig


class _StructuredResult(BaseModel):
    """用于验证 Provider 结构化调用的最小结果模型。"""

    answer: str


def test_ollama_maps_max_tokens_to_num_predict() -> None:
    """ChatOllama 无 max_tokens/streaming 字段，应映射到 num_predict 且不传 streaming."""
    config = LLMProviderConfig(
        name="ollama", provider="ollama", model="llama3", max_tokens=512
    )
    settings = Settings()
    with patch.object(langchain_ollama, "ChatOllama") as mock_cls:
        mock_cls.return_value = "fake"
        _create_chat_model(config, settings)
    kwargs = mock_cls.call_args.kwargs
    assert kwargs["num_predict"] == 512
    assert "max_tokens" not in kwargs
    assert "streaming" not in kwargs


def test_openai_uses_max_tokens_without_streaming() -> None:
    """ChatOpenAI 使用完整响应，实时分批由应用层负责。"""
    config = LLMProviderConfig(
        name="primary", provider="openai", model="gpt-4o", max_tokens=2048
    )
    settings = Settings()
    with patch("langchain_openai.ChatOpenAI") as mock_cls:
        mock_cls.return_value = "fake"
        _create_chat_model(config, settings)
    kwargs = mock_cls.call_args.kwargs
    assert kwargs["max_tokens"] == 2048
    assert kwargs["streaming"] is False
    assert kwargs["stream_usage"] is True


def test_provider_level_max_tokens_falls_back_to_global() -> None:
    """provider 级别 max_tokens=-1 时使用全局 llm_max_tokens."""
    config = LLMProviderConfig(
        name="primary", provider="openai", model="gpt-4o", max_tokens=-1
    )
    settings = Settings(llm_max_tokens=8192)
    with patch("langchain_openai.ChatOpenAI") as mock_cls:
        mock_cls.return_value = "fake"
        _create_chat_model(config, settings)
    kwargs = mock_cls.call_args.kwargs
    assert kwargs["max_tokens"] == 8192


def test_retry_settings_map_to_retry_config() -> None:
    """Settings 中的全部重试参数应无损映射到领域配置."""
    settings = LLMRetrySettings(
        max_attempts=7,
        min_delay_ms=123,
        max_delay_ms=456,
        jitter=0.25,
        timeout_ms=789,
    )

    retry_config = _to_retry_config(settings)

    assert retry_config.max_attempts == 7
    assert retry_config.min_delay_ms == 123
    assert retry_config.max_delay_ms == 456
    assert retry_config.jitter == 0.25
    assert retry_config.timeout_ms == 789


def test_retry_settings_support_nested_environment_variables(monkeypatch) -> None:
    monkeypatch.setenv("LLM_RETRY__MAX_ATTEMPTS", "6")
    monkeypatch.setenv("LLM_SECONDARY_RETRY__TIMEOUT_MS", "45000")

    settings = Settings(_env_file=None)

    assert settings.llm_retry.max_attempts == 6
    assert settings.llm_secondary_retry.timeout_ms == 45000


@pytest.mark.asyncio
async def test_structured_invocation_is_owned_by_provider() -> None:
    """Provider 应负责绑定 schema、调用 runnable 并校验结果。"""
    model = MagicMock()
    runnable = MagicMock()
    runnable.ainvoke = AsyncMock(return_value={"answer": "ok"})
    model.with_structured_output.return_value = runnable
    provider = LLMProvider(model)

    result = await provider.ainvoke_structured(
        _StructuredResult,
        [HumanMessage(content="ping")],
    )

    assert result == _StructuredResult(answer="ok")
    model.with_structured_output.assert_called_once_with(_StructuredResult)
    runnable.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_structured_invocation_keeps_schema_when_falling_back() -> None:
    """结构化调用故障转移后仍必须在备用 Provider 上绑定同一 schema。"""
    primary_model = MagicMock()
    primary_runnable = MagicMock()
    primary_runnable.ainvoke = AsyncMock(side_effect=ConnectionError("offline"))
    primary_model.with_structured_output.return_value = primary_runnable

    fallback_model = MagicMock()
    fallback_runnable = MagicMock()
    fallback_runnable.ainvoke = AsyncMock(return_value={"answer": "fallback"})
    fallback_model.with_structured_output.return_value = fallback_runnable

    retry_manager = LLMRetryManager(
        RetryConfig(max_attempts=1, min_delay_ms=0, max_delay_ms=0)
    )
    provider = LLMProvider(primary_model, retry_manager=retry_manager)
    retry_manager.add_fallback_provider(LLMProvider(fallback_model))

    result = await provider.ainvoke_structured(
        _StructuredResult,
        [HumanMessage(content="ping")],
    )

    assert result == _StructuredResult(answer="fallback")
    fallback_model.with_structured_output.assert_called_once_with(_StructuredResult)
    fallback_runnable.ainvoke.assert_awaited_once()
