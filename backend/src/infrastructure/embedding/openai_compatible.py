"""OpenAI 兼容远程 Embedding 服务客户端，实现知识库向量化无状态化。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

import httpx2
import openai
from openai import AsyncOpenAI

from infrastructure.http import (
    ResponseLimitError,
    RetryableHttpError,
    build_bounded_async_client,
    build_http_timeout,
    find_response_limit_error,
    has_control_characters,
    run_with_http_retry,
    validate_http_base_url,
)

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"
DEFAULT_EMBEDDING_DIMENSIONS = 1024
DEFAULT_EMBEDDING_TIMEOUT_SECONDS = 30.0
DEFAULT_EMBEDDING_BATCH_SIZE = 32
DEFAULT_EMBEDDING_MAX_CONCURRENCY = 4
MIN_EMBEDDING_TIMEOUT_SECONDS = 0.1
MAX_EMBEDDING_TIMEOUT_SECONDS = 300.0
MAX_EMBEDDING_BATCH_SIZE = 256
MAX_EMBEDDING_CONCURRENCY = 16
MAX_EMBEDDING_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_EMBEDDING_RETRIES = 2

_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})
_BASE_BACKOFF_SECONDS = 0.25
_MAX_BACKOFF_SECONDS = 5.0
# 当未配置 API Key 时用于阻断 AsyncOpenAI 自动回退读取进程环境中的 OPENAI_API_KEY，
# 并在每次请求中显式通过 openai.omit 移除 Authorization 请求头，绝不向远端发送占位值。
_OMITTED_API_KEY_PLACEHOLDER = "athena-embedding-no-auth"


class _StreamedBytesResponse(Protocol):
    def iter_bytes(self, chunk_size: int | None = None) -> AsyncIterator[bytes]: ...


class EmbeddingError(RuntimeError):
    """远程 Embedding 基础设施的稳定安全异常基类，不包含密钥或原始响应内容。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class EmbeddingConfigurationError(EmbeddingError):
    """远程 Embedding 配置缺失、格式非法或与向量库维度约束冲突。"""

    def __init__(self, code: str = "embedding_config_invalid") -> None:
        super().__init__(code)


class EmbeddingUpstreamError(EmbeddingError):
    """远程 Embedding 上游请求失败、限流、超时或鉴权拒绝。"""

    def __init__(
        self,
        code: str = "embedding_upstream_error",
        *,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        self.retryable = retryable
        self.status_code = status_code
        super().__init__(code)


class EmbeddingProtocolError(EmbeddingError):
    """远程 Embedding 返回了非法 JSON、错误数量、非有限浮点或错误维度向量。"""

    def __init__(self, code: str = "embedding_response_invalid") -> None:
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class OpenAICompatibleEmbeddingConfig:
    """经过严格校验的 OpenAI 兼容远程 Embedding 服务配置。"""

    base_url: str
    api_key: str | None = field(default=None, repr=False)
    model: str = DEFAULT_EMBEDDING_MODEL
    dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS
    timeout_seconds: float = DEFAULT_EMBEDDING_TIMEOUT_SECONDS
    batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE
    send_dimensions: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str):
            raise EmbeddingConfigurationError("embedding_base_url_invalid")
        _validate_base_url(self.base_url)
        if self.api_key is not None:
            if (
                not isinstance(self.api_key, str)
                or not self.api_key.strip()
                or has_control_characters(self.api_key)
            ):
                raise EmbeddingConfigurationError("embedding_api_key_invalid")
        if (
            not isinstance(self.model, str)
            or not self.model.strip()
            or len(self.model) > 256
            or has_control_characters(self.model)
        ):
            raise EmbeddingConfigurationError("embedding_model_invalid")
        if (
            not isinstance(self.dimensions, int)
            or isinstance(self.dimensions, bool)
            or self.dimensions < 1
        ):
            raise EmbeddingConfigurationError("embedding_dimensions_invalid")
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or not math.isfinite(self.timeout_seconds)
            or not MIN_EMBEDDING_TIMEOUT_SECONDS
            <= self.timeout_seconds
            <= MAX_EMBEDDING_TIMEOUT_SECONDS
        ):
            raise EmbeddingConfigurationError("embedding_timeout_invalid")
        if (
            not isinstance(self.batch_size, int)
            or isinstance(self.batch_size, bool)
            or not 1 <= self.batch_size <= MAX_EMBEDDING_BATCH_SIZE
        ):
            raise EmbeddingConfigurationError("embedding_batch_size_invalid")
        if not isinstance(self.send_dimensions, bool):
            raise EmbeddingConfigurationError("embedding_send_dimensions_invalid")

    @property
    def embeddings_url(self) -> str:
        """返回规范化的 OpenAI 兼容 /embeddings 请求地址。"""
        return f"{self.base_url.rstrip('/')}/embeddings"

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        expected_dimensions: int | None = None,
    ) -> OpenAICompatibleEmbeddingConfig:
        """从统一 ATHENA_EMBEDDING_* 环境变量加载并校验配置。

        当提供 expected_dimensions（如知识库 PostgreSQL Vector(1024) 约束）时，
        启动阶段立即校验维度一致性，避免写入或检索不兼容向量。
        """
        source = os.environ if environ is None else environ
        raw_base_url = source.get("ATHENA_EMBEDDING_BASE_URL")
        if raw_base_url is None or not raw_base_url.strip():
            raise EmbeddingConfigurationError("embedding_base_url_missing")
        # 保留去除首尾空白前对控制字符的拒绝语义，只去除环境配置常见的首尾空格。
        if has_control_characters(raw_base_url):
            raise EmbeddingConfigurationError("embedding_base_url_invalid")
        base_url = raw_base_url.strip()

        raw_api_key = source.get("ATHENA_EMBEDDING_API_KEY")
        api_key: str | None
        if raw_api_key is None or not raw_api_key.strip():
            api_key = None
        else:
            if has_control_characters(raw_api_key):
                raise EmbeddingConfigurationError("embedding_api_key_invalid")
            api_key = raw_api_key.strip()

        raw_model = source.get("ATHENA_EMBEDDING_MODEL")
        model = (
            DEFAULT_EMBEDDING_MODEL
            if raw_model is None or not raw_model.strip()
            else raw_model.strip()
        )
        if raw_model is not None and has_control_characters(raw_model):
            raise EmbeddingConfigurationError("embedding_model_invalid")

        dimensions = _parse_positive_int(
            source,
            "ATHENA_EMBEDDING_DIMENSIONS",
            default=DEFAULT_EMBEDDING_DIMENSIONS,
            code="embedding_dimensions_invalid",
        )
        if expected_dimensions is not None and dimensions != expected_dimensions:
            raise EmbeddingConfigurationError("embedding_dimensions_mismatch")

        timeout_seconds = _parse_bounded_float(
            source,
            "ATHENA_EMBEDDING_TIMEOUT_SECONDS",
            default=DEFAULT_EMBEDDING_TIMEOUT_SECONDS,
            minimum=MIN_EMBEDDING_TIMEOUT_SECONDS,
            maximum=MAX_EMBEDDING_TIMEOUT_SECONDS,
            code="embedding_timeout_invalid",
        )
        batch_size = _parse_positive_int(
            source,
            "ATHENA_EMBEDDING_BATCH_SIZE",
            default=DEFAULT_EMBEDDING_BATCH_SIZE,
            maximum=MAX_EMBEDDING_BATCH_SIZE,
            code="embedding_batch_size_invalid",
        )
        send_dimensions = _parse_strict_flag(
            source,
            "ATHENA_EMBEDDING_SEND_DIMENSIONS",
            default=True,
            code="embedding_send_dimensions_invalid",
        )
        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            dimensions=dimensions,
            timeout_seconds=timeout_seconds,
            batch_size=batch_size,
            send_dimensions=send_dimensions,
        )


class OpenAICompatibleEmbeddingClient:
    """基于 AsyncOpenAI 与共享有界 HTTP 基座的 OpenAI 兼容远程 Embedding 客户端。

    对外契约与知识库端口保持一致：start() / embed(texts) / aclose()。
    """

    def __init__(
        self,
        config: OpenAICompatibleEmbeddingConfig,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_retries: int = MAX_EMBEDDING_RETRIES,
        max_response_bytes: int = MAX_EMBEDDING_RESPONSE_BYTES,
        max_concurrency: int = DEFAULT_EMBEDDING_MAX_CONCURRENCY,
    ) -> None:
        if max_retries < 0 or max_retries > 5:
            raise ValueError("embedding_max_retries_invalid")
        if max_response_bytes < 1:
            raise ValueError("embedding_max_response_bytes_invalid")
        if (
            not isinstance(max_concurrency, int)
            or isinstance(max_concurrency, bool)
            or not 1 <= max_concurrency <= MAX_EMBEDDING_CONCURRENCY
        ):
            raise ValueError("embedding_max_concurrency_invalid")
        self._config = config
        self._transport = transport
        self._sleep = sleep
        self._max_retries = max_retries
        self._max_response_bytes = max_response_bytes
        self._max_concurrency = max_concurrency
        self._client: AsyncOpenAI | None = None
        self._closed = False
        self._lock = asyncio.Lock()

    @property
    def config(self) -> OpenAICompatibleEmbeddingConfig:
        """暴露只读配置供组合根或测试检查（repr 已屏蔽 API Key）。"""
        return self._config

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        expected_dimensions: int | None = None,
    ) -> OpenAICompatibleEmbeddingClient:
        """按环境变量创建远程 Embedding 客户端。"""
        return cls(
            OpenAICompatibleEmbeddingConfig.from_environment(
                environ,
                expected_dimensions=expected_dimensions,
            )
        )

    async def start(self) -> None:
        """初始化可复用的有界异步 OpenAI SDK 客户端与连接池。"""
        async with self._lock:
            if self._closed:
                raise EmbeddingUpstreamError("embedding_client_closed")
            if self._client is not None:
                return
            self._client = self._build_client()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """将文本列表按 batch_size 自动分批受控并发向量化，保序拼接并逐条校验维度与有限性。"""
        if self._closed:
            raise EmbeddingUpstreamError("embedding_client_closed")
        if not texts:
            return []
        client = await self._ensure_client()
        batch_size = self._config.batch_size
        batches = [
            texts[start_index : start_index + batch_size]
            for start_index in range(0, len(texts), batch_size)
        ]
        # 单批次快路径（如在线 RAG 查询单条文本）直接执行，避免额外的信号量与协程调度开销。
        if len(batches) == 1:
            try:
                single_vectors = await self._embed_batch(client, batches[0])
            except RuntimeError:
                if self._closed:
                    raise EmbeddingUpstreamError("embedding_client_closed") from None
                raise
            if self._closed:
                raise EmbeddingUpstreamError("embedding_client_closed")
            return single_vectors

        # 多批次受控并发（默认 Semaphore(4)）：按批次下标保序回填，
        # 任一批失败立即取消同组未完成批次。
        semaphore = asyncio.Semaphore(self._max_concurrency)
        ordered_batches: list[list[list[float]] | None] = [None] * len(batches)

        async def _run_batch(batch_index: int, batch: list[str]) -> None:
            async with semaphore:
                # 严守 aclose() 竞态防护：拿到信号量后发请求前复核关闭状态。
                if self._closed:
                    raise EmbeddingUpstreamError("embedding_client_closed")
                try:
                    batch_vectors = await self._embed_batch(client, batch)
                except RuntimeError:
                    if self._closed:
                        raise EmbeddingUpstreamError("embedding_client_closed") from None
                    raise
                if self._closed:
                    raise EmbeddingUpstreamError("embedding_client_closed")
                ordered_batches[batch_index] = batch_vectors

        tasks = [
            asyncio.create_task(_run_batch(batch_index, batch))
            for batch_index, batch in enumerate(batches)
        ]
        try:
            pending: set[asyncio.Task[None]] = set(tasks)
            while pending:
                done, pending = await asyncio.wait(
                    pending,
                    return_when=asyncio.FIRST_EXCEPTION,
                )
                first_error: BaseException | None = None
                for finished in done:
                    if finished.cancelled():
                        if first_error is None:
                            first_error = asyncio.CancelledError()
                        continue
                    # 统一提取全部已完成任务异常，避免同 tick 多批失败遗留未检索异常告警。
                    error = finished.exception()
                    if error is not None and first_error is None:
                        first_error = error
                if first_error is not None:
                    for other in pending:
                        other.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    if self._closed and not isinstance(
                        first_error, (asyncio.CancelledError, EmbeddingError)
                    ):
                        raise EmbeddingUpstreamError("embedding_client_closed") from None
                    raise first_error
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        # 返回前再次复核关闭状态，防止并发 aclose() 完成瞬间返回部分或脏结果。
        if self._closed:
            raise EmbeddingUpstreamError("embedding_client_closed")

        results: list[list[float]] = []
        for batch_vectors in ordered_batches:
            assert batch_vectors is not None
            results.extend(batch_vectors)
        return results

    async def aclose(self) -> None:
        """标记关闭并释放底层 SDK 客户端与 HTTP 连接池，可安全重复调用。"""
        async with self._lock:
            self._closed = True
            client = self._client
            self._client = None
        if client is not None:
            await client.close()

    def _build_client(self) -> AsyncOpenAI:
        """构造挂载有界超时、有界连接池、禁重定向与字节限流的 AsyncOpenAI 客户端。"""
        timeout = build_http_timeout(self._config.timeout_seconds)
        # 显式协商 gzip/deflate 压缩降低大向量响应带宽，同时放开 transport 层非 identity 拒绝；
        # 解压后的明文字节上限由 _read_bounded_body 流式强制校验（默认 16 MiB），防范 gzip 炸弹。
        http_client = build_bounded_async_client(
            timeout_seconds=self._config.timeout_seconds,
            max_response_bytes=self._max_response_bytes,
            transport=self._transport,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
            },
            too_large_code="embedding_response_too_large",
            length_invalid_code="embedding_response_length_invalid",
            encoding_unsupported_code="embedding_response_decode_failed",
            reject_non_identity_encoding=False,
        )
        # 注册非 2xx 响应提前关闭钩子：阻止 AsyncOpenAI 对错误体调用 aread()/解压，
        # 既彻底杜绝非 2xx zip-bomb OOM，也防止损坏 gzip 错误页触发 DecodingError 绕过重试。
        http_client.event_hooks.setdefault("response", []).append(_close_non_success_response)
        # 禁用 SDK 内置重试，统一由共享 tenacity 执行器控制状态码集合、退避与流释放顺序。
        return AsyncOpenAI(
            api_key=self._config.api_key or _OMITTED_API_KEY_PLACEHOLDER,
            base_url=self._config.base_url,
            timeout=timeout,
            max_retries=0,
            http_client=http_client,
        )

    async def _ensure_client(self) -> AsyncOpenAI:
        """在锁内原子获取或延迟启动连接池，避免与并发 aclose() 竞态触发 AssertionError。"""
        async with self._lock:
            if self._closed:
                raise EmbeddingUpstreamError("embedding_client_closed")
            if self._client is None:
                self._client = self._build_client()
            return self._client

    async def _read_bounded_body(self, response: _StreamedBytesResponse) -> bytes:
        """流式读取并限流解压后响应体，防范小体积压缩包膨胀耗尽内存（gzip 炸弹防护）。"""
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.iter_bytes():
            total += len(chunk)
            if total > self._max_response_bytes:
                raise EmbeddingProtocolError("embedding_response_too_large")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _embed_batch(
        self,
        client: AsyncOpenAI,
        batch: list[str],
    ) -> list[list[float]]:
        """执行单批次远程 Embedding 请求，对 429/502/503/504、超时与瞬态传输故障执行退避重试。"""
        dimensions: int | openai.Omit = (
            self._config.dimensions if self._config.send_dimensions else openai.omit
        )
        # 未配置 API Key 时显式移除 Authorization 头，确保本地或无鉴权服务绝不收到占位凭证。
        extra_headers: Mapping[str, str | openai.Omit] | None = (
            {"Authorization": openai.omit} if self._config.api_key is None else None
        )

        def _on_retry(attempt_number: int, delay: float, error: RetryableHttpError) -> None:
            if error.status_code is not None:
                logger.warning(
                    "embedding_upstream_retry status_code=%d attempt=%d delay=%.2f batch_size=%d",
                    error.status_code,
                    attempt_number,
                    delay,
                    len(batch),
                )
            elif isinstance(error.final_error, EmbeddingUpstreamError) and (
                error.final_error.code == "embedding_timeout"
            ):
                logger.warning(
                    "embedding_timeout_retry attempt=%d delay=%.2f batch_size=%d",
                    attempt_number,
                    delay,
                    len(batch),
                )
            else:
                logger.warning(
                    "embedding_transport_retry attempt=%d delay=%.2f batch_size=%d",
                    attempt_number,
                    delay,
                    len(batch),
                )

        async def _sleep_if_open(seconds: float) -> None:
            if self._closed:
                raise EmbeddingUpstreamError("embedding_client_closed")
            await self._sleep(seconds)
            if self._closed:
                raise EmbeddingUpstreamError("embedding_client_closed")

        async def _attempt(_attempt_index: int) -> list[list[float]]:
            if self._closed:
                raise EmbeddingUpstreamError("embedding_client_closed")
            try:
                # 显式指定 encoding_format="float" 覆盖 OpenAI SDK 默认的 base64 请求格式；
                # 通过 with_streaming_response 确保退出 async with 即关闭响应流并归还连接池，
                # 随后才由 tenacity 执行退避等待。
                async with client.embeddings.with_streaming_response.create(
                    model=self._config.model,
                    input=batch,
                    dimensions=dimensions,
                    encoding_format="float",
                    extra_headers=extra_headers,
                ) as response:
                    raw_body = await self._read_bounded_body(response)
                return self._parse_response(raw_body, expected_count=len(batch))
            except asyncio.CancelledError:
                raise
            except EmbeddingError:
                raise
            except ResponseLimitError as error:
                code = str(error)
                logger.warning(
                    "embedding_protocol_failed code=%s batch_size=%d",
                    code,
                    len(batch),
                )
                raise EmbeddingProtocolError(code) from None
            except openai.APIStatusError as error:
                # 兜底确认异常响应流已关闭，防止重试等待期间长期占用连接池槽位。
                if not error.response.is_closed:
                    try:
                        await error.response.aclose()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning("embedding_response_close_failed")
                if self._closed:
                    raise EmbeddingUpstreamError("embedding_client_closed") from None
                status_code = error.status_code
                if status_code in _RETRYABLE_STATUS_CODES:
                    code = (
                        "embedding_rate_limited"
                        if status_code == 429
                        else "embedding_upstream_unavailable"
                    )
                    raise RetryableHttpError(
                        retry_after_header=error.response.headers.get("retry-after"),
                        final_error=EmbeddingUpstreamError(
                            code,
                            retryable=True,
                            status_code=status_code,
                        ),
                        status_code=status_code,
                    ) from None
                # 4xx 参数/鉴权错误、重定向及非瞬态 5xx 禁止重试，直接映射安全错误码。
                mapped = _map_non_retryable_status(status_code)
                logger.warning(
                    "embedding_upstream_failed code=%s status_code=%d batch_size=%d",
                    mapped.code,
                    status_code,
                    len(batch),
                )
                raise mapped from None
            except (openai.APITimeoutError, httpx2.TimeoutException):
                if self._closed:
                    raise EmbeddingUpstreamError("embedding_client_closed") from None
                # 严禁透传底层超时异常上下文，避免请求头中的 API Key 泄露到日志或堆栈。
                raise RetryableHttpError(
                    retry_after_header=None,
                    final_error=EmbeddingUpstreamError("embedding_timeout", retryable=True),
                    status_code=None,
                ) from None
            except httpx2.DecodingError as error:
                logger.warning(
                    "embedding_protocol_failed code=embedding_response_decode_failed "
                    "exception_type=%s batch_size=%d",
                    type(error).__name__,
                    len(batch),
                )
                raise EmbeddingProtocolError("embedding_response_decode_failed") from None
            except (
                openai.APIConnectionError,
                openai.OpenAIError,
                httpx2.HTTPError,
                httpx2.StreamError,
            ) as error:
                if self._closed:
                    raise EmbeddingUpstreamError("embedding_client_closed") from None
                limit_error = find_response_limit_error(error)
                if limit_error is not None:
                    code = str(limit_error)
                    logger.warning(
                        "embedding_protocol_failed code=%s batch_size=%d",
                        code,
                        len(batch),
                    )
                    raise EmbeddingProtocolError(code) from None
                if _caused_by_decoding_error(error):
                    logger.warning(
                        "embedding_protocol_failed code=embedding_response_decode_failed "
                        "exception_type=%s batch_size=%d",
                        type(error).__name__,
                        len(batch),
                    )
                    raise EmbeddingProtocolError("embedding_response_decode_failed") from None
                if _caused_by_transient_transport_error(error):
                    # 仅对连接重置、断流等瞬态网络/远端协议中断复用既有退避重试；
                    # 此时已退出 with_streaming_response，响应流已释放后再等待。
                    raise RetryableHttpError(
                        retry_after_header=None,
                        final_error=EmbeddingUpstreamError(
                            "embedding_transport_error",
                            retryable=True,
                        ),
                        status_code=None,
                    ) from None
                # 兜底映射其余 SDK / 传输 / StreamError 异常并截断异常链，保持 retryable=False。
                logger.warning(
                    "embedding_upstream_failed code=embedding_transport_error "
                    "exception_type=%s batch_size=%d",
                    type(error).__name__,
                    len(batch),
                )
                raise EmbeddingUpstreamError("embedding_transport_error", retryable=False) from None
            except RuntimeError:
                if self._closed:
                    raise EmbeddingUpstreamError("embedding_client_closed") from None
                raise

        try:
            return await run_with_http_retry(
                _attempt,
                max_retries=self._max_retries,
                sleep=_sleep_if_open,
                base_seconds=_BASE_BACKOFF_SECONDS,
                max_seconds=_MAX_BACKOFF_SECONDS,
                on_retry=_on_retry,
            )
        except EmbeddingUpstreamError as error:
            if error.retryable:
                if error.status_code is not None:
                    logger.warning(
                        "embedding_upstream_failed code=%s status_code=%d batch_size=%d",
                        error.code,
                        error.status_code,
                        len(batch),
                    )
                else:
                    logger.warning(
                        "embedding_upstream_failed code=%s batch_size=%d",
                        error.code,
                        len(batch),
                    )
            raise

    def _parse_response(self, raw_body: bytes, *, expected_count: int) -> list[list[float]]:
        """解析 OpenAI embeddings 响应并按 index 重排、校验维度与有限浮点约束。"""
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise EmbeddingProtocolError("embedding_response_invalid_json") from None

        if not isinstance(payload, dict):
            raise EmbeddingProtocolError("embedding_response_invalid")
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != expected_count:
            raise EmbeddingProtocolError("embedding_count_mismatch")

        ordered: list[list[float] | None] = [None] * expected_count
        expected_dimensions = self._config.dimensions
        for position, item in enumerate(data):
            if not isinstance(item, dict):
                raise EmbeddingProtocolError("embedding_item_invalid")
            if "index" in item:
                raw_index = item["index"]
                if (
                    not isinstance(raw_index, int)
                    or isinstance(raw_index, bool)
                    or not 0 <= raw_index < expected_count
                ):
                    raise EmbeddingProtocolError("embedding_index_invalid")
                index = raw_index
            else:
                index = position
            if ordered[index] is not None:
                raise EmbeddingProtocolError("embedding_index_duplicate")

            raw_vector = item.get("embedding")
            if not isinstance(raw_vector, list):
                raise EmbeddingProtocolError("embedding_vector_invalid")
            if len(raw_vector) != expected_dimensions:
                raise EmbeddingProtocolError("embedding_dimensions_invalid")

            vector: list[float] = []
            for value in raw_vector:
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise EmbeddingProtocolError("embedding_vector_invalid")
                try:
                    numeric = float(value)
                except OverflowError:
                    raise EmbeddingProtocolError("embedding_vector_non_finite") from None
                if not math.isfinite(numeric):
                    raise EmbeddingProtocolError("embedding_vector_non_finite")
                vector.append(numeric)
            ordered[index] = vector

        if any(vector is None for vector in ordered):
            raise EmbeddingProtocolError("embedding_index_missing")
        return [vector for vector in ordered if vector is not None]


async def _close_non_success_response(response: httpx2.Response) -> None:
    """对非 2xx 响应提前关闭流，阻止 SDK 读取或解压错误体，同时保留状态码与响应头。"""
    if response.is_success:
        return
    try:
        await response.aclose()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("embedding_response_close_failed status_code=%d", response.status_code)


def _caused_by_decoding_error(error: BaseException) -> bool:
    """沿异常链检查底层是否由 httpx2.DecodingError 引发，并防御循环引用。"""
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        if isinstance(current, httpx2.DecodingError):
            return True
        visited.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _caused_by_transient_transport_error(error: BaseException) -> bool:
    """沿异常链检查是否为可重试的瞬态网络/协议中断（NetworkError / RemoteProtocolError）。"""
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        if isinstance(current, (httpx2.NetworkError, httpx2.RemoteProtocolError)):
            return True
        visited.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _map_non_retryable_status(status_code: int) -> EmbeddingUpstreamError:
    """将不可重试 HTTP 状态码映射为稳定安全错误码。"""
    if status_code in {401, 403}:
        return EmbeddingUpstreamError(
            "embedding_auth_failed",
            retryable=False,
            status_code=status_code,
        )
    if status_code in {400, 422}:
        return EmbeddingUpstreamError(
            "embedding_request_invalid",
            retryable=False,
            status_code=status_code,
        )
    if status_code == 404:
        return EmbeddingUpstreamError(
            "embedding_endpoint_not_found",
            retryable=False,
            status_code=status_code,
        )
    if 400 <= status_code < 500:
        return EmbeddingUpstreamError(
            "embedding_client_error",
            retryable=False,
            status_code=status_code,
        )
    return EmbeddingUpstreamError(
        "embedding_upstream_error",
        retryable=False,
        status_code=status_code,
    )


def _validate_base_url(base_url: str) -> None:
    """复用共享 HTTP Base URL 校验并将非法输入映射为 Embedding 配置异常。"""
    try:
        validate_http_base_url(base_url, allow_http=True, allow_query=False)
    except ValueError:
        raise EmbeddingConfigurationError("embedding_base_url_invalid") from None


def _parse_positive_int(
    source: Mapping[str, str],
    name: str,
    *,
    default: int,
    maximum: int | None = None,
    code: str,
) -> int:
    raw = source.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise EmbeddingConfigurationError(code) from None
    if value < 1 or (maximum is not None and value > maximum):
        raise EmbeddingConfigurationError(code)
    return value


def _parse_bounded_float(
    source: Mapping[str, str],
    name: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
    code: str,
) -> float:
    raw = source.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        raise EmbeddingConfigurationError(code) from None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise EmbeddingConfigurationError(code)
    return value


def _parse_strict_flag(
    source: Mapping[str, str],
    name: str,
    *,
    default: bool,
    code: str,
) -> bool:
    raw = source.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise EmbeddingConfigurationError(code)


__all__ = [
    "DEFAULT_EMBEDDING_BATCH_SIZE",
    "DEFAULT_EMBEDDING_DIMENSIONS",
    "DEFAULT_EMBEDDING_MAX_CONCURRENCY",
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_EMBEDDING_TIMEOUT_SECONDS",
    "MAX_EMBEDDING_BATCH_SIZE",
    "MAX_EMBEDDING_CONCURRENCY",
    "MAX_EMBEDDING_RESPONSE_BYTES",
    "MAX_EMBEDDING_RETRIES",
    "MAX_EMBEDDING_TIMEOUT_SECONDS",
    "MIN_EMBEDDING_TIMEOUT_SECONDS",
    "EmbeddingConfigurationError",
    "EmbeddingError",
    "EmbeddingProtocolError",
    "EmbeddingUpstreamError",
    "OpenAICompatibleEmbeddingClient",
    "OpenAICompatibleEmbeddingConfig",
]
