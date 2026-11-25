"""共享异步 HTTP 基础设施：字节限流、URL 校验、有界客户端工厂与退避重试。"""

from infrastructure.http.bounded import (
    BoundedResponseStream,
    BoundedResponseTransport,
    ResponseLimitError,
    caused_by_response_limit,
    find_response_limit_error,
)
from infrastructure.http.client import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    DEFAULT_KEEPALIVE_EXPIRY_SECONDS,
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_MAX_KEEPALIVE_CONNECTIONS,
    build_bounded_async_client,
    build_http_timeout,
)
from infrastructure.http.retry import (
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_BACKOFF_MAX_SECONDS,
    DEFAULT_RETRY_AFTER_MAX_SECONDS,
    RetryableHttpError,
    compute_retry_delay,
    parse_retry_after_seconds,
    run_with_http_retry,
)
from infrastructure.http.url import (
    has_control_characters,
    validate_http_base_url,
)

__all__ = [
    "DEFAULT_BACKOFF_BASE_SECONDS",
    "DEFAULT_BACKOFF_MAX_SECONDS",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
    "DEFAULT_KEEPALIVE_EXPIRY_SECONDS",
    "DEFAULT_MAX_CONNECTIONS",
    "DEFAULT_MAX_KEEPALIVE_CONNECTIONS",
    "DEFAULT_RETRY_AFTER_MAX_SECONDS",
    "BoundedResponseStream",
    "BoundedResponseTransport",
    "ResponseLimitError",
    "RetryableHttpError",
    "build_bounded_async_client",
    "build_http_timeout",
    "caused_by_response_limit",
    "compute_retry_delay",
    "find_response_limit_error",
    "has_control_characters",
    "parse_retry_after_seconds",
    "run_with_http_retry",
    "validate_http_base_url",
]
