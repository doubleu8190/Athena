"""LLM Provider 参数映射测试 — 验证各 Provider 构造参数名正确."""

from __future__ import annotations

from unittest.mock import patch

import langchain_ollama

from athena.config.settings import LLMProviderConfig, LLMRetrySettings, Settings
from athena.core.llm.provider import _create_chat_model, _to_retry_config


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


def test_openai_uses_max_tokens_and_streaming() -> None:
    """ChatOpenAI 应接收 max_tokens 与 streaming=True."""
    config = LLMProviderConfig(
        name="primary", provider="openai", model="gpt-4o", max_tokens=2048
    )
    settings = Settings()
    with patch("langchain_openai.ChatOpenAI") as mock_cls:
        mock_cls.return_value = "fake"
        _create_chat_model(config, settings)
    kwargs = mock_cls.call_args.kwargs
    assert kwargs["max_tokens"] == 2048
    assert kwargs["streaming"] is True
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
