"""普通用户个人 Skill HTTP 契约测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from adapter.file.storage import FileStorageError
from api.security import get_current_auth
from api.skill import build_skill_personal_router, build_skill_user_router
from app.skill.model import SkillAdminDetails
from app.skill.personal import PersonalSkillDraft, PersonalSkillError
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.skill import (
    DependencyCheck,
    DependencyCheckStatus,
    PackageResource,
    Skill,
    SkillPackageManifest,
    SkillScope,
    SkillVersion,
)
from main import _handle_os_error

NOW = datetime(2026, 11, 19, tzinfo=UTC)


class _PersonalService:
    """返回固定个人 Skill 数据并记录关键写参数。"""

    def __init__(self) -> None:
        check = DependencyCheck(
            status=DependencyCheckStatus.SATISFIED,
            python_version="not-checked",
            python_implementation="markdown-only",
            items=(),
            checked_at=NOW,
        )
        manifest = SkillPackageManifest(
            slug="personal-demo",
            name="周报",
            description="整理周报",
            guide="只使用事实。",
            files=(
                PackageResource(
                    path="SKILL.md",
                    size_bytes=18,
                    sha256="a" * 64,
                    text=True,
                ),
            ),
        )
        self.version = SkillVersion(
            id="skillver_demo",
            skill_id="skill_demo",
            ordinal=1,
            label="v1",
            manifest=manifest,
            object_key="skills/aa/demo.zip",
            package_sha256="b" * 64,
            dependency_requirements=(),
            dependency_check=check,
            created_by="usr_demo",
            created_at=NOW,
        )
        self.skill = Skill(
            id="skill_demo",
            slug=manifest.slug,
            current_version_id=self.version.id,
            enabled=True,
            script_requires_approval=False,
            revision=1,
            created_by="usr_demo",
            created_at=NOW,
            updated_at=NOW,
            name=manifest.name,
            description=manifest.description,
            current_version_label="v1",
            dependency_check=check,
            scope=SkillScope.PERSONAL,
            owner_id="usr_demo",
        )
        self.deleted: list[tuple[str, int]] = []

    async def user_directory(self, actor: AuthContext) -> tuple[Skill, ...]:
        del actor
        return (self.skill,)

    async def list_personal(self, actor: AuthContext) -> tuple[Skill, ...]:
        del actor
        return (self.skill,)

    async def parse_personal_document(
        self,
        filename: str,
        path: Path,
    ) -> PersonalSkillDraft:
        if not filename.casefold().endswith(".md"):
            raise PersonalSkillError("skill_markdown_filename_invalid", field="file")
        if b"scripts:" in path.read_bytes():
            raise PersonalSkillError(
                "skill_markdown_field_unsupported",
                field="scripts",
            )
        return PersonalSkillDraft(
            name="导入名称",
            description="导入描述",
            guide="导入正文",
        )

    async def create_personal(
        self,
        actor: AuthContext,
        **kwargs: object,
    ) -> SkillAdminDetails:
        del actor, kwargs
        return SkillAdminDetails(self.skill, (self.version,))

    async def get_personal(
        self,
        actor: AuthContext,
        skill_id: str,
    ) -> SkillAdminDetails:
        del actor, skill_id
        return SkillAdminDetails(self.skill, (self.version,))

    async def publish_personal(
        self,
        actor: AuthContext,
        skill_id: str,
        **kwargs: object,
    ) -> SkillAdminDetails:
        del actor, skill_id, kwargs
        return SkillAdminDetails(self.skill, (self.version,))

    async def set_personal_enabled(
        self,
        actor: AuthContext,
        skill_id: str,
        **kwargs: object,
    ) -> SkillAdminDetails:
        del actor, skill_id, kwargs
        return SkillAdminDetails(self.skill, (self.version,))

    async def delete_personal(
        self,
        actor: AuthContext,
        skill_id: str,
        *,
        expected_revision: int,
    ) -> None:
        del actor
        self.deleted.append((skill_id, expected_revision))


class _FailingPersonalService(_PersonalService):
    """让指定个人路由统一抛出数据库或对象存储故障。"""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def user_directory(self, actor: AuthContext) -> tuple[Skill, ...]:
        del actor
        raise self.error

    async def list_personal(self, actor: AuthContext) -> tuple[Skill, ...]:
        del actor
        raise self.error

    async def create_personal(
        self,
        actor: AuthContext,
        **kwargs: object,
    ) -> SkillAdminDetails:
        del actor, kwargs
        raise self.error

    async def get_personal(
        self,
        actor: AuthContext,
        skill_id: str,
    ) -> SkillAdminDetails:
        del actor, skill_id
        raise self.error

    async def publish_personal(
        self,
        actor: AuthContext,
        skill_id: str,
        **kwargs: object,
    ) -> SkillAdminDetails:
        del actor, skill_id, kwargs
        raise self.error

    async def set_personal_enabled(
        self,
        actor: AuthContext,
        skill_id: str,
        **kwargs: object,
    ) -> SkillAdminDetails:
        del actor, skill_id, kwargs
        raise self.error

    async def delete_personal(
        self,
        actor: AuthContext,
        skill_id: str,
        *,
        expected_revision: int,
    ) -> None:
        del actor, skill_id, expected_revision
        raise self.error


def _client(
    service: _PersonalService | None = None,
) -> tuple[TestClient, _PersonalService]:
    """挂载目录与个人路由，并覆盖当前登录用户。"""
    user = User(
        id="usr_demo",
        email="demo@example.test",
        password_hash="unused",
        display_name=None,
        status=UserStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        role=UserRole.USER,
    )
    actor = AuthContext(user=user, session_id="auth_demo")
    service = service or _PersonalService()
    app = FastAPI()
    app.dependency_overrides[get_current_auth] = lambda: actor
    app.add_exception_handler(OSError, _handle_os_error)
    app.include_router(build_skill_user_router(cast(Any, service)))
    app.include_router(build_skill_personal_router(cast(Any, service)))
    return TestClient(app), service


def test_personal_skill_http_contract_and_scope() -> None:
    """目录、详情、解析、发布、启停和删除遵循已锁定响应结构。"""
    client, service = _client()

    directory = client.get("/api/v1/skills")
    assert directory.status_code == 200
    assert directory.json()["items"][0]["scope"] == "personal"

    personal = client.get("/api/v1/me/skills")
    assert personal.status_code == 200
    assert personal.json()["items"][0]["id"] == "skill_demo"

    parsed = client.post(
        "/api/v1/me/skills/parse",
        files={"file": ("notes.MD", "正文".encode(), "text/markdown")},
    )
    assert parsed.status_code == 200
    assert parsed.json() == {
        "name": "导入名称",
        "description": "导入描述",
        "guide": "导入正文",
    }

    created = client.post(
        "/api/v1/me/skills",
        headers={"Idempotency-Key": "create-demo"},
        json={"name": "周报", "description": "整理周报", "guide": "只使用事实。"},
    )
    assert created.status_code == 201
    assert created.json()["scope"] == "personal"
    assert created.json()["versions"][0]["id"] == "skillver_demo"

    published = client.post(
        "/api/v1/me/skills/skill_demo/versions",
        headers={"Idempotency-Key": "publish-demo"},
        json={
            "name": "周报",
            "description": "整理周报",
            "guide": "只使用事实。",
            "expected_revision": 1,
        },
    )
    assert published.status_code == 201

    toggled = client.patch(
        "/api/v1/me/skills/skill_demo",
        json={"enabled": False, "expected_revision": 1},
    )
    assert toggled.status_code == 200

    deleted = client.delete(
        "/api/v1/me/skills/skill_demo",
        params={"expected_revision": 1},
    )
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert service.deleted == [("skill_demo", 1)]


def test_personal_create_rejects_extra_execution_fields() -> None:
    """结构化创建请求不能夹带脚本或依赖字段。"""
    client, _ = _client()

    response = client.post(
        "/api/v1/me/skills",
        headers={"Idempotency-Key": "create-extra"},
        json={
            "name": "周报",
            "description": "整理周报",
            "guide": "正文",
            "scripts": [],
        },
    )

    assert response.status_code == 422


def test_personal_write_reuses_origin_guard() -> None:
    """个人写接口与现有管理接口复用同源校验。"""
    client, _ = _client()

    response = client.post(
        "/api/v1/me/skills",
        headers={
            "Idempotency-Key": "create-origin",
            "Origin": "https://evil.example",
        },
        json={"name": "周报", "description": "整理周报", "guide": "正文"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "origin_not_allowed"}


def test_parse_endpoint_returns_stable_markdown_error_code() -> None:
    """上传错误返回稳定错误码和安全的表单字段定位。"""
    client, _ = _client()

    response = client.post(
        "/api/v1/me/skills/parse",
        files={"file": ("skill.py", b"print('x')", "text/plain")},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "skill_markdown_filename_invalid",
            "field": "file",
        }
    }


def test_parse_endpoint_rejects_multiple_files() -> None:
    """同一 multipart 字段出现多个文件时不能静默选择其中一个。"""
    client, _ = _client()

    response = client.post(
        "/api/v1/me/skills/parse",
        files=[
            ("file", ("one.md", b"one", "text/markdown")),
            ("file", ("two.md", b"two", "text/markdown")),
        ],
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "personal_skill_file_invalid"}


def test_parse_endpoint_reports_unsupported_frontmatter_field() -> None:
    """frontmatter 未知字段保留错误码，并指出具体不支持的字段。"""
    client, _ = _client()

    response = client.post(
        "/api/v1/me/skills/parse",
        files={
            "file": (
                "skill.md",
                b"---\nname: demo\nscripts: []\n---\nbody",
                "text/markdown",
            )
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "skill_markdown_field_unsupported",
            "field": "scripts",
        }
    }


@pytest.mark.parametrize(
    ("method", "path", "payload", "idempotency_key"),
    (
        ("GET", "/api/v1/skills", None, None),
        ("GET", "/api/v1/me/skills", None, None),
        ("GET", "/api/v1/me/skills/skill_demo", None, None),
        (
            "PATCH",
            "/api/v1/me/skills/skill_demo",
            {"enabled": False, "expected_revision": 1},
            None,
        ),
        ("DELETE", "/api/v1/me/skills/skill_demo?expected_revision=1", None, None),
        (
            "POST",
            "/api/v1/me/skills",
            {"name": "周报", "description": "描述", "guide": "正文"},
            "create-db",
        ),
        (
            "POST",
            "/api/v1/me/skills/skill_demo/versions",
            {
                "name": "周报",
                "description": "描述",
                "guide": "正文",
                "expected_revision": 1,
            },
            "publish-db",
        ),
    ),
)
def test_personal_database_transport_failures_return_database_unavailable(
    method: str,
    path: str,
    payload: dict[str, object] | None,
    idempotency_key: str | None,
) -> None:
    """个人目录和写接口的原始数据库 OSError 统一投影为 database_unavailable。"""
    service = _FailingPersonalService(
        ConnectionRefusedError("database password must not leak")
    )
    client, _ = _client(service)
    request_options: dict[str, Any] = {}
    if payload is not None:
        request_options["json"] = payload
    if idempotency_key is not None:
        request_options["headers"] = {"Idempotency-Key": idempotency_key}

    response = client.request(method, path, **request_options)

    assert response.status_code == 503
    assert response.json() == {"detail": "database_unavailable"}
    assert "password" not in response.text


@pytest.mark.parametrize(
    ("path", "payload", "idempotency_key"),
    (
        (
            "/api/v1/me/skills",
            {"name": "周报", "description": "描述", "guide": "正文"},
            "create-storage",
        ),
        (
            "/api/v1/me/skills/skill_demo/versions",
            {
                "name": "周报",
                "description": "描述",
                "guide": "正文",
                "expected_revision": 1,
            },
            "publish-storage",
        ),
    ),
)
def test_personal_storage_failures_keep_file_storage_projection(
    path: str,
    payload: dict[str, object],
    idempotency_key: str,
) -> None:
    """生产对象存储异常仍返回 file_storage_unavailable，不能误报数据库故障。"""
    service = _FailingPersonalService(FileStorageError())
    client, _ = _client(service)

    response = client.post(
        path,
        headers={"Idempotency-Key": idempotency_key},
        json=payload,
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "file_storage_unavailable"}
