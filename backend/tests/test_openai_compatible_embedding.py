"""OpenAI 兼容远程 Embedding 客户端、TiktokenTokenCounter 与档案标识单元测试。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest

from infrastructure.embedding import (
    EmbeddingConfigurationError,
    EmbeddingProtocolError,
    EmbeddingUpstreamError,
    OpenAICompatibleEmbeddingClient,
    OpenAICompatibleEmbeddingConfig,
    TiktokenTokenCounter,
    remote_embedding_profile_id,
)


def _embedding_response(
    vectors: list[tuple[int, list[float]]],
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx2.Response:
    payload = {
        "object": "list",
        "data": [
            {"object": "embedding", "index": index, "embedding": vector}
            for index, vector in vectors
        ],
    }
    return httpx2.Response(
        status_code,
        json=payload,
        headers=headers,
    )


def test_config_from_environment_defaults_and_hides_api_key() -> None:
    config = OpenAICompatibleEmbeddingConfig.from_environment(
        {
            "ATHENA_EMBEDDING_BASE_URL": "https://embed.example.test/v1/",
            "ATHENA_EMBEDDING_API_KEY": "sk-super-secret-value",
        },
        expected_dimensions=1024,
    )
    assert config.base_url == "https://embed.example.test/v1/"
    assert config.embeddings_url == "https://embed.example.test/v1/embeddings"
    assert config.api_key == "sk-super-secret-value"
    assert config.model == "BAAI/bge-m3"
    assert config.dimensions == 1024
    assert config.timeout_seconds == 30.0
    assert config.batch_size == 32
    assert config.send_dimensions is True
    assert "sk-super-secret-value" not in repr(config)

    no_key = OpenAICompatibleEmbeddingConfig.from_environment(
        {
            "ATHENA_EMBEDDING_BASE_URL": "http://127.0.0.1:8000/v1",
            "ATHENA_EMBEDDING_API_KEY": "   ",
            "ATHENA_EMBEDDING_SEND_DIMENSIONS": "false",
        }
    )
    assert no_key.api_key is None
    assert no_key.send_dimensions is False


@pytest.mark.parametrize(
    "bad_url",
    (
        "",
        "   ",
        "ftp://embed.example.test/v1",
        "embed.example.test/v1",
        "http://user:pass@embed.example.test/v1",
        "https://user@embed.example.test/v1",
        "http://embed.example.test/v1?foo=bar",
        "http://embed.example.test/v1#frag",
        "http://embed.example.test:abc/v1",
        "http://embed.example.test:/v1",
        "http://embed.example.test:70000/v1",
        "http://embed.example.test/v1\nX-Inject: 1",
        "http://embed example.test/v1",
        "http://127.0.0.999:8000/v1",
    ),
)
def test_config_rejects_invalid_base_url(bad_url: str) -> None:
    with pytest.raises(EmbeddingConfigurationError):
        OpenAICompatibleEmbeddingConfig.from_environment({"ATHENA_EMBEDDING_BASE_URL": bad_url})


def test_config_rejects_dimension_mismatch_and_invalid_parameters() -> None:
    with pytest.raises(EmbeddingConfigurationError, match="embedding_dimensions_mismatch"):
        OpenAICompatibleEmbeddingConfig.from_environment(
            {
                "ATHENA_EMBEDDING_BASE_URL": "http://127.0.0.1:8000/v1",
                "ATHENA_EMBEDDING_DIMENSIONS": "1536",
            },
            expected_dimensions=1024,
        )

    for overrides in (
        {"ATHENA_EMBEDDING_DIMENSIONS": "0"},
        {"ATHENA_EMBEDDING_DIMENSIONS": "abc"},
        {"ATHENA_EMBEDDING_TIMEOUT_SECONDS": "0"},
        {"ATHENA_EMBEDDING_TIMEOUT_SECONDS": "nan"},
        {"ATHENA_EMBEDDING_TIMEOUT_SECONDS": "inf"},
        {"ATHENA_EMBEDDING_TIMEOUT_SECONDS": "999"},
        {"ATHENA_EMBEDDING_BATCH_SIZE": "0"},
        {"ATHENA_EMBEDDING_BATCH_SIZE": "500"},
        {"ATHENA_EMBEDDING_SEND_DIMENSIONS": "maybe"},
        {"ATHENA_EMBEDDING_API_KEY": "sk-bad\r\nHeader: inject"},
    ):
        with pytest.raises(EmbeddingConfigurationError):
            OpenAICompatibleEmbeddingConfig.from_environment(
                {
                    "ATHENA_EMBEDDING_BASE_URL": "http://127.0.0.1:8000/v1",
                    **overrides,
                }
            )


def test_embed_batches_preserves_order_and_handles_optional_auth_and_dimensions() -> None:
    seen_requests: list[tuple[str, dict[str, Any], str | None]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen_requests.append((str(request.url), body, request.headers.get("authorization")))
        inputs = body["input"]
        # 故意倒序返回 index，验证客户端按 index 重排并跨批次保序拼接。
        items = [
            (idx, [float(len(text)), float(idx), 0.5, 1.0])
            for idx, text in reversed(list(enumerate(inputs)))
        ]
        return _embedding_response(items)

    config = OpenAICompatibleEmbeddingConfig(
        base_url="http://embed.internal:8000/v1",
        api_key="sk-batch-key",
        model="BAAI/bge-m3",
        dimensions=4,
        batch_size=2,
        send_dimensions=True,
    )

    async def scenario() -> None:
        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=httpx2.MockTransport(handler),
        )
        await client.start()
        try:
            assert await client.embed([]) == []
            assert seen_requests == []

            vectors = await client.embed(["a", "bb", "ccc", "dddd", "eeeee"])
            assert vectors == [
                [1.0, 0.0, 0.5, 1.0],
                [2.0, 1.0, 0.5, 1.0],
                [3.0, 0.0, 0.5, 1.0],
                [4.0, 1.0, 0.5, 1.0],
                [5.0, 0.0, 0.5, 1.0],
            ]
        finally:
            await client.aclose()

        # 关闭后再次请求应明确失败，且重复 aclose 保持幂等。
        await client.aclose()
        with pytest.raises(EmbeddingUpstreamError, match="embedding_client_closed"):
            await client.embed(["after-close"])

    asyncio.run(scenario())

    assert len(seen_requests) == 3
    assert [req[1]["input"] for req in seen_requests] == [
        ["a", "bb"],
        ["ccc", "dddd"],
        ["eeeee"],
    ]
    assert all(req[0] == "http://embed.internal:8000/v1/embeddings" for req in seen_requests)
    assert all(req[1]["dimensions"] == 4 for req in seen_requests)
    assert all(req[2] == "Bearer sk-batch-key" for req in seen_requests)

    # 验证无 API Key 且关闭 send_dimensions 时不发送 Authorization 头和 dimensions 字段。
    seen_requests.clear()
    minimal_config = OpenAICompatibleEmbeddingConfig(
        base_url="http://127.0.0.1:8000/v1",
        api_key=None,
        model="local-embed",
        dimensions=4,
        send_dimensions=False,
    )

    async def minimal_scenario() -> None:
        client = OpenAICompatibleEmbeddingClient(
            minimal_config,
            transport=httpx2.MockTransport(handler),
        )
        try:
            vectors = await client.embed(["hello"])
            assert vectors == [[5.0, 0.0, 0.5, 1.0]]
        finally:
            await client.aclose()

    asyncio.run(minimal_scenario())
    assert len(seen_requests) == 1
    assert "dimensions" not in seen_requests[0][1]
    assert seen_requests[0][2] is None


def test_embed_rejects_dimension_mismatch_and_invalid_protocol_payloads() -> None:
    bad_payloads: list[tuple[bytes, str]] = [
        (b"not-json", "embedding_response_invalid_json"),
        (
            json.dumps({"data": [{"index": 0, "embedding": [0.1, 0.2]}]}).encode(),
            "embedding_dimensions_invalid",
        ),
        (
            json.dumps({"data": []}).encode(),
            "embedding_count_mismatch",
        ),
        (
            json.dumps({"data": [{"index": 0, "embedding": [0.1, "bad", 0.3, 0.4]}]}).encode(),
            "embedding_vector_invalid",
        ),
        (
            json.dumps({"data": [{"index": 0, "embedding": [True, 0.2, 0.3, 0.4]}]}).encode(),
            "embedding_vector_invalid",
        ),
        (
            b'{"data":[{"index":0,"embedding":[0.1,NaN,0.3,0.4]}]}',
            "embedding_vector_non_finite",
        ),
        (
            json.dumps({"data": [{"index": 9, "embedding": [0.1, 0.2, 0.3, 0.4]}]}).encode(),
            "embedding_index_invalid",
        ),
    ]
    config = OpenAICompatibleEmbeddingConfig(
        base_url="http://127.0.0.1:8000/v1",
        dimensions=4,
    )

    for raw_body, expected_code in bad_payloads:

        def handler(_request: httpx2.Request, body: bytes = raw_body) -> httpx2.Response:
            return httpx2.Response(200, content=body, headers={"content-type": "application/json"})

        async def scenario(transport_handler: Any = handler) -> None:
            client = OpenAICompatibleEmbeddingClient(
                config,
                transport=httpx2.MockTransport(transport_handler),
            )
            try:
                await client.embed(["hello"])
            finally:
                await client.aclose()

        with pytest.raises(EmbeddingProtocolError, match=expected_code):
            asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    (
        (429, "embedding_rate_limited"),
        (502, "embedding_upstream_unavailable"),
        (503, "embedding_upstream_unavailable"),
        (504, "embedding_upstream_unavailable"),
    ),
)
def test_embed_retries_transient_statuses_and_timeouts_up_to_two_times(
    status_code: int,
    expected_code: str,
) -> None:
    attempts = 0
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    def recover_on_third(_request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx2.Response(status_code, text="temporary failure")
        return _embedding_response([(0, [0.1, 0.2, 0.3, 0.4])])

    config = OpenAICompatibleEmbeddingConfig(
        base_url="http://127.0.0.1:8000/v1",
        dimensions=4,
    )

    async def run_recover() -> list[list[float]]:
        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=httpx2.MockTransport(recover_on_third),
            sleep=fake_sleep,
        )
        try:
            return await client.embed(["retry-me"])
        finally:
            await client.aclose()

    assert asyncio.run(run_recover()) == [[0.1, 0.2, 0.3, 0.4]]
    assert attempts == 3
    assert delays == [0.25, 0.5]

    # 连续失败 3 次（首次 + 2 次重试）后抛出对应可重试错误码。
    exhausted_attempts = 0
    delays.clear()

    def always_fail(_request: httpx2.Request) -> httpx2.Response:
        nonlocal exhausted_attempts
        exhausted_attempts += 1
        return httpx2.Response(status_code, text="still failing")

    async def run_exhausted() -> None:
        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=httpx2.MockTransport(always_fail),
            sleep=fake_sleep,
        )
        try:
            await client.embed(["retry-me"])
        finally:
            await client.aclose()

    with pytest.raises(EmbeddingUpstreamError, match=expected_code) as exc_info:
        asyncio.run(run_exhausted())
    assert exc_info.value.retryable is True
    assert exc_info.value.status_code == status_code
    assert exhausted_attempts == 3
    assert delays == [0.25, 0.5]


def test_embed_retries_timeout_and_never_leaks_api_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_key = "sk-do-not-leak-anywhere-987654"
    attempts = 0
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    def timeout_handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        raise httpx2.ReadTimeout(f"timeout with header {request.headers.get('authorization')}")

    config = OpenAICompatibleEmbeddingConfig(
        base_url="http://127.0.0.1:8000/v1",
        api_key=secret_key,
        dimensions=4,
    )

    async def scenario() -> None:
        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=httpx2.MockTransport(timeout_handler),
            sleep=fake_sleep,
        )
        try:
            await client.embed(["hello"])
        finally:
            await client.aclose()

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(EmbeddingUpstreamError, match="embedding_timeout") as exc_info:
            asyncio.run(scenario())

    assert attempts == 3
    assert delays == [0.25, 0.5]
    assert exc_info.value.retryable is True
    assert exc_info.value.__cause__ is None
    assert secret_key not in str(exc_info.value)
    assert secret_key not in repr(exc_info.value)
    assert secret_key not in caplog.text


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    (
        (400, "embedding_request_invalid"),
        (401, "embedding_auth_failed"),
        (403, "embedding_auth_failed"),
        (404, "embedding_endpoint_not_found"),
        (422, "embedding_request_invalid"),
        (500, "embedding_upstream_error"),
    ),
)
def test_embed_does_not_retry_4xx_or_non_transient_errors_and_hides_key(
    status_code: int,
    expected_code: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_key = "sk-non-retryable-secret-key"
    attempts = 0
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    def error_handler(_request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        return httpx2.Response(status_code, text=f"upstream echoed {secret_key}")

    config = OpenAICompatibleEmbeddingConfig(
        base_url="http://127.0.0.1:8000/v1",
        api_key=secret_key,
        dimensions=4,
    )

    async def scenario() -> None:
        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=httpx2.MockTransport(error_handler),
            sleep=fake_sleep,
        )
        try:
            await client.embed(["bad-input"])
        finally:
            await client.aclose()

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(EmbeddingUpstreamError, match=expected_code) as exc_info:
            asyncio.run(scenario())

    assert attempts == 1
    assert delays == []
    assert exc_info.value.retryable is False
    assert exc_info.value.status_code == status_code
    assert secret_key not in str(exc_info.value)
    assert secret_key not in caplog.text


def test_embed_maps_corrupted_gzip_decoding_error_without_leaking_api_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """损坏的 gzip 响应触发 httpx2.DecodingError 时应映射为协议异常并截断异常链防密钥泄露。"""
    secret_key = "sk-gzip-decode-secret-12345"

    def corrupted_gzip_handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            content=b"not-valid-gzip-stream",
            headers={
                "content-type": "application/json",
                "content-encoding": "gzip",
            },
        )

    config = OpenAICompatibleEmbeddingConfig(
        base_url="http://127.0.0.1:8000/v1",
        api_key=secret_key,
        dimensions=4,
    )

    async def scenario() -> None:
        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=httpx2.MockTransport(corrupted_gzip_handler),
        )
        try:
            await client.embed(["hello"])
        finally:
            await client.aclose()

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(
            EmbeddingProtocolError, match="embedding_response_decode_failed"
        ) as exc_info:
            asyncio.run(scenario())

    assert exc_info.value.__cause__ is None
    assert secret_key not in str(exc_info.value)
    assert secret_key not in repr(exc_info.value)
    assert secret_key not in caplog.text


def test_embed_maps_other_httpx_stream_errors_without_leaking_api_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """其余 httpx2 StreamError/HTTPError 同样应被脱敏映射且截断异常链。"""
    secret_key = "sk-stream-error-secret-67890"

    def stream_error_handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.StreamError(f"stream error for {request.headers.get('authorization')}")

    config = OpenAICompatibleEmbeddingConfig(
        base_url="http://127.0.0.1:8000/v1",
        api_key=secret_key,
        dimensions=4,
    )

    async def scenario() -> None:
        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=httpx2.MockTransport(stream_error_handler),
        )
        try:
            await client.embed(["hello"])
        finally:
            await client.aclose()

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(EmbeddingUpstreamError, match="embedding_transport_error") as exc_info:
            asyncio.run(scenario())

    assert exc_info.value.retryable is False
    assert exc_info.value.__cause__ is None
    assert secret_key not in str(exc_info.value)
    assert secret_key not in caplog.text


def test_embed_closes_previous_response_stream_before_retry_sleep() -> None:
    """触发 429/5xx 重试退避时，必须在进入 sleep 前关闭上一次响应流以归还连接池。"""

    class _TrackedStream(httpx2.AsyncByteStream):
        def __init__(self, body: bytes) -> None:
            self._body = body
            self.closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield self._body

        async def aclose(self) -> None:
            self.closed = True

    streams: list[_TrackedStream] = []
    closed_before_sleep: list[bool] = []

    class _RetryingTransport(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            if len(streams) == 0:
                stream = _TrackedStream(b"rate limited")
                streams.append(stream)
                return httpx2.Response(429, stream=stream, request=request)
            payload = json.dumps(
                {"object": "list", "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]}]}
            ).encode("utf-8")
            stream = _TrackedStream(payload)
            streams.append(stream)
            return httpx2.Response(200, stream=stream, request=request)

        async def aclose(self) -> None:
            return None

    async def verify_sleep(seconds: float) -> None:
        assert seconds == 0.25
        # 断言进入退避等待前，前一次 429 响应流已经关闭并归还连接。
        assert len(streams) == 1
        closed_before_sleep.append(streams[0].closed)

    config = OpenAICompatibleEmbeddingConfig(
        base_url="http://127.0.0.1:8000/v1",
        dimensions=4,
    )

    async def scenario() -> list[list[float]]:
        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=_RetryingTransport(),
            sleep=verify_sleep,
        )
        try:
            return await client.embed(["check-stream-closed"])
        finally:
            await client.aclose()

    assert asyncio.run(scenario()) == [[0.1, 0.2, 0.3, 0.4]]
    assert closed_before_sleep == [True]
    assert all(stream.closed for stream in streams)


def test_embed_closing_between_batches_raises_closed_error_cleanly() -> None:
    """分批执行期间客户端被关闭时，应抛出明确 EmbeddingUpstreamError 而非 AssertionError。"""
    config = OpenAICompatibleEmbeddingConfig(
        base_url="http://127.0.0.1:8000/v1",
        dimensions=4,
        batch_size=1,
    )

    async def scenario() -> None:
        client: OpenAICompatibleEmbeddingClient | None = None

        async def handler(_request: httpx2.Request) -> httpx2.Response:
            assert client is not None
            await client.aclose()
            return _embedding_response([(0, [0.1, 0.2, 0.3, 0.4])])

        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=httpx2.MockTransport(handler),
        )
        with pytest.raises(EmbeddingUpstreamError, match="embedding_client_closed"):
            await client.embed(["first", "second"])

    asyncio.run(scenario())


def test_tiktoken_token_counter_lifecycle_and_special_tokens() -> None:
    counter = TiktokenTokenCounter()
    asyncio.run(counter.start())
    assert counter.count("") == 0
    assert counter.count("hello world") == 2
    assert counter.count("中文知识库检索与分块") > 0
    # 用户文档中出现特殊 token 字面量时不得触发 ValueError。
    assert counter.count("literal <|endoftext|> marker") > 0
    counter.close()
    # 关闭后支持惰性重新加载且再次关闭幂等。
    assert counter.count("hello") == 1
    counter.close()
    counter.close()

    with pytest.raises(ValueError, match="token_counter_encoding_invalid"):
        TiktokenTokenCounter("   ")


def test_remote_embedding_profile_id_is_stable_and_bounded() -> None:
    assert remote_embedding_profile_id("BAAI/bge-m3", 1024) == "openai_compatible:BAAI/bge-m3:1024"
    long_model = "org/" + "m" * 150
    hashed_first = remote_embedding_profile_id(long_model, 1024)
    hashed_second = remote_embedding_profile_id(long_model, 1024)
    assert hashed_first == hashed_second
    assert hashed_first.startswith("openai_compatible:sha256:")
    assert hashed_first.endswith(":1024")
    assert len(hashed_first) <= 128
