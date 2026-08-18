"""Token counting and provider usage normalization tests."""

from langchain_core.messages import AIMessageChunk, HumanMessage

from athena.core.llm.tokens import (
    ModelTokenCounter,
    TokenUsage,
    conservative_text_token_count,
    token_usage_from_chunks,
)


def test_conservative_counter_handles_short_and_chinese_text():
    assert conservative_text_token_count("") == 0
    assert conservative_text_token_count("a") == 1
    assert conservative_text_token_count("这是中文文本") >= 6


def test_message_fallback_includes_structure_and_never_returns_zero():
    counter = ModelTokenCounter(object())  # type: ignore[arg-type]

    assert counter.count_message_tokens([HumanMessage(content="a")]) > 1


def test_stream_usage_is_accumulated_across_chunks():
    chunks = [
        AIMessageChunk(
            content="first",
            usage_metadata={
                "input_tokens": 12,
                "output_tokens": 1,
                "total_tokens": 13,
            },
        ),
        AIMessageChunk(
            content="second",
            usage_metadata={
                "input_tokens": 0,
                "output_tokens": 2,
                "total_tokens": 2,
            },
        ),
    ]

    assert token_usage_from_chunks(chunks) == TokenUsage(
        input_tokens=12, output_tokens=3
    )


def test_ollama_response_metadata_usage_is_normalized():
    chunks = [
        AIMessageChunk(
            content="answer",
            response_metadata={"prompt_eval_count": 8, "eval_count": 3},
        )
    ]

    assert token_usage_from_chunks(chunks) == TokenUsage(
        input_tokens=8, output_tokens=3
    )
