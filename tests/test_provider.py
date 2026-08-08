"""LLM Provider 参数映射测试 — 验证各 Provider 构造参数名正确."""

from __future__ import annotations

from unittest.mock import patch

import langchain_ollama

from athena.config.settings import LLMProviderConfig, Settings
from athena.core.llm.provider import _create_chat_model


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
