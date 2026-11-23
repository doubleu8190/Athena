"""OpenAI 兼容远程 Embedding 服务客户端，实现知识库向量化无状态化。"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx2

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"
DEFAULT_EMBEDDING_DIMENSIONS = 1024
DEFAULT_EMBEDDING_TIMEOUT_SECONDS = 30.0
DEFAULT_EMBEDDING_BATCH_SIZE = 32
MIN_EMBEDDING_TIMEOUT_SECONDS = 0.1
MAX_EMBEDDING_TIMEOUT_SECONDS = 300.0
MAX_EMBEDDING_BATCH_SIZE = 256
MAX_EMBEDDING_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_EMBEDDING_RETRIES = 2

_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})
_BASE_BACKOFF_SECONDS = 0.25
_MAX_BACKOFF_SECONDS = 5.0
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


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
                or _has_control_characters(self.api_key)
            ):
                raise EmbeddingConfigurationError("embedding_api_key_invalid")
        if (
            not isinstance(self.model, str)
            or not self.model.strip()
            or len(self.model) > 256
            or _has_control_characters(self.model)
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
        if _has_control_characters(raw_base_url):
            raise EmbeddingConfigurationError("embedding_base_url_invalid")
        base_url = raw_base_url.strip()

        raw_api_key = source.get("ATHENA_EMBEDDING_API_KEY")
        api_key: str | None
        if raw_api_key is None or not raw_api_key.strip():
            api_key = None
        else:
            if _has_control_characters(raw_api_key):
                raise EmbeddingConfigurationError("embedding_api_key_invalid")
            api_key = raw_api_key.strip()

        raw_model = source.get("ATHENA_EMBEDDING_MODEL")
        model = (
            DEFAULT_EMBEDDING_MODEL
            if raw_model is None or not raw_model.strip()
            else raw_model.strip()
        )
        if raw_model is not None and _has_control_characters(raw_model):
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
    """基于有界 HTTP 连接池的 OpenAI 兼容远程 Embedding 客户端。

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
    ) -> None:
        if max_retries < 0 or max_retries > 5:
            raise ValueError("embedding_max_retries_invalid")
        if max_response_bytes < 1:
            raise ValueError("embedding_max_response_bytes_invalid")
        self._config = config
        self._transport = transport
        self._sleep = sleep
        self._max_retries = max_retries
        self._max_response_bytes = max_response_bytes
        self._client: httpx2.AsyncClient | None = None
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
        """初始化可复用的有界异步 HTTP 客户端连接池。"""
        async with self._lock:
            if self._closed:
                raise EmbeddingUpstreamError("embedding_client_closed")
            if self._client is not None:
                return
            self._client = self._build_client()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """将文本列表按 batch_size 自动分批向量化，保序拼接并逐条校验维度与有限性。"""
        if self._closed:
            raise EmbeddingUpstreamError("embedding_client_closed")
        if not texts:
            return []
        client = await self._ensure_client()
        results: list[list[float]] = []
        batch_size = self._config.batch_size
        # 按配置批次切片并依次请求，避免单次大请求触发上游限制，同时保证返回顺序与输入一致。
        for start_index in range(0, len(texts), batch_size):
            if self._closed:
                raise EmbeddingUpstreamError("embedding_client_closed")
            batch = texts[start_index : start_index + batch_size]
            batch_vectors = await self._embed_batch(client, batch)
            results.extend(batch_vectors)
        return results

    async def aclose(self) -> None:
        """标记关闭并释放底层 HTTP 连接池，可安全重复调用。"""
        async with self._lock:
            self._closed = True
            client = self._client
            self._client = None
        if client is not None:
            await client.aclose()

    def _build_client(self) -> httpx2.AsyncClient:
        """构造有界超时、有界连接池且禁用重定向的 HTTP 客户端，防止凭据重定向泄露。"""
        timeout = httpx2.Timeout(
            timeout=self._config.timeout_seconds,
            connect=min(10.0, self._config.timeout_seconds),
        )
        limits = httpx2.Limits(
            max_connections=32,
            max_keepalive_connections=16,
            keepalive_expiry=30.0,
        )
        kwargs: dict[str, Any] = {
            "timeout": timeout,
            "limits": limits,
            "follow_redirects": False,
            "headers": {
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx2.AsyncClient(**kwargs)

    async def _ensure_client(self) -> httpx2.AsyncClient:
        """在锁内原子获取或延迟启动连接池，避免与并发 aclose() 竞态触发 AssertionError。"""
        async with self._lock:
            if self._closed:
                raise EmbeddingUpstreamError("embedding_client_closed")
            if self._client is None:
                self._client = self._build_client()
            return self._client

    async def _embed_batch(
        self,
        client: httpx2.AsyncClient,
        batch: list[str],
    ) -> list[list[float]]:
        """执行单批次远程 Embedding 请求，仅对 429/502/503/504 与传输超时执行有界退避重试。"""
        payload: dict[str, Any] = {
            "model": self._config.model,
            "input": batch,
        }
        if self._config.send_dimensions:
            payload["dimensions"] = self._config.dimensions

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._config.api_key is not None:
            headers["Authorization"] = f"Bearer {self._config.api_key}"

        endpoint = self._config.embeddings_url
        for attempt in range(self._max_retries + 1):
            retry_delay: float | None = None
            try:
                async with client.stream(
                    "POST",
                    endpoint,
                    json=payload,
                    headers=headers,
                ) as response:
                    status_code = response.status_code
                    if 200 <= status_code < 300:
                        raw_body = await self._read_bounded_body(response)
                        return self._parse_response(raw_body, expected_count=len(batch))

                    if status_code in _RETRYABLE_STATUS_CODES:
                        if attempt < self._max_retries:
                            retry_delay = self._retry_delay(
                                attempt,
                                response.headers.get("retry-after"),
                            )
                            logger.warning(
                                "embedding_upstream_retry status_code=%d attempt=%d "
                                "delay=%.2f batch_size=%d",
                                status_code,
                                attempt + 1,
                                retry_delay,
                                len(batch),
                            )
                        else:
                            code = (
                                "embedding_rate_limited"
                                if status_code == 429
                                else "embedding_upstream_unavailable"
                            )
                            logger.warning(
                                "embedding_upstream_failed code=%s status_code=%d batch_size=%d",
                                code,
                                status_code,
                                len(batch),
                            )
                            raise EmbeddingUpstreamError(
                                code,
                                retryable=True,
                                status_code=status_code,
                            )
                    else:
                        # 4xx 参数/鉴权错误及非瞬态 5xx 禁止重试，直接映射安全错误码。
                        error = _map_non_retryable_status(status_code)
                        logger.warning(
                            "embedding_upstream_failed code=%s status_code=%d batch_size=%d",
                            error.code,
                            status_code,
                            len(batch),
                        )
                        raise error
            except asyncio.CancelledError:
                raise
            except EmbeddingError:
                raise
            except httpx2.TimeoutException:
                if attempt < self._max_retries:
                    retry_delay = self._retry_delay(attempt, None)
                    logger.warning(
                        "embedding_timeout_retry attempt=%d delay=%.2f batch_size=%d",
                        attempt + 1,
                        retry_delay,
                        len(batch),
                    )
                else:
                    logger.warning(
                        "embedding_upstream_failed code=embedding_timeout batch_size=%d",
                        len(batch),
                    )
                    # 严禁透传底层 httpx2 异常上下文，避免请求头中的 API Key 泄露到日志或堆栈。
                    raise EmbeddingUpstreamError("embedding_timeout", retryable=True) from None
            except httpx2.TransportError as error:
                logger.warning(
                    "embedding_upstream_failed code=embedding_transport_error "
                    "exception_type=%s batch_size=%d",
                    type(error).__name__,
                    len(batch),
                )
                raise EmbeddingUpstreamError("embedding_transport_error", retryable=False) from None
            except httpx2.DecodingError as error:
                # 响应解码失败（如损坏压缩体）必须截断 httpx2 异常链，
                # 避免携带 Authorization 头逃逸。
                logger.warning(
                    "embedding_protocol_failed code=embedding_response_decode_failed "
                    "exception_type=%s batch_size=%d",
                    type(error).__name__,
                    len(batch),
                )
                raise EmbeddingProtocolError("embedding_response_decode_failed") from None
            except (httpx2.HTTPError, httpx2.StreamError) as error:
                # 兜底映射其余 httpx2 异常，确保任何客户端/流异常都不回显上游凭据。
                logger.warning(
                    "embedding_upstream_failed code=embedding_transport_error "
                    "exception_type=%s batch_size=%d",
                    type(error).__name__,
                    len(batch),
                )
                raise EmbeddingUpstreamError("embedding_transport_error", retryable=False) from None

            if retry_delay is not None:
                # 已退出 async with 释放响应流并归还连接池，再执行退避等待，
                # 避免并发重试期间长期占用连接池槽位。
                await self._sleep(retry_delay)

        raise EmbeddingUpstreamError("embedding_upstream_unavailable", retryable=True)

    async def _read_bounded_body(self, response: httpx2.Response) -> bytes:
        """在读取阶段限制响应总字节数，防止异常上游返回超大响应耗尽内存。"""
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length.strip())
            except ValueError:
                raise EmbeddingProtocolError("embedding_response_length_invalid") from None
            if declared < 0 or declared > self._max_response_bytes:
                raise EmbeddingProtocolError("embedding_response_too_large")

        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > self._max_response_bytes:
                raise EmbeddingProtocolError("embedding_response_too_large")
            chunks.append(chunk)
        return b"".join(chunks)

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
                numeric = float(value)
                if not math.isfinite(numeric):
                    raise EmbeddingProtocolError("embedding_vector_non_finite")
                vector.append(numeric)
            ordered[index] = vector

        if any(vector is None for vector in ordered):
            raise EmbeddingProtocolError("embedding_index_missing")
        return [vector for vector in ordered if vector is not None]

    @staticmethod
    def _retry_delay(attempt: int, retry_after_header: str | None) -> float:
        """计算有界指数退避等待时长；429 携带合法 Retry-After 时取安全上限内的值。"""
        delay = float(min(_MAX_BACKOFF_SECONDS, _BASE_BACKOFF_SECONDS * (2**attempt)))
        if retry_after_header is not None:
            try:
                parsed = float(retry_after_header.strip())
            except ValueError:
                parsed = math.nan
            if math.isfinite(parsed) and parsed >= 0.0:
                delay = min(_MAX_BACKOFF_SECONDS, max(delay, parsed))
        return delay


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


def _has_control_characters(value: str) -> bool:
    """检查字符串是否包含 ASCII 控制字符（防止 Header 注入或日志污染）。"""
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _validate_base_url(base_url: str) -> None:
    """校验远程 Embedding 服务 Base URL：仅允许 http/https，禁止控制字符、空白与凭据片段。"""
    if (
        not base_url
        or _has_control_characters(base_url)
        or any(ch.isspace() for ch in base_url)
        or "\\" in base_url
    ):
        raise EmbeddingConfigurationError("embedding_base_url_invalid")
    try:
        parsed = urlsplit(base_url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise EmbeddingConfigurationError("embedding_base_url_invalid") from None

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise EmbeddingConfigurationError("embedding_base_url_invalid")

    trailing = parsed.netloc.rsplit("]", 1)[-1] if parsed.netloc.startswith("[") else parsed.netloc
    if (":" in trailing and port is None) or (port is not None and not 1 <= port <= 65535):
        raise EmbeddingConfigurationError("embedding_base_url_invalid")

    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        if host.replace(".", "").isdigit() and "." in host:
            raise EmbeddingConfigurationError("embedding_base_url_invalid") from None

    if len(host) > 253 or any(_HOST_LABEL.fullmatch(label) is None for label in host.split(".")):
        raise EmbeddingConfigurationError("embedding_base_url_invalid")


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
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_EMBEDDING_TIMEOUT_SECONDS",
    "MAX_EMBEDDING_BATCH_SIZE",
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
