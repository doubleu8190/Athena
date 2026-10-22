"""Tests for the retrying LLM provider's token counter initialization."""

from infrastructure.integrations.llm.llm_provider import RetryingLLMProvider


def test_default_token_counter_is_initialized() -> None:
    provider = RetryingLLMProvider(object())

    assert provider.count_text_tokens("one two") == 2


def test_injected_token_counter_is_used() -> None:
    provider = RetryingLLMProvider(object(), token_counter=lambda _text: 7)

    assert provider.count_text_tokens("one two") == 7
