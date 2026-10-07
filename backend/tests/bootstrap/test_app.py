"""迁移期应用工厂测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from bootstrap import create_app


def test_migration_app_starts_without_external_services() -> None:
    """应用工厂不应在阶段 0 连接数据库或启动后台任务。"""
    client = TestClient(create_app())
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "architecture": "restructured"}
