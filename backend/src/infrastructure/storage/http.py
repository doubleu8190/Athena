"""MinIO / S3 客户端共用的有界 urllib3 HTTP 连接池工厂。"""

from __future__ import annotations

import math
import os
import ssl
from typing import Any

DEFAULT_MINIO_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_MINIO_READ_TIMEOUT_SECONDS = 30.0
_ALLOWED_RETRY_METHODS = frozenset({"HEAD", "GET", "PUT", "DELETE"})
_RETRYABLE_STATUS_CODES = (500, 502, 503, 504)


def build_minio_http_pool(
    *,
    maxsize: int,
    block: bool,
    secure: bool,
    connect_timeout: float = DEFAULT_MINIO_CONNECT_TIMEOUT_SECONDS,
    read_timeout: float = DEFAULT_MINIO_READ_TIMEOUT_SECONDS,
) -> Any:
    """构造有界超时、有界重试（禁用 POST 与重定向）及按需严格 TLS 的 urllib3 连接池。"""
    if maxsize < 1:
        raise ValueError("minio_http_pool_maxsize_invalid")
    if not math.isfinite(connect_timeout) or connect_timeout <= 0:
        raise ValueError("minio_http_pool_connect_timeout_invalid")
    if not math.isfinite(read_timeout) or read_timeout <= 0:
        raise ValueError("minio_http_pool_read_timeout_invalid")

    # 延迟导入 urllib3，保留可选 SDK 缺失的 ImportError 语义并兼容单测 monkeypatch。
    import urllib3
    from urllib3.util import Retry, Timeout

    timeout = Timeout(connect=connect_timeout, read=read_timeout)
    retries = Retry(
        total=2,
        connect=2,
        read=2,
        status=2,
        redirect=0,
        other=0,
        backoff_factor=0.2,
        respect_retry_after_header=False,
        status_forcelist=_RETRYABLE_STATUS_CODES,
        allowed_methods=_ALLOWED_RETRY_METHODS,
    )
    kwargs: dict[str, Any] = {
        "maxsize": maxsize,
        "block": block,
        "timeout": timeout,
        "retries": retries,
    }
    if secure:
        # 启用 HTTPS 时强制校验证书与主机名，支持通过 SSL_CERT_FILE 指定私有 CA 根证书。
        context = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None)
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        kwargs["ssl_context"] = context
    return urllib3.PoolManager(**kwargs)


__all__ = [
    "DEFAULT_MINIO_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_MINIO_READ_TIMEOUT_SECONDS",
    "build_minio_http_pool",
]
