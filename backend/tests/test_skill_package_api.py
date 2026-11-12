"""管理员 Skill 包成员错误的轻量 HTTP 映射测试。"""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.security import require_admin
from api.skill import build_skill_admin_router
from app.skill.service import SkillAuthorizationError
from domain.skill import SkillPackageError


class _PackageService:
    """让两个上传入口在不访问数据库时抛出同一可定位错误。"""

    def __init__(self, error: Exception) -> None:
        """保存待抛出的领域或通用异常。"""
        self.error = error

    async def import_package(self, *args: object, **kwargs: object) -> object:
        """模拟首发导入失败。"""
        del args, kwargs
        raise self.error

    async def add_version(self, *args: object, **kwargs: object) -> object:
        """模拟版本新增失败。"""
        del args, kwargs
        raise self.error


def _client(error: Exception) -> TestClient:
    """创建只挂载管理员 Skill 路由的测试应用。"""
    app = FastAPI()
    app.dependency_overrides[require_admin] = lambda: object()
    app.include_router(build_skill_admin_router(cast(Any, _PackageService(error))))
    return TestClient(app)


@pytest.mark.parametrize(
    ("path", "headers", "params"),
    (
        ("/api/v1/admin/skills/import", {"Idempotency-Key": "import-key"}, {}),
        (
            "/api/v1/admin/skills/skill_one/versions",
            {"Idempotency-Key": "version-key"},
            {"label": "v2"},
        ),
    ),
)
def test_admin_package_routes_return_member_path_detail(
    path: str,
    headers: dict[str, str],
    params: dict[str, str],
) -> None:
    """import 与 versions 均返回 code 和安全 member_path 对象。"""
    error = SkillPackageError(
        "skill_zip_path_invalid",
        member_path="scripts/\\x01bad.py",
    )

    response = _client(error).post(
        path,
        headers=headers,
        params=params,
        files={"package": ("skill.zip", b"zip", "application/zip")},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "skill_zip_path_invalid",
            "member_path": "scripts/\\x01bad.py",
        }
    }


def test_admin_package_route_keeps_pathless_detail_string() -> None:
    """无成员路径的通用值错误不回显原异常，仍返回稳定字符串 detail。"""
    response = _client(ValueError("postgresql://secret/internal")).post(
        "/api/v1/admin/skills/import",
        headers={"Idempotency-Key": "import-key"},
        files={"package": ("skill.zip", b"zip", "application/zip")},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "skill_package_invalid"}


@pytest.mark.parametrize(
    ("path", "headers", "params"),
    (
        ("/api/v1/admin/skills/import", {"Idempotency-Key": "import-key"}, {}),
        (
            "/api/v1/admin/skills/skill_one/versions",
            {"Idempotency-Key": "version-key"},
            {"label": "v2"},
        ),
    ),
)
def test_admin_package_authorization_failure_returns_403(
    path: str,
    headers: dict[str, str],
    params: dict[str, str],
) -> None:
    """import 与 versions 的事务授权异常都稳定映射为 403。"""
    response = _client(SkillAuthorizationError("admin_required")).post(
        path,
        headers=headers,
        params=params,
        files={"package": ("skill.zip", b"zip", "application/zip")},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "admin_required"}


def test_configure_transaction_authorization_failure_returns_403() -> None:
    """服务层事务授权异常必须稳定映射为 403，而不是未处理 500。"""

    class _ConfigureService:
        async def configure(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            raise SkillAuthorizationError("admin_required")

    app = FastAPI()
    app.dependency_overrides[require_admin] = lambda: object()
    app.include_router(
        build_skill_admin_router(cast(Any, _ConfigureService()))
    )

    with TestClient(app) as client:
        response = client.patch(
            "/api/v1/admin/skills/skill_one",
            json={
                "expected_revision": 1,
                "enabled": False,
                "current_version_id": "skillver_one",
                "script_requires_approval": False,
            },
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "admin_required"}
