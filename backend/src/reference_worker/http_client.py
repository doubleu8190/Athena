"""Worker 调用 Athena HTTPS 服务时的证书验证客户端。"""

from __future__ import annotations

import httpx

from .config import ReferenceWorkerConfig


def create_center_client(config: ReferenceWorkerConfig) -> httpx.AsyncClient:
    """创建禁用代理、重定向且严格验证 CA 的 Athena 客户端。

    参数：
    - `config`（ReferenceWorkerConfig）：Athena TLS 根证书与服务凭据配置。

    返回：带 Worker 服务认证和协议版本头的异步 HTTP client。

    异常：CA 文件不合法时由 httpx/SSL 库抛出配置错误。
    """
    return httpx.AsyncClient(
        verify=str(config.ca_certificate_path),
        timeout=10.0,
        follow_redirects=False,
        trust_env=False,
        headers={
            "Authorization": f"Bearer {config.service_token}",
            "X-Athena-Protocol-Version": "1.0",
        },
    )


__all__ = ["create_center_client"]
