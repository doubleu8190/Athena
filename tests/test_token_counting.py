"""Token counting and provider usage normalization tests."""

from langchain_core.messages import AIMessageChunk, HumanMessage

from athena.core.llm.tokens import (
    EmbeddingTokenCounter,
    ModelTokenCounter,
    OpenAITokenCounter,
    TokenUsage,
    conservative_text_token_count,
    token_usage_from_chunks,
)


class _OpenAIModel:
    __module__ = "langchain_openai.testing"
    model_name = "gpt-4o"


class _CustomTokenizerModel:
    def get_token_ids(self, text: str) -> list[int]:
        return list(range(len(text)))


class _OpenAIEmbeddingModel:
    __module__ = "langchain_openai.embeddings.base"

    def get_token_ids(self, text: str) -> list[int]:
        raise AssertionError("embedding models must not use the chat tokenizer")


class _EmbeddingTokenizer:
    def encode(self, text: str, **_: object) -> list[int]:
        return list(range(len(text) + 2))


def test_conservative_counter_handles_short_and_chinese_text():
    assert conservative_text_token_count("") == 0
    assert conservative_text_token_count("a") == 1
    assert conservative_text_token_count("这是中文文本") >= 6


def test_message_fallback_includes_structure_and_never_returns_zero():
    counter = ModelTokenCounter(object())  # type: ignore[arg-type]

    assert counter.count_message_tokens([HumanMessage(content="a")]) > 1


def test_openai_counter_uses_model_specific_tiktoken_encoding():
    counter = ModelTokenCounter(_OpenAIModel())  # type: ignore[arg-type]

    assert counter.count_text_tokens("hello 世界") > 0


def test_unknown_openai_model_uses_openai_fallback_encoding():
    counter = OpenAITokenCounter("deployment-that-is-not-in-tiktoken")

    assert counter.count_text_tokens("hello") > 0


def test_non_openai_model_only_uses_explicit_tokenizer():
    counter = ModelTokenCounter(_CustomTokenizerModel())  # type: ignore[arg-type]

    assert counter.count_text_tokens("abcd") == 4


def test_openai_embedding_model_does_not_use_chat_tiktoken():
    counter = ModelTokenCounter(_OpenAIEmbeddingModel())  # type: ignore[arg-type]

    assert counter.count_text_tokens("abcd") == conservative_text_token_count("abcd")


def test_embedding_counter_uses_embedding_tokenizer():
    counter = EmbeddingTokenCounter(_EmbeddingTokenizer())

    assert counter.count_text_tokens("abcd") == 6


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
