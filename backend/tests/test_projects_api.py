"""项目 HTTP API 与服务行为测试。"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Never

import pytest
from auth_support import AuthTestClient, make_auth_service
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.auth.service import AuthService
from app.project.service import ProjectService
from domain.project import Project, ProjectPage
from main import create_app

PROJECT_ID_PATTERN = re.compile(r"^\d{4}\.\d{2}\.\d{2}\.\d{2}\.\d{2}\.\d{2}\.\d{3}$")
UUID_PATTERNS = (
    re.compile(r"^[0-9a-f]{32}$"),
    re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$"),
)


class InMemoryProjectRepository:
    """为 HTTP 测试保存项目实体。"""

    def __init__(self, projects: tuple[Project, ...] = ()) -> None:
        self.items = {project.id: project for project in projects}

    async def create(self, project: Project) -> Project:
        self.items[project.id] = project
        return project

    async def get(self, owner_id: str, project_id: str) -> Project | None:
        project = self.items.get(project_id)
        if project is None or project.owner_id != owner_id or project.deleted_time is not None:
            return None
        return project

    async def list_active(
        self,
        owner_id: str,
        *,
        offset: int,
        limit: int,
    ) -> ProjectPage:
        active = sorted(
            (
                project
                for project in self.items.values()
                if project.owner_id == owner_id and project.deleted_time is None
            ),
            key=lambda project: (project.updated_at, project.id),
            reverse=True,
        )
        return ProjectPage(items=tuple(active[offset : offset + limit]), total=len(active))

    async def rename(
        self,
        owner_id: str,
        project_id: str,
        name: str,
        updated_at: datetime,
    ) -> Project | None:
        project = await self.get(owner_id, project_id)
        if project is None:
            return None
        renamed = replace(project, name=name, updated_at=updated_at)
        self.items[project_id] = renamed
        return renamed

    async def soft_delete(
        self,
        owner_id: str,
        project_id: str,
        deleted_at: datetime,
    ) -> bool:
        project = self.items.get(project_id)
        if project is None or project.owner_id != owner_id:
            return False
        if project.deleted_time is None:
            self.items[project_id] = replace(
                project,
                updated_at=deleted_at,
                deleted_time=deleted_at,
            )
        return True


class FailingProjectRepository:
    """模拟所有数据库操作均不可用。"""

    @staticmethod
    def _raise_database_error() -> Never:
        raise OperationalError(
            "SELECT projects",
            {},
            RuntimeError("database password and host must not leak"),
        )

    async def create(self, project: Project) -> Project:
        self._raise_database_error()

    async def get(self, owner_id: str, project_id: str) -> Project | None:
        self._raise_database_error()

    async def list_active(
        self,
        owner_id: str,
        *,
        offset: int,
        limit: int,
    ) -> ProjectPage:
        self._raise_database_error()

    async def rename(
        self,
        owner_id: str,
        project_id: str,
        name: str,
        updated_at: datetime,
    ) -> Project | None:
        self._raise_database_error()

    async def soft_delete(
        self,
        owner_id: str,
        project_id: str,
        deleted_at: datetime,
    ) -> bool:
        self._raise_database_error()


class MutableClock:
    """可以推进的确定性时钟。"""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: Any) -> None:
        self.value += timedelta(**kwargs)


def _sequence_id_factory(*values: str) -> Any:
    iterator = iter(values)
    return lambda: next(iterator)


def _client(
    service: ProjectService,
    *,
    auth_service: AuthService | None = None,
    email: str = "user@example.com",
) -> AuthTestClient:
    return AuthTestClient(
        create_app(
            project_service=service,
            auth_service=auth_service or make_auth_service(),
        ),
        email=email,
    )


def _project(
    project_id: str,
    name: str,
    *,
    owner_id: str = "user_1",
    minute: int,
    deleted: bool = False,
) -> Project:
    timestamp = datetime(2026, 1, 1, 12, minute, tzinfo=UTC)
    return Project(
        id=project_id,
        owner_id=owner_id,
        name=name,
        created_at=timestamp,
        updated_at=timestamp,
        deleted_time=timestamp if deleted else None,
    )


def test_create_returns_201_location_and_normalized_name() -> None:
    repository = InMemoryProjectRepository()
    service = ProjectService(
        repository,
        id_factory=lambda: "project_fixed",
        clock=lambda: datetime(2026, 2, 1, 8, 30, tzinfo=UTC),
    )

    with _client(service) as client:
        response = client.post("/api/v1/projects", json={"name": "  新项目  "})

    assert response.status_code == 201
    assert response.headers["location"] == "/api/v1/projects/project_fixed"
    assert response.json() == {
        "id": "project_fixed",
        "name": "新项目",
        "created_at": "2026-02-01T08:30:00Z",
        "updated_at": "2026-02-01T08:30:00Z",
    }
    assert "owner_id" not in response.json()


def test_default_project_ids_are_process_monotonic_timestamps() -> None:
    first_service = ProjectService(
        InMemoryProjectRepository(),
        clock=lambda: datetime(2026, 2, 1, 8, 30, tzinfo=UTC),
    )
    second_service = ProjectService(
        InMemoryProjectRepository(),
        clock=lambda: datetime(2026, 2, 1, 8, 30, tzinfo=UTC),
    )

    with _client(first_service) as client:
        first_response = client.post("/api/v1/projects", json={"name": "项目一"})
    with _client(second_service) as client:
        second_response = client.post("/api/v1/projects", json={"name": "项目二"})

    first_id = first_response.json()["id"]
    second_id = second_response.json()["id"]
    assert PROJECT_ID_PATTERN.fullmatch(first_id)
    assert PROJECT_ID_PATTERN.fullmatch(second_id)
    assert len(first_id) <= 64
    assert len(second_id) <= 64
    assert all(pattern.fullmatch(first_id) is None for pattern in UUID_PATTERNS)
    assert all(pattern.fullmatch(second_id) is None for pattern in UUID_PATTERNS)
    assert second_id > first_id


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "a" * 121,
        "第一行\n第二行",
        "名称\t后缀",
        "名称\u0085后缀",
    ],
)
def test_invalid_project_names_return_422(name: str) -> None:
    repository = InMemoryProjectRepository()
    service = ProjectService(
        repository,
        id_factory=lambda: "project_invalid",
        clock=lambda: datetime(2026, 2, 1, tzinfo=UTC),
    )

    with _client(service) as client:
        response = client.post("/api/v1/projects", json={"name": name})

    assert response.status_code == 422
    assert response.json() == {"detail": "project_name_invalid"}
    assert repository.items == {}


def test_create_rejects_extra_fields() -> None:
    repository = InMemoryProjectRepository()
    service = ProjectService(
        repository,
        id_factory=lambda: "project_extra",
        clock=lambda: datetime(2026, 2, 1, tzinfo=UTC),
    )

    with _client(service) as client:
        response = client.post(
            "/api/v1/projects",
            json={
                "name": "项目",
                "deleted_time": "2026-02-01T00:00:00Z",
                "owner_id": "attacker",
            },
        )

    assert response.status_code == 422
    assert repository.items == {}


def test_duplicate_names_create_distinct_projects() -> None:
    repository = InMemoryProjectRepository()
    service = ProjectService(
        repository,
        id_factory=_sequence_id_factory("project_1", "project_2"),
        clock=lambda: datetime(2026, 2, 1, tzinfo=UTC),
    )

    with _client(service) as client:
        first = client.post("/api/v1/projects", json={"name": "同名项目"})
        second = client.post("/api/v1/projects", json={"name": "同名项目"})

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["name"] == second.json()["name"] == "同名项目"
    assert len(repository.items) == 2


def test_projects_are_isolated_by_authenticated_user() -> None:
    repository = InMemoryProjectRepository()
    service = ProjectService(
        repository,
        id_factory=_sequence_id_factory("project_user_1", "project_user_2"),
        clock=lambda: datetime(2026, 2, 1, tzinfo=UTC),
    )
    auth_service = make_auth_service()

    with _client(service, auth_service=auth_service, email="user-one@example.com") as client:
        user_one_created = client.post("/api/v1/projects", json={"name": "用户一项目"})
    with _client(service, auth_service=auth_service, email="user-two@example.com") as client:
        user_two_created = client.post("/api/v1/projects", json={"name": "用户二项目"})

    with _client(service, auth_service=auth_service, email="user-one@example.com") as client:
        user_one_list = client.get("/api/v1/projects")
        user_one_gets_user_two = client.get(f"/api/v1/projects/{user_two_created.json()['id']}")
        user_one_renames_user_two = client.patch(
            f"/api/v1/projects/{user_two_created.json()['id']}",
            json={"name": "越权改名"},
        )
        user_one_deletes_user_two = client.delete(
            f"/api/v1/projects/{user_two_created.json()['id']}"
        )
    with _client(service, auth_service=auth_service, email="user-two@example.com") as client:
        user_two_list = client.get("/api/v1/projects")
        user_two_gets_user_one = client.get(f"/api/v1/projects/{user_one_created.json()['id']}")
        user_two_deletes_user_one = client.delete(
            f"/api/v1/projects/{user_one_created.json()['id']}"
        )

    assert user_one_created.status_code == 201
    assert user_two_created.status_code == 201
    assert repository.items["project_user_1"].owner_id == "user_1"
    assert repository.items["project_user_2"].owner_id == "user_2"
    assert [item["id"] for item in user_one_list.json()["items"]] == ["project_user_1"]
    assert [item["id"] for item in user_two_list.json()["items"]] == ["project_user_2"]
    assert "owner_id" not in user_one_list.json()["items"][0]
    assert user_one_gets_user_two.status_code == 404
    assert user_one_renames_user_two.status_code == 404
    assert user_one_deletes_user_two.status_code == 404
    assert user_two_gets_user_one.status_code == 404
    assert user_two_deletes_user_one.status_code == 404
    assert repository.items["project_user_2"].name == "用户二项目"

    with _client(service, auth_service=auth_service, email="user-one@example.com") as client:
        first_delete = client.delete("/api/v1/projects/project_user_1")
        repeated_delete = client.delete("/api/v1/projects/project_user_1")
    with _client(service, auth_service=auth_service, email="user-two@example.com") as client:
        deleted_is_invisible = client.get("/api/v1/projects/project_user_1")
        user_two_still_visible = client.get("/api/v1/projects/project_user_2")

    assert first_delete.status_code == 204
    assert repeated_delete.status_code == 204
    assert deleted_is_invisible.status_code == 404
    assert user_two_still_visible.status_code == 200


def test_project_routes_return_401_without_authentication() -> None:
    service = ProjectService(
        InMemoryProjectRepository(),
        id_factory=lambda: "project_fixed",
        clock=lambda: datetime(2026, 2, 1, tzinfo=UTC),
    )

    with TestClient(create_app(project_service=service)) as client:
        list_response = client.get("/api/v1/projects")
        create_response = client.post("/api/v1/projects", json={"name": "项目"})

    assert list_response.status_code == 401
    assert list_response.json() == {"detail": "not_authenticated"}
    assert create_response.status_code == 401
    assert create_response.json() == {"detail": "not_authenticated"}


def test_revoked_session_cookie_returns_401() -> None:
    service = ProjectService(
        InMemoryProjectRepository(),
        id_factory=lambda: "project_fixed",
        clock=lambda: datetime(2026, 2, 1, tzinfo=UTC),
    )

    with _client(service) as client:
        logout = client.post("/api/v1/auth/logout")
        response = client.get("/api/v1/projects")

    assert logout.status_code == 204
    assert response.status_code == 401
    assert response.json() == {"detail": "not_authenticated"}


def test_create_app_rejects_legacy_current_user_id() -> None:
    legacy_create_app: Any = create_app
    with pytest.raises(TypeError):
        legacy_create_app(current_user_id="user_1")


def test_legacy_principal_environment_and_user_header_do_not_authenticate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATHENA_CURRENT_USER_ID", "user_1")
    service = ProjectService(
        InMemoryProjectRepository(),
        id_factory=lambda: "project_fixed",
        clock=lambda: datetime(2026, 2, 1, tzinfo=UTC),
    )

    with TestClient(
        create_app(
            project_service=service,
            auth_service=make_auth_service(),
        )
    ) as client:
        response = client.get(
            "/api/v1/projects",
            headers={"X-User-ID": "user_1"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "not_authenticated"}


def test_cross_origin_project_write_is_rejected() -> None:
    service = ProjectService(
        InMemoryProjectRepository(),
        id_factory=lambda: "project_fixed",
        clock=lambda: datetime(2026, 2, 1, tzinfo=UTC),
    )

    with _client(service) as client:
        response = client.post(
            "/api/v1/projects",
            headers={"Origin": "https://evil.example.com"},
            json={"name": "项目"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "origin_not_allowed"}


def test_list_returns_active_projects_in_updated_order_with_pagination() -> None:
    repository = InMemoryProjectRepository(
        (
            _project("project_old", "旧项目", minute=0),
            _project("project_middle", "中间项目", minute=1),
            _project("project_new", "新项目", minute=2),
            _project("project_deleted", "已删除", minute=3, deleted=True),
        )
    )
    service = ProjectService(repository, clock=lambda: datetime(2026, 2, 1, tzinfo=UTC))

    with _client(service) as client:
        first_page = client.get("/api/v1/projects", params={"page": 1, "page_size": 2})
        second_page = client.get("/api/v1/projects", params={"page": 2, "page_size": 2})

    assert first_page.status_code == 200
    assert first_page.json() == {
        "items": [
            {
                "id": "project_new",
                "name": "新项目",
                "created_at": "2026-01-01T12:02:00Z",
                "updated_at": "2026-01-01T12:02:00Z",
            },
            {
                "id": "project_middle",
                "name": "中间项目",
                "created_at": "2026-01-01T12:01:00Z",
                "updated_at": "2026-01-01T12:01:00Z",
            },
        ],
        "total": 3,
        "page": 1,
        "page_size": 2,
    }
    assert second_page.status_code == 200
    assert [item["id"] for item in second_page.json()["items"]] == ["project_old"]


@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"page_size": 0},
        {"page_size": 101},
    ],
)
def test_list_rejects_invalid_pagination(params: dict[str, int]) -> None:
    service = ProjectService(
        InMemoryProjectRepository(),
        clock=lambda: datetime(2026, 2, 1, tzinfo=UTC),
    )

    with _client(service) as client:
        response = client.get("/api/v1/projects", params=params)

    assert response.status_code == 422


def test_get_returns_project_or_404_with_consistent_detail() -> None:
    repository = InMemoryProjectRepository(
        (
            _project("project_active", "有效项目", minute=1),
            _project("project_deleted", "已删除", minute=2, deleted=True),
        )
    )
    service = ProjectService(repository, clock=lambda: datetime(2026, 2, 1, tzinfo=UTC))

    with _client(service) as client:
        found = client.get("/api/v1/projects/project_active")
        missing = client.get("/api/v1/projects/project_missing")
        deleted = client.get("/api/v1/projects/project_deleted")

    assert found.status_code == 200
    assert found.json()["name"] == "有效项目"
    assert missing.status_code == 404
    assert missing.json() == {"detail": "project_not_found"}
    assert deleted.status_code == 404
    assert deleted.json() == {"detail": "project_not_found"}


def test_rename_updates_name_and_updated_at_only() -> None:
    original = _project("project_1", "旧名称", minute=1)
    repository = InMemoryProjectRepository((original,))
    clock = MutableClock(original.updated_at)
    service = ProjectService(repository, clock=clock)
    clock.advance(minutes=5)

    with _client(service) as client:
        response = client.patch(
            "/api/v1/projects/project_1",
            json={"name": "  新名称  "},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == original.id
    assert body["name"] == "新名称"
    assert body["created_at"] == "2026-01-01T12:01:00Z"
    assert body["updated_at"] == "2026-01-01T12:06:00Z"
    assert repository.items["project_1"].deleted_time is None


def test_rename_missing_or_deleted_project_returns_404() -> None:
    repository = InMemoryProjectRepository(
        (_project("project_deleted", "已删除", minute=1, deleted=True),)
    )
    service = ProjectService(repository, clock=lambda: datetime(2026, 2, 1, tzinfo=UTC))

    with _client(service) as client:
        missing = client.patch("/api/v1/projects/project_missing", json={"name": "新名称"})
        deleted = client.patch(
            "/api/v1/projects/project_deleted",
            json={"name": "新名称"},
        )

    assert missing.status_code == 404
    assert missing.json() == {"detail": "project_not_found"}
    assert deleted.status_code == 404
    assert deleted.json() == {"detail": "project_not_found"}


def test_delete_is_soft_and_idempotent() -> None:
    original = _project("project_1", "待删除项目", minute=1)
    repository = InMemoryProjectRepository((original,))
    clock = MutableClock(original.updated_at)
    service = ProjectService(repository, clock=clock)
    clock.advance(minutes=3)

    with _client(service) as client:
        first_delete = client.delete("/api/v1/projects/project_1")
        stored_after_delete = repository.items["project_1"]
        second_delete = client.delete("/api/v1/projects/project_1")
        get_after_delete = client.get("/api/v1/projects/project_1")
        rename_after_delete = client.patch(
            "/api/v1/projects/project_1",
            json={"name": "不能复活"},
        )
        missing_delete = client.delete("/api/v1/projects/project_missing")

    assert first_delete.status_code == 204
    assert first_delete.content == b""
    assert stored_after_delete.deleted_time == datetime(2026, 1, 1, 12, 4, tzinfo=UTC)
    assert stored_after_delete.updated_at == datetime(2026, 1, 1, 12, 4, tzinfo=UTC)
    assert second_delete.status_code == 204
    assert get_after_delete.status_code == 404
    assert rename_after_delete.status_code == 404
    assert missing_delete.status_code == 404
    assert missing_delete.json() == {"detail": "project_not_found"}


@pytest.mark.parametrize("name", ["  ", "名称\u0085后缀"])
def test_rename_rejects_invalid_name_without_changing_project(name: str) -> None:
    original = _project("project_1", "旧名称", minute=1)
    repository = InMemoryProjectRepository((original,))
    service = ProjectService(repository, clock=lambda: datetime(2026, 2, 1, tzinfo=UTC))

    with _client(service) as client:
        response = client.patch(
            "/api/v1/projects/project_1",
            json={"name": name},
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "project_name_invalid"}
    assert repository.items["project_1"] == original


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("POST", "/api/v1/projects", {"name": "项目"}),
        ("GET", "/api/v1/projects", None),
        ("GET", "/api/v1/projects/project_1", None),
        ("PATCH", "/api/v1/projects/project_1", {"name": "新名称"}),
        ("DELETE", "/api/v1/projects/project_1", None),
    ],
)
def test_database_failures_return_503_without_leaking_details(
    method: str,
    path: str,
    payload: dict[str, str] | None,
) -> None:
    service = ProjectService(
        FailingProjectRepository(),
        id_factory=lambda: "project_fixed",
        clock=lambda: datetime(2026, 2, 1, tzinfo=UTC),
    )
    request_options: dict[str, Any] = {}
    if payload is not None:
        request_options["json"] = payload

    with _client(service) as client:
        response = client.request(method, path, **request_options)

    assert response.status_code == 503
    assert response.json() == {"detail": "database_unavailable"}
    assert "password" not in response.text
    assert "database password" not in response.text
