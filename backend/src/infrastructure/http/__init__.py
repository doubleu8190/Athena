"""共享异步 HTTP 基础设施：URL 安全校验与基于 tenacity 的退避重试。"""

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
    "DEFAULT_RETRY_AFTER_MAX_SECONDS",
    "RetryableHttpError",
    "compute_retry_delay",
    "has_control_characters",
    "parse_retry_after_seconds",
    "run_with_http_retry",
    "validate_http_base_url",
]
