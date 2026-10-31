"""以 TLS 和本地 SQLite 启动独立参考 Worker。"""

from __future__ import annotations

import uvicorn

from interfaces.http.worker_protocol import WorkerProtocolHttp

from .config import ReferenceWorkerConfig
from .runtime import ReferenceWorker


def main() -> None:
    """从环境加载 TLS 配置并启动参考 Worker HTTPS 服务。"""
    config = ReferenceWorkerConfig.from_environment()
    worker = ReferenceWorker(config)
    protocol = WorkerProtocolHttp(
        {"athena-orchestrator": config.orchestrator_token},
        max_json_body_bytes=config.max_json_body_bytes,
    )
    app = worker.create_app(protocol)
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        ssl_certfile=str(config.tls_certificate_path),
        ssl_keyfile=str(config.tls_private_key_path),
        access_log=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
