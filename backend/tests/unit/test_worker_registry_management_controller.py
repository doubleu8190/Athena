"""验证注册中心管理路由使用浏览器登录会话保护。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.auth import AuthenticationConfig, AuthenticationService
from interfaces.http.auth import AuthenticationMiddleware
from interfaces.http.auth.options import AuthOptions
from interfaces.http.auth.routes import build_auth_router
from interfaces.http.controllers.worker_registry_management_controller import (
    build_worker_registry_management_router,
)


class EmptyRegistryService:
    """为 HTTP 边界测试提供不访问数据库的空注册结果。"""

    async def list_registry_instances(self) -> list[object]:
        """返回空快照，供测试确认成功登录后的路由响应。"""
        return []


def test_registry_management_route_requires_browser_session() -> None:
    """管理路由拒绝未登录请求，并允许有效 Cookie 会话查询。"""
    options = AuthOptions(
        enabled=True,
        username="registry-user",
        password="registry-secret",
        session_secret="r" * 32,
    )
    auth_service = AuthenticationService(
        AuthenticationConfig(
            enabled=True,
            username=options.username,
            password=options.password,
            session_secret=options.session_secret,
            session_ttl_seconds=options.session_ttl_seconds,
        )
    )
    app = FastAPI()
    app.include_router(build_auth_router(options=options, auth_service=auth_service), prefix="/api")
    app.include_router(
        build_worker_registry_management_router(EmptyRegistryService()),
        prefix="/api",
    )
    app.add_middleware(
        AuthenticationMiddleware,
        options=options,
        auth_service=auth_service,
    )

    client = TestClient(app)
    assert client.get("/api/worker-registry/instances").status_code == 401
    assert client.post(
        "/api/auth/login",
        json={"username": options.username, "password": options.password},
    ).status_code == 200
    response = client.get("/api/worker-registry/instances")
    assert response.status_code == 200
    assert response.json() == []
