"""OpenAI 兼容远程 Embedding 客户端、TiktokenTokenCounter 与档案标识单元测试。"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import zlib
from collections.abc import AsyncIterator
from typing import Any, cast

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
from infrastructure.embedding.openai_compatible import (
    DEFAULT_EMBEDDING_MAX_CONCURRENCY,
    MAX_EMBEDDING_CONCURRENCY,
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
    # 多批次采用有界并发调度，服务端收到请求的先后不固定，但切片集合与返回向量严格保序。
    assert {tuple(req[1]["input"]) for req in seen_requests} == {
        ("a", "bb"),
        ("ccc", "dddd"),
        ("eeeee",),
    }
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


def test_embed_enforces_max_response_bytes_and_validates_content_length() -> None:
    """验证响应字节上限（含 Content-Length 声明、负值/非法长度与流式分片累计超限）。"""

    class _ChunkedStream(httpx2.AsyncByteStream):
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = chunks
            self.closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in self._chunks:
                yield chunk

        async def aclose(self) -> None:
            self.closed = True

    config = OpenAICompatibleEmbeddingConfig(
        base_url="http://127.0.0.1:8000/v1",
        dimensions=4,
    )

    # 1) 流式分片累计超限 -> embedding_response_too_large 且底层流关闭。
    stream_oversize = _ChunkedStream([b'{"data":', b'[{"index":0,"embedding":[0.1,0.2,0.3,0.4]}]}'])

    class _StreamTransport(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, stream=stream_oversize, request=request)

        async def aclose(self) -> None:
            return None

    async def run_stream_oversize() -> None:
        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=_StreamTransport(),
            max_response_bytes=16,
        )
        try:
            await client.embed(["hello"])
        finally:
            await client.aclose()

    with pytest.raises(EmbeddingProtocolError, match="embedding_response_too_large"):
        asyncio.run(run_stream_oversize())
    assert stream_oversize.closed is True

    # 2) Content-Length 声明超限与非法/负值长度在读取前直接拒绝。
    for content_length, expected_code in (
        ("9999", "embedding_response_too_large"),
        ("invalid-int", "embedding_response_length_invalid"),
        ("-10", "embedding_response_length_invalid"),
    ):
        unconsumed = _ChunkedStream([b"unused"])

        class _HeaderTransport(httpx2.AsyncBaseTransport):
            def __init__(self, raw_len: str, resp_stream: _ChunkedStream) -> None:
                self._raw_len = raw_len
                self._resp_stream = resp_stream

            async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
                return httpx2.Response(
                    200,
                    headers={"content-length": self._raw_len},
                    stream=self._resp_stream,
                    request=request,
                )

            async def aclose(self) -> None:
                return None

        async def run_header_case(
            raw_len: str = content_length,
            resp_stream: _ChunkedStream = unconsumed,
        ) -> None:
            client = OpenAICompatibleEmbeddingClient(
                config,
                transport=_HeaderTransport(raw_len, resp_stream),
                max_response_bytes=64,
            )
            try:
                await client.embed(["hello"])
            finally:
                await client.aclose()

        with pytest.raises(EmbeddingProtocolError, match=expected_code):
            asyncio.run(run_header_case())
        assert unconsumed.closed is True


def test_embed_respects_retry_after_header_with_cap_and_ignores_invalid() -> None:
    """429 携带合法 Retry-After 时在安全上限内生效，非法值回退至默认指数退避。"""
    attempts = 0
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    def handler(_request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx2.Response(429, headers={"retry-after": "2.0"}, text="slow down")
        if attempts == 2:
            return httpx2.Response(429, headers={"retry-after": "120"}, text="still busy")
        return _embedding_response([(0, [0.1, 0.2, 0.3, 0.4])])

    config = OpenAICompatibleEmbeddingConfig(
        base_url="http://127.0.0.1:8000/v1",
        dimensions=4,
    )

    async def scenario() -> list[list[float]]:
        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=httpx2.MockTransport(handler),
            sleep=fake_sleep,
        )
        try:
            return await client.embed(["retry-after"])
        finally:
            await client.aclose()

    assert asyncio.run(scenario()) == [[0.1, 0.2, 0.3, 0.4]]
    assert attempts == 3
    assert delays == [2.0, 5.0]


def test_embed_without_api_key_never_uses_openai_env_key_or_sends_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未配置 API Key 时，即使存在 OPENAI_API_KEY 环境变量也绝不发送 Authorization。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-must-never-be-sent")
    seen_auth: list[str | None] = []
    seen_body: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_auth.append(request.headers.get("authorization"))
        seen_body.append(json.loads(request.content.decode("utf-8")))
        return _embedding_response([(0, [0.1, 0.2, 0.3, 0.4])])

    config = OpenAICompatibleEmbeddingConfig(
        base_url="http://127.0.0.1:8000/v1",
        api_key=None,
        dimensions=4,
        send_dimensions=False,
    )

    async def scenario() -> list[list[float]]:
        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=httpx2.MockTransport(handler),
        )
        try:
            return await client.embed(["local-no-auth"])
        finally:
            await client.aclose()

    assert asyncio.run(scenario()) == [[0.1, 0.2, 0.3, 0.4]]
    assert seen_auth == [None]
    assert "dimensions" not in seen_body[0]
    assert seen_body[0]["encoding_format"] == "float"


def test_embed_rejects_redirect_without_following_and_propagates_cancellation() -> None:
    """重定向不跟随直接映射为不可重试错误；取消信号透传不重试。"""
    attempts = 0

    def redirect_handler(_request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        return httpx2.Response(302, headers={"location": "http://other.example/v1/embeddings"})

    config = OpenAICompatibleEmbeddingConfig(
        base_url="http://127.0.0.1:8000/v1",
        api_key="sk-no-redirect-leak",
        dimensions=4,
    )

    async def run_redirect() -> None:
        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=httpx2.MockTransport(redirect_handler),
        )
        try:
            await client.embed(["redirect"])
        finally:
            await client.aclose()

    with pytest.raises(EmbeddingUpstreamError, match="embedding_upstream_error") as exc_info:
        asyncio.run(run_redirect())
    assert attempts == 1
    assert exc_info.value.retryable is False
    assert exc_info.value.status_code == 302

    cancel_attempts = 0

    def cancel_handler(_request: httpx2.Request) -> httpx2.Response:
        nonlocal cancel_attempts
        cancel_attempts += 1
        raise asyncio.CancelledError()

    async def run_cancel() -> None:
        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=httpx2.MockTransport(cancel_handler),
        )
        try:
            await client.embed(["cancel"])
        finally:
            await client.aclose()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_cancel())
    assert cancel_attempts == 1


@pytest.mark.parametrize("encoding", ("gzip", "deflate"))
def test_embed_decodes_valid_streaming_compressed_response(encoding: str) -> None:
    """200 OK 合法 gzip/deflate 流式响应应正常解压解码并返回向量，同时关闭底层流。"""

    class _Stream(httpx2.AsyncByteStream):
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = chunks
            self.closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in self._chunks:
                yield chunk

        async def aclose(self) -> None:
            self.closed = True

    raw_json = json.dumps(
        {"object": "list", "data": [{"index": 0, "embedding": [0.25, 0.5, 0.75, 1.0]}]}
    ).encode("utf-8")
    compressed = gzip.compress(raw_json) if encoding == "gzip" else zlib.compress(raw_json)
    mid = max(1, len(compressed) // 2)
    response_stream = _Stream([compressed[:mid], compressed[mid:]])
    seen_accept_encoding: list[str | None] = []

    class _CompressedTransport(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            seen_accept_encoding.append(request.headers.get("accept-encoding"))
            return httpx2.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "content-encoding": encoding,
                },
                stream=response_stream,
                request=request,
            )

        async def aclose(self) -> None:
            return None

    config = OpenAICompatibleEmbeddingConfig(
        base_url="http://127.0.0.1:8000/v1",
        dimensions=4,
    )

    async def scenario() -> list[list[float]]:
        client = OpenAICompatibleEmbeddingClient(config, transport=_CompressedTransport())
        try:
            return await client.embed(["compressed"])
        finally:
            await client.aclose()

    assert asyncio.run(scenario()) == [[0.25, 0.5, 0.75, 1.0]]
    assert response_stream.closed is True
    assert seen_accept_encoding == ["gzip, deflate"]


def test_embed_rejects_200_gzip_zip_bomb_and_closes_stream() -> None:
    """200 OK 压缩体积在上限内但解压后超限（zip-bomb）必须立即中止并关闭底层流。"""

    class _TrackedStream(httpx2.AsyncByteStream):
        def __init__(self, body: bytes) -> None:
            self._body = body
            self.closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield self._body

        async def aclose(self) -> None:
            self.closed = True

    # 压缩后不足 500 字节（远低于 1024 上限），解压后 64 KiB 远超上限。
    bomb = gzip.compress(b"A" * 65_536)
    assert len(bomb) < 1024
    bomb_stream = _TrackedStream(bomb)

    class _BombTransport(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "content-encoding": "gzip",
                    "content-length": str(len(bomb)),
                },
                stream=bomb_stream,
                request=request,
            )

        async def aclose(self) -> None:
            return None

    config = OpenAICompatibleEmbeddingConfig(
        base_url="http://127.0.0.1:8000/v1",
        dimensions=4,
    )

    async def scenario() -> None:
        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=_BombTransport(),
            max_response_bytes=1024,
        )
        try:
            await client.embed(["zip-bomb"])
        finally:
            await client.aclose()

    with pytest.raises(EmbeddingProtocolError, match="embedding_response_too_large"):
        asyncio.run(scenario())
    assert bomb_stream.closed is True


@pytest.mark.parametrize("payload_kind", ("zip_bomb", "corrupt_gzip"))
def test_embed_closes_non_2xx_compressed_error_streams_without_reading_body(
    payload_kind: str,
) -> None:
    """非 2xx（500/502/429）zip-bomb 与损坏 gzip 必须在 0 字节读取前提下关闭并正确映射或重试。"""

    class _TrackedStream(httpx2.AsyncByteStream):
        def __init__(self, body: bytes) -> None:
            self._body = body
            self.iterated = False
            self.closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            self.iterated = True
            yield self._body

        async def aclose(self) -> None:
            self.closed = True

    error_body = (
        gzip.compress(b"B" * 131_072) if payload_kind == "zip_bomb" else b"corrupted-gzip-stream"
    )
    config = OpenAICompatibleEmbeddingConfig(
        base_url="http://127.0.0.1:8000/v1",
        dimensions=4,
    )

    # 1) 500 错误体不读取、不解压，直接映射为不可重试 embedding_upstream_error。
    stream_500 = _TrackedStream(error_body)

    class _Transport500(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                500,
                headers={"content-encoding": "gzip"},
                stream=stream_500,
                request=request,
            )

        async def aclose(self) -> None:
            return None

    async def run_500() -> None:
        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=_Transport500(),
            max_response_bytes=1024,
        )
        try:
            await client.embed(["err-500"])
        finally:
            await client.aclose()

    with pytest.raises(EmbeddingUpstreamError, match="embedding_upstream_error") as exc_500:
        asyncio.run(run_500())
    assert exc_500.value.retryable is False
    assert exc_500.value.status_code == 500
    assert stream_500.iterated is False
    assert stream_500.closed is True

    # 2) 502 / 429（携带 Retry-After）错误体均不读取、不解压，按既定策略完成退避重试并成功。
    retry_streams: list[_TrackedStream] = []
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    class _RetryTransport(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            attempt = len(retry_streams)
            if attempt == 0:
                stream = _TrackedStream(error_body)
                retry_streams.append(stream)
                return httpx2.Response(
                    429,
                    headers={"content-encoding": "gzip", "retry-after": "1.5"},
                    stream=stream,
                    request=request,
                )
            if attempt == 1:
                stream = _TrackedStream(error_body)
                retry_streams.append(stream)
                return httpx2.Response(
                    502,
                    headers={"content-encoding": "gzip"},
                    stream=stream,
                    request=request,
                )
            ok_payload = json.dumps(
                {"object": "list", "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]}]}
            ).encode("utf-8")
            ok_stream = _TrackedStream(ok_payload)
            retry_streams.append(ok_stream)
            return httpx2.Response(200, stream=ok_stream, request=request)

        async def aclose(self) -> None:
            return None

    async def run_retry() -> list[list[float]]:
        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=_RetryTransport(),
            sleep=fake_sleep,
            max_response_bytes=1024,
        )
        try:
            return await client.embed(["retry-compressed-errors"])
        finally:
            await client.aclose()

    assert asyncio.run(run_retry()) == [[0.1, 0.2, 0.3, 0.4]]
    assert delays == [1.5, 0.5]
    assert len(retry_streams) == 3
    # 前两次 429/502 均在 0 字节读取下直接关闭。
    assert retry_streams[0].iterated is False and retry_streams[0].closed is True
    assert retry_streams[1].iterated is False and retry_streams[1].closed is True
    assert retry_streams[2].iterated is True and retry_streams[2].closed is True


def test_embed_streaming_corrupted_gzip_maps_decoding_error_closes_stream_and_hides_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """200 流式损坏 gzip 在 _read_bounded_body 触发 DecodingError 时安全映射、关流且零密钥泄露。"""
    secret_key = "sk-stream-gzip-secret-99999"

    class _CorruptGzipStream(httpx2.AsyncByteStream):
        def __init__(self) -> None:
            self.iterated = False
            self.closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            self.iterated = True
            yield b"definitely-not-gzip-bytes"

        async def aclose(self) -> None:
            self.closed = True

    corrupt_stream = _CorruptGzipStream()

    class _StreamTransport(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "content-encoding": "gzip",
                },
                stream=corrupt_stream,
                request=request,
            )

        async def aclose(self) -> None:
            return None

    config = OpenAICompatibleEmbeddingConfig(
        base_url="http://127.0.0.1:8000/v1",
        api_key=secret_key,
        dimensions=4,
    )

    async def scenario() -> None:
        client = OpenAICompatibleEmbeddingClient(config, transport=_StreamTransport())
        try:
            await client.embed(["corrupt-stream"])
        finally:
            await client.aclose()

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(
            EmbeddingProtocolError, match="embedding_response_decode_failed"
        ) as exc_info:
            asyncio.run(scenario())

    assert corrupt_stream.iterated is True
    assert corrupt_stream.closed is True
    assert exc_info.value.__cause__ is None
    assert secret_key not in str(exc_info.value)
    assert secret_key not in repr(exc_info.value)
    assert secret_key not in caplog.text


def test_embed_rejects_huge_integer_overflow_as_non_finite_vector() -> None:
    """向量元素包含超出 float 范围的超大整数（10**400）时映射为 embedding_vector_non_finite。"""
    huge_int = str(10**400).encode("ascii")
    raw_body = b'{"data":[{"index":0,"embedding":[0.1,' + huge_int + b",0.3,0.4]}]}"

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            content=raw_body,
            headers={"content-type": "application/json"},
        )

    config = OpenAICompatibleEmbeddingConfig(
        base_url="http://127.0.0.1:8000/v1",
        dimensions=4,
    )

    async def scenario() -> None:
        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=httpx2.MockTransport(handler),
        )
        try:
            await client.embed(["huge-int"])
        finally:
            await client.aclose()

    with pytest.raises(EmbeddingProtocolError, match="embedding_vector_non_finite"):
        asyncio.run(scenario())


def test_client_validates_max_concurrency_bounds_and_types() -> None:
    """max_concurrency 非法值（<1、>16、bool、非整型）必须在构造期明确拒绝，合法边界值正常生效。"""
    config = OpenAICompatibleEmbeddingConfig(
        base_url="http://127.0.0.1:8000/v1",
        dimensions=4,
    )

    for bad_concurrency in (
        0,
        -1,
        MAX_EMBEDDING_CONCURRENCY + 1,
        100,
        True,
        False,
        2.5,
        "4",
        None,
    ):
        with pytest.raises(ValueError, match="embedding_max_concurrency_invalid"):
            OpenAICompatibleEmbeddingClient(
                config,
                max_concurrency=cast(Any, bad_concurrency),
            )

    # 合法边界值（1 / 默认值 4 / 上限 16）与 from_environment 工厂均正常构造且保留精确并发度。
    for valid_concurrency in (1, DEFAULT_EMBEDDING_MAX_CONCURRENCY, MAX_EMBEDDING_CONCURRENCY):
        client = OpenAICompatibleEmbeddingClient(config, max_concurrency=valid_concurrency)
        assert client._max_concurrency == valid_concurrency

    env_client = OpenAICompatibleEmbeddingClient.from_environment(
        {"ATHENA_EMBEDDING_BASE_URL": "http://127.0.0.1:8000/v1"},
        expected_dimensions=1024,
    )
    assert env_client._max_concurrency == DEFAULT_EMBEDDING_MAX_CONCURRENCY


def test_embed_multi_batch_concurrency_preserves_order_and_cancels_siblings_on_failure() -> None:
    """验证多批 Semaphore 并发上限、乱序完成保序拼接，以及任一批失败时安全取消兄弟任务。"""
    config = OpenAICompatibleEmbeddingConfig(
        base_url="http://127.0.0.1:8000/v1",
        dimensions=4,
        batch_size=1,
    )

    # 1) 验证 Semaphore(3) 受控并发上限与后批先完成时的严格保序拼接。
    async def verify_semaphore_and_order() -> None:
        in_flight = 0
        max_in_flight = 0
        concurrency_reached = asyncio.Event()
        batch_2_done = asyncio.Event()
        batch_1_done = asyncio.Event()
        completion_order: list[int] = []

        async def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal in_flight, max_in_flight
            body = json.loads(request.content.decode("utf-8"))
            idx = int(body["input"][0])
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            if in_flight == 3:
                concurrency_reached.set()
            try:
                if idx < 3:
                    # 等待 3 个并发槽位同时打满，再人为令 batch 2 -> 1 -> 0 逆序完成。
                    await asyncio.wait_for(concurrency_reached.wait(), timeout=2.0)
                    if idx == 1:
                        await asyncio.wait_for(batch_2_done.wait(), timeout=2.0)
                    elif idx == 0:
                        await asyncio.wait_for(batch_1_done.wait(), timeout=2.0)
                completion_order.append(idx)
                if idx == 2:
                    batch_2_done.set()
                elif idx == 1:
                    batch_1_done.set()
            finally:
                in_flight -= 1
            return _embedding_response([(0, [float(idx), 1.0, 2.0, 3.0])])

        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=httpx2.MockTransport(handler),
            max_concurrency=3,
        )
        try:
            vectors = await client.embed([str(i) for i in range(6)])
        finally:
            await client.aclose()

        assert max_in_flight == 3
        assert completion_order.index(2) < completion_order.index(1) < completion_order.index(0)
        assert vectors == [[float(i), 1.0, 2.0, 3.0] for i in range(6)]

    asyncio.run(verify_semaphore_and_order())

    # 2) 验证任一批失败后立即取消在途及排队兄弟任务，且零未回收任务与零未检索异常。
    async def verify_sibling_cancellation() -> None:
        loop = asyncio.get_running_loop()
        loop_exceptions: list[dict[str, Any]] = []
        loop.set_exception_handler(lambda _loop, context: loop_exceptions.append(context))

        in_flight_slow = 0
        siblings_ready = asyncio.Event()
        cancelled_siblings = 0
        started_tokens: list[str] = []

        async def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal in_flight_slow, cancelled_siblings
            token = json.loads(request.content.decode("utf-8"))["input"][0]
            started_tokens.append(token)
            if token == "fail":
                await asyncio.wait_for(siblings_ready.wait(), timeout=2.0)
                return httpx2.Response(400, text="bad request")
            in_flight_slow += 1
            if in_flight_slow >= 2:
                siblings_ready.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled_siblings += 1
                raise
            return _embedding_response([(0, [0.0, 0.0, 0.0, 0.0])])

        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=httpx2.MockTransport(handler),
            max_concurrency=4,
        )
        current = asyncio.current_task()
        before_tasks = {task for task in asyncio.all_tasks() if task is not current}
        try:
            with pytest.raises(EmbeddingUpstreamError, match="embedding_request_invalid"):
                await client.embed(["slow-1", "slow-2", "fail", "slow-3", "slow-4", "slow-5"])
        finally:
            await client.aclose()

        # 让出事件循环并检查任务列表：已启动兄弟批必须全部取消，尾部排队批未启动，零泄露任务/异常。
        await asyncio.sleep(0)
        after_tasks = {task for task in asyncio.all_tasks() if task is not current}
        started_siblings = [token for token in started_tokens if token != "fail"]
        assert len(started_siblings) >= 2
        assert cancelled_siblings == len(started_siblings)
        assert "slow-5" not in started_tokens
        assert after_tasks == before_tasks
        assert loop_exceptions == []

    asyncio.run(verify_sibling_cancellation())

    # 3) 验证同 tick 多个批次同时失败时全部已完成任务异常均被回收，不触发未检索异常告警。
    async def verify_multi_failure_same_tick() -> None:
        loop = asyncio.get_running_loop()
        loop_exceptions: list[dict[str, Any]] = []
        loop.set_exception_handler(lambda _loop, context: loop_exceptions.append(context))

        arrived = 0
        both_ready = asyncio.Event()

        async def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal arrived
            token = json.loads(request.content.decode("utf-8"))["input"][0]
            arrived += 1
            if arrived == 2:
                both_ready.set()
            await asyncio.wait_for(both_ready.wait(), timeout=2.0)
            status = 400 if token == "fail-a" else 500
            return httpx2.Response(status, text="boom")

        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=httpx2.MockTransport(handler),
            max_concurrency=2,
        )
        try:
            with pytest.raises(EmbeddingUpstreamError):
                await client.embed(["fail-a", "fail-b"])
        finally:
            await client.aclose()

        await asyncio.sleep(0)
        assert loop_exceptions == []

    asyncio.run(verify_multi_failure_same_tick())


def test_embed_retries_connect_and_remote_protocol_errors_without_leaking_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ConnectError / RemoteProtocolError 应指数退避重试，耗尽报 retryable=True 且零密钥泄露。"""
    secret_key = "sk-transient-transport-secret-424242"
    config = OpenAICompatibleEmbeddingConfig(
        base_url="http://127.0.0.1:8000/v1",
        api_key=secret_key,
        dimensions=4,
    )

    # 1) 依次遭遇 ConnectError 与 RemoteProtocolError 后于第 3 次尝试恢复成功。
    recover_attempts = 0
    recover_delays: list[float] = []

    async def record_recover_sleep(seconds: float) -> None:
        recover_delays.append(seconds)

    def recover_handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal recover_attempts
        recover_attempts += 1
        auth_header = request.headers.get("authorization")
        if recover_attempts == 1:
            raise httpx2.ConnectError(f"connection reset for {auth_header} api_key={secret_key}")
        if recover_attempts == 2:
            raise httpx2.RemoteProtocolError(f"peer eof for {auth_header} api_key={secret_key}")
        return _embedding_response([(0, [0.1, 0.2, 0.3, 0.4])])

    async def run_recover() -> list[list[float]]:
        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=httpx2.MockTransport(recover_handler),
            sleep=record_recover_sleep,
        )
        try:
            return await client.embed(["transient-recover"])
        finally:
            await client.aclose()

    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(run_recover()) == [[0.1, 0.2, 0.3, 0.4]]

    assert recover_attempts == 3
    assert recover_delays == [0.25, 0.5]
    assert "embedding_transport_retry" in caplog.text
    assert secret_key not in caplog.text

    # 2) ConnectError 与 RemoteProtocolError 持续耗尽后均抛出 retryable=True、截断异常链且不泄密钥。
    for error_cls in (httpx2.ConnectError, httpx2.RemoteProtocolError):
        caplog.clear()
        exhausted_attempts = 0
        exhausted_delays: list[float] = []

        async def record_exhausted_sleep(
            seconds: float,
            sink: list[float] = exhausted_delays,
        ) -> None:
            sink.append(seconds)

        def failing_handler(
            request: httpx2.Request,
            exc_type: type[httpx2.HTTPError] = error_cls,
        ) -> httpx2.Response:
            nonlocal exhausted_attempts
            exhausted_attempts += 1
            raise exc_type(
                f"transport failed header={request.headers.get('authorization')} key={secret_key}"
            )

        async def run_exhausted(
            handler_fn: Any = failing_handler,
            sleep_fn: Any = record_exhausted_sleep,
        ) -> None:
            client = OpenAICompatibleEmbeddingClient(
                config,
                transport=httpx2.MockTransport(handler_fn),
                sleep=sleep_fn,
            )
            try:
                await client.embed(["transient-exhausted"])
            finally:
                await client.aclose()

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(
                EmbeddingUpstreamError, match="embedding_transport_error"
            ) as exc_info:
                asyncio.run(run_exhausted())

        assert exhausted_attempts == 3
        assert exhausted_delays == [0.25, 0.5]
        assert exc_info.value.retryable is True
        assert exc_info.value.status_code is None
        assert exc_info.value.__cause__ is None
        assert secret_key not in str(exc_info.value)
        assert secret_key not in repr(exc_info.value)
        assert secret_key not in caplog.text
