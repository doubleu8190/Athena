"""验证应用组装后的 HTTP 存活检查。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from main import create_app


def test_health() -> None:
    """通过真实路由检查响应状态与内容。

    参数：无。
    返回：None。
    异常：AssertionError，响应不符合约定时抛出。
    """
    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_os_error_outside_database_routes_is_not_masked_as_503() -> None:
    """未来外部 I/O 路由的 OSError 不能被数据库异常处理器全局吞掉。"""
    app: FastAPI = create_app()

    async def fail_external_io() -> None:
        raise ConnectionRefusedError("external service unavailable")

    app.add_api_route("/health/external-io", fail_external_io, methods=["GET"])
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/health/external-io")

    assert response.status_code == 500
    assert response.status_code != 503
