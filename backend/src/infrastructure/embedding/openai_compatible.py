"""OpenAI 兼容远程 Embedding 服务客户端（基于官方 openai.AsyncOpenAI 薄封装）。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx2
import openai
from openai import AsyncOpenAI

from infrastructure.http import has_control_characters, validate_http_base_url

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
MAX_EMBEDDING_RETRIES = 2


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


def _is_valid_int(value: object, *, minimum: int, maximum: int | None = None) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= minimum
        and (maximum is None or value <= maximum)
    )


def _parse_env_number(
    source: Mapping[str, str],
    name: str,
    *,
    default: int | float,
    code: str,
    as_float: bool = False,
) -> int | float:
    raw = source.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip()) if as_float else int(raw.strip())
    except ValueError:
        raise EmbeddingConfigurationError(code) from None


def _parse_env_bool(source: Mapping[str, str], name: str, *, default: bool, code: str) -> bool:
    raw = source.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise EmbeddingConfigurationError(code)


@dataclass(frozen=True, slots=True)
class OpenAICompatibleEmbeddingConfig:
    """经过校验的 OpenAI 兼容远程 Embedding 服务配置。"""

    base_url: str
    api_key: str | None = field(default=None, repr=False)
    model: str = DEFAULT_EMBEDDING_MODEL
    dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS
    timeout_seconds: float = DEFAULT_EMBEDDING_TIMEOUT_SECONDS
    batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE
    send_dimensions: bool = True

    def __post_init__(self) -> None:
        try:
            validate_http_base_url(self.base_url, allow_http=True, allow_query=False)
        except (TypeError, ValueError):
            raise EmbeddingConfigurationError("embedding_base_url_invalid") from None
        if self.api_key is not None and (
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
        if not _is_valid_int(self.dimensions, minimum=1):
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
        if not _is_valid_int(self.batch_size, minimum=1, maximum=MAX_EMBEDDING_BATCH_SIZE):
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
        """从 ATHENA_EMBEDDING_* 环境变量加载并校验配置。"""
        source = os.environ if environ is None else environ
        raw_base_url = source.get("ATHENA_EMBEDDING_BASE_URL")
        if raw_base_url is None or not raw_base_url.strip():
            raise EmbeddingConfigurationError("embedding_base_url_missing")
        if has_control_characters(raw_base_url):
            raise EmbeddingConfigurationError("embedding_base_url_invalid")

        raw_api_key = source.get("ATHENA_EMBEDDING_API_KEY")
        api_key = None if raw_api_key is None or not raw_api_key.strip() else raw_api_key.strip()
        if raw_api_key is not None and api_key is not None and has_control_characters(raw_api_key):
            raise EmbeddingConfigurationError("embedding_api_key_invalid")

        raw_model = source.get("ATHENA_EMBEDDING_MODEL")
        if raw_model is not None and has_control_characters(raw_model):
            raise EmbeddingConfigurationError("embedding_model_invalid")
        model = (
            DEFAULT_EMBEDDING_MODEL
            if raw_model is None or not raw_model.strip()
            else raw_model.strip()
        )
        dimensions = int(
            _parse_env_number(
                source,
                "ATHENA_EMBEDDING_DIMENSIONS",
                default=DEFAULT_EMBEDDING_DIMENSIONS,
                code="embedding_dimensions_invalid",
            )
        )
        if expected_dimensions is not None and dimensions != expected_dimensions:
            raise EmbeddingConfigurationError("embedding_dimensions_mismatch")
        return cls(
            base_url=raw_base_url.strip(),
            api_key=api_key,
            model=model,
            dimensions=dimensions,
            timeout_seconds=float(
                _parse_env_number(
                    source,
                    "ATHENA_EMBEDDING_TIMEOUT_SECONDS",
                    default=DEFAULT_EMBEDDING_TIMEOUT_SECONDS,
                    code="embedding_timeout_invalid",
                    as_float=True,
                )
            ),
            batch_size=int(
                _parse_env_number(
                    source,
                    "ATHENA_EMBEDDING_BATCH_SIZE",
                    default=DEFAULT_EMBEDDING_BATCH_SIZE,
                    code="embedding_batch_size_invalid",
                )
            ),
            send_dimensions=_parse_env_bool(
                source,
                "ATHENA_EMBEDDING_SEND_DIMENSIONS",
                default=True,
                code="embedding_send_dimensions_invalid",
            ),
        )


class OpenAICompatibleEmbeddingClient:
    """基于官方 openai.AsyncOpenAI 的 OpenAI 兼容远程 Embedding 薄包装客户端。"""

    def __init__(
        self,
        config: OpenAICompatibleEmbeddingConfig,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        max_retries: int = MAX_EMBEDDING_RETRIES,
        max_concurrency: int = DEFAULT_EMBEDDING_MAX_CONCURRENCY,
    ) -> None:
        if not _is_valid_int(max_retries, minimum=0, maximum=5):
            raise ValueError("embedding_max_retries_invalid")
        if not _is_valid_int(max_concurrency, minimum=1, maximum=MAX_EMBEDDING_CONCURRENCY):
            raise ValueError("embedding_max_concurrency_invalid")
        self._config = config
        self._transport = transport
        self._max_retries = max_retries
        self._max_concurrency = max_concurrency
        self._sdk_client: AsyncOpenAI | None = None
        self._closed = False
        self._lock = asyncio.Lock()

    @property
    def config(self) -> OpenAICompatibleEmbeddingConfig:
        """暴露只读配置供组合根或 Worker 读取 batch_size 等参数（repr 已屏蔽 API Key）。"""
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
                environ, expected_dimensions=expected_dimensions
            )
        )

    async def start(self) -> None:
        """初始化可复用的 AsyncOpenAI 客户端与连接池。"""
        await self._ensure_client()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """按 batch_size 受控并发切片向量化，保序拼接并逐条校验维度与有限浮点约束。"""
        if self._closed:
            raise EmbeddingUpstreamError("embedding_client_closed")
        if not texts:
            return []
        client = await self._ensure_client()
        batch_size = self._config.batch_size
        batches = [texts[start : start + batch_size] for start in range(0, len(texts), batch_size)]
        # 单批次快路径直接执行，避免额外的信号量与 TaskGroup 调度开销。
        if len(batches) == 1:
            vectors = await self._embed_batch(client, batches[0])
            if self._closed:
                raise EmbeddingUpstreamError("embedding_client_closed")
            return vectors

        # 多批次受控并发：TaskGroup 在任一批失败时自动取消兄弟任务并等待回收。
        semaphore = asyncio.Semaphore(self._max_concurrency)
        ordered_batches: list[list[list[float]] | None] = [None] * len(batches)

        async def _run_batch(batch_index: int, batch: list[str]) -> None:
            async with semaphore:
                if self._closed:
                    raise EmbeddingUpstreamError("embedding_client_closed")
                vectors = await self._embed_batch(client, batch)
                if self._closed:
                    raise EmbeddingUpstreamError("embedding_client_closed")
                ordered_batches[batch_index] = vectors

        try:
            async with asyncio.TaskGroup() as group:
                for batch_index, batch in enumerate(batches):
                    group.create_task(_run_batch(batch_index, batch))
        except ExceptionGroup as exc_group:
            first: Exception = exc_group
            while isinstance(first, ExceptionGroup):
                first = first.exceptions[0]
            if self._closed and not isinstance(first, EmbeddingError):
                raise EmbeddingUpstreamError("embedding_client_closed") from None
            raise first from None

        if self._closed:
            raise EmbeddingUpstreamError("embedding_client_closed")
        results: list[list[float]] = []
        for batch_vectors in ordered_batches:
            if batch_vectors is None:
                raise asyncio.CancelledError()
            results.extend(batch_vectors)
        return results

    async def aclose(self) -> None:
        """标记关闭并释放底层 AsyncOpenAI 与 HTTP 连接池，可安全重复调用。"""
        async with self._lock:
            self._closed = True
            client = self._sdk_client
            self._sdk_client = None
        if client is not None:
            await client.close()

    async def _ensure_client(self) -> AsyncOpenAI:
        """在锁内原子懒创建 AsyncOpenAI，避免与并发 aclose() 竞态。"""
        async with self._lock:
            if self._closed:
                raise EmbeddingUpstreamError("embedding_client_closed")
            if self._sdk_client is None:
                timeout = httpx2.Timeout(
                    timeout=self._config.timeout_seconds,
                    connect=min(10.0, self._config.timeout_seconds),
                )
                http_client = httpx2.AsyncClient(
                    timeout=timeout,
                    limits=httpx2.Limits(
                        max_connections=32,
                        max_keepalive_connections=16,
                        keepalive_expiry=30.0,
                    ),
                    follow_redirects=False,
                    transport=self._transport,
                )
                # 显式传空 org/project 与 _enforce_credentials=False，绝不误读 OPENAI_* 环境变量。
                self._sdk_client = AsyncOpenAI(
                    api_key=self._config.api_key or "",
                    base_url=self._config.base_url.rstrip("/"),
                    organization="",
                    project="",
                    _enforce_credentials=False,
                    timeout=timeout,
                    max_retries=self._max_retries,
                    http_client=http_client,
                )
            return self._sdk_client

    async def _embed_batch(self, client: AsyncOpenAI, batch: list[str]) -> list[list[float]]:
        """委托 AsyncOpenAI 执行单批次 /embeddings 请求并将底层异常脱敏转译。"""
        if self._closed:
            raise EmbeddingUpstreamError("embedding_client_closed")
        # 屏蔽空 OpenAI-Organization/Project；无 api_key 时显式省略 Authorization。
        extra_headers: dict[str, str | openai.Omit] = {
            "OpenAI-Organization": openai.omit,
            "OpenAI-Project": openai.omit,
        }
        if self._config.api_key is None:
            extra_headers["Authorization"] = openai.omit

        try:
            raw_response = await client.embeddings.with_raw_response.create(
                model=self._config.model,
                input=batch,
                encoding_format="float",
                dimensions=self._config.dimensions if self._config.send_dimensions else openai.omit,
                extra_headers=extra_headers,
            )
        except asyncio.CancelledError:
            raise
        except EmbeddingError:
            raise
        except Exception as error:
            if self._closed:
                raise EmbeddingUpstreamError("embedding_client_closed") from None
            mapped = _translate_client_error(error, batch_size=len(batch))
            if mapped is not None:
                raise mapped from None
            raise

        if self._closed:
            raise EmbeddingUpstreamError("embedding_client_closed")
        return self._parse_response(raw_response.http_response.content, expected_count=len(batch))

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
            raw_index = item.get("index", position)
            if not _is_valid_int(raw_index, minimum=0, maximum=expected_count - 1):
                raise EmbeddingProtocolError("embedding_index_invalid")
            if ordered[raw_index] is not None:
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
            ordered[raw_index] = vector

        if any(vector is None for vector in ordered):
            raise EmbeddingProtocolError("embedding_index_missing")
        return [vector for vector in ordered if vector is not None]


def _translate_client_error(error: Exception, *, batch_size: int) -> EmbeddingError | None:
    """将 OpenAI SDK 与 httpx2 底层异常映射为脱敏稳定异常，绝不回显密钥或响应体。"""
    if isinstance(error, openai.APITimeoutError):
        logger.warning("embedding_upstream_failed code=embedding_timeout batch_size=%d", batch_size)
        return EmbeddingUpstreamError("embedding_timeout", retryable=True)
    if isinstance(error, openai.APIConnectionError):
        if isinstance(error.__cause__, httpx2.DecodingError):
            logger.warning(
                "embedding_protocol_failed code=embedding_response_decode_failed batch_size=%d",
                batch_size,
            )
            return EmbeddingProtocolError("embedding_response_decode_failed")
        logger.warning(
            "embedding_upstream_failed code=embedding_transport_error batch_size=%d", batch_size
        )
        return EmbeddingUpstreamError("embedding_transport_error", retryable=True)
    if isinstance(error, openai.APIStatusError):
        mapped = _map_status_error(error.status_code)
        logger.warning(
            "embedding_upstream_failed code=%s status_code=%d batch_size=%d",
            mapped.code,
            error.status_code,
            batch_size,
        )
        return mapped
    if isinstance(error, httpx2.DecodingError):
        logger.warning(
            "embedding_protocol_failed code=embedding_response_decode_failed batch_size=%d",
            batch_size,
        )
        return EmbeddingProtocolError("embedding_response_decode_failed")
    if isinstance(error, (httpx2.HTTPError, httpx2.StreamError)):
        logger.warning(
            "embedding_upstream_failed code=embedding_transport_error "
            "exception_type=%s batch_size=%d",
            type(error).__name__,
            batch_size,
        )
        return EmbeddingUpstreamError("embedding_transport_error", retryable=False)
    return None


def _map_status_error(status_code: int) -> EmbeddingUpstreamError:
    """将 HTTP 状态码映射为脱敏上游异常，与 SDK 标准重试集合 {408,409,429,>=500} 对齐。"""
    if status_code == 429:
        return EmbeddingUpstreamError("embedding_rate_limited", retryable=True, status_code=429)
    if status_code in {408, 409} or status_code >= 500:
        return EmbeddingUpstreamError(
            "embedding_upstream_unavailable", retryable=True, status_code=status_code
        )
    if status_code in {401, 403}:
        code = "embedding_auth_failed"
    elif status_code in {400, 422}:
        code = "embedding_request_invalid"
    elif status_code == 404:
        code = "embedding_endpoint_not_found"
    elif 400 <= status_code < 500:
        code = "embedding_client_error"
    else:
        code = "embedding_upstream_error"
    return EmbeddingUpstreamError(code, retryable=False, status_code=status_code)


__all__ = [
    "DEFAULT_EMBEDDING_BATCH_SIZE",
    "DEFAULT_EMBEDDING_DIMENSIONS",
    "DEFAULT_EMBEDDING_MAX_CONCURRENCY",
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_EMBEDDING_TIMEOUT_SECONDS",
    "MAX_EMBEDDING_BATCH_SIZE",
    "MAX_EMBEDDING_CONCURRENCY",
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
