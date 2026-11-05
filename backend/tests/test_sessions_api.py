"""会话 HTTP API 与应用服务行为测试。"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from auth_support import AuthTestClient, incrementing_factory, make_auth_service
from fastapi.testclient import TestClient

from app.auth.service import AuthService
from app.project.service import ProjectService
from app.session.service import SessionService
from domain.project import Project, ProjectHasSessionsError, ProjectNotFoundError, ProjectPage
from domain.session import (
    Session,
    SessionPage,
    SessionProjectLockedError,
    SessionScope,
)
from main import create_app

SESSION_ID_PATTERN = re.compile(r"^sess_[0-9a-f]{32}$")


class InMemoryProjectRepository:
    """为会话与项目联合测试保存项目实体。"""

    def __init__(self) -> None:
        self.items: dict[str, Project] = {}
        self.session_repository: InMemorySessionRepository | None = None

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
            if self.session_repository is not None and any(
                session.owner_id == owner_id
                and session.project_id == project_id
                and session.deleted_time is None
                for session in self.session_repository.items.values()
            ):
                raise ProjectHasSessionsError("project_has_sessions")
            self.items[project_id] = replace(
                project,
                updated_at=deleted_at,
                deleted_time=deleted_at,
            )
        return True


class InMemorySessionRepository:
    """为 HTTP 测试保存并通过 owner 过滤会话实体。"""

    def __init__(self, project_repository: InMemoryProjectRepository) -> None:
        self.items: dict[str, Session] = {}
        self._project_repository = project_repository

    async def create(self, session: Session) -> Session:
        if session.project_id is not None:
            await self._require_visible_project(session.owner_id, session.project_id)
        self.items[session.id] = session
        return session

    async def get_visible(self, scope: SessionScope) -> Session | None:
        session = self.items.get(scope.session_id)
        if (
            session is None
            or session.owner_id != scope.owner_id
            or session.deleted_time is not None
        ):
            return None
        return session

    async def list_active(
        self,
        owner_id: str,
        *,
        project_id: str | None,
        unbound: bool,
        offset: int,
        limit: int,
    ) -> SessionPage:
        active = [
            session
            for session in self.items.values()
            if session.owner_id == owner_id and session.deleted_time is None
        ]
        if project_id is not None:
            active = [session for session in active if session.project_id == project_id]
        if unbound:
            active = [session for session in active if session.project_id is None]
        active.sort(key=lambda session: (session.updated_at, session.id), reverse=True)
        return SessionPage(
            items=tuple(active[offset : offset + limit]),
            total=len(active),
        )

    async def update(
        self,
        scope: SessionScope,
        *,
        update_title: bool,
        title: str | None,
        update_project: bool,
        project_id: str | None,
        updated_at: datetime,
    ) -> Session | None:
        session = await self.get_visible(scope)
        if session is None:
            return None
        title_changed = update_title and title != session.title
        project_changed = update_project and project_id != session.project_id
        if not title_changed and not project_changed:
            return session
        if project_changed and session.last_message_sequence > 0:
            raise SessionProjectLockedError("session_project_locked")
        if project_changed and project_id is not None:
            await self._require_visible_project(scope.owner_id, project_id)
        updated = replace(
            session,
            title=title if title_changed else session.title,
            project_id=project_id if project_changed else session.project_id,
            updated_at=updated_at,
        )
        self.items[session.id] = updated
        return updated

    async def soft_delete(
        self,
        owner_id: str,
        session_id: str,
        deleted_at: datetime,
    ) -> bool:
        session = self.items.get(session_id)
        if session is None or session.owner_id != owner_id:
            return False
        if session.deleted_time is None:
            self.items[session_id] = replace(
                session,
                updated_at=deleted_at,
                deleted_time=deleted_at,
            )
        return True

    async def _require_visible_project(self, owner_id: str, project_id: str) -> None:
        if await self._project_repository.get(owner_id, project_id) is None:
            raise ProjectNotFoundError("project_not_found")


class IncrementingClock:
    """每次调用返回递增时间的确定性时钟。"""

    def __init__(self, value: datetime) -> None:
        self._value = value

    def __call__(self) -> datetime:
        current = self._value
        self._value += timedelta(seconds=1)
        return current


def _build_services() -> tuple[
    InMemoryProjectRepository,
    InMemorySessionRepository,
    ProjectService,
    SessionService,
    AuthService,
]:
    project_repository = InMemoryProjectRepository()
    session_repository = InMemorySessionRepository(project_repository)
    project_repository.session_repository = session_repository
    project_service = ProjectService(
        project_repository,
        id_factory=incrementing_factory("project_"),
        clock=IncrementingClock(datetime(2026, 1, 1, tzinfo=UTC)),
    )
    session_service = SessionService(
        session_repository,
        id_factory=incrementing_factory("session_"),
        clock=IncrementingClock(datetime(2026, 1, 1, 1, tzinfo=UTC)),
    )
    return (
        project_repository,
        session_repository,
        project_service,
        session_service,
        make_auth_service(),
    )


def _app(
    project_service: ProjectService,
    session_service: SessionService,
    auth_service: AuthService,
) -> Any:
    return create_app(
        project_service=project_service,
        session_service=session_service,
        auth_service=auth_service,
    )


def _client(
    project_service: ProjectService,
    session_service: SessionService,
    auth_service: AuthService,
    *,
    email: str,
) -> AuthTestClient:
    return AuthTestClient(
        _app(project_service, session_service, auth_service),
        email=email,
    )


def test_create_returns_bound_and_unbound_sessions() -> None:
    project_repository, session_repository, project_service, session_service, auth_service = (
        _build_services()
    )

    with _client(
        project_service,
        session_service,
        auth_service,
        email="owner@example.com",
    ) as client:
        project_response = client.post("/api/v1/projects", json={"name": "项目"})
        unbound_response = client.post(
            "/api/v1/sessions",
            json={"title": "  未绑定会话  "},
        )
        bound_response = client.post(
            "/api/v1/sessions",
            json={"title": None, "project_id": project_response.json()["id"]},
        )

    assert project_response.status_code == 201
    assert unbound_response.status_code == 201
    assert unbound_response.headers["location"] == "/api/v1/sessions/session_1"
    assert unbound_response.json() == {
        "id": "session_1",
        "title": "未绑定会话",
        "project_id": None,
        "created_at": "2026-01-01T01:00:00Z",
        "updated_at": "2026-01-01T01:00:00Z",
    }
    assert bound_response.status_code == 201
    assert bound_response.json()["title"] is None
    assert bound_response.json()["project_id"] == project_response.json()["id"]
    assert "owner_id" not in bound_response.json()
    assert len(session_repository.items) == 2
    assert all(session.owner_id == "user_1" for session in session_repository.items.values())
    assert project_repository.items[project_response.json()["id"]].owner_id == "user_1"


@pytest.mark.parametrize(
    "title",
    ["", "   ", "a" * 121, "第一行\n第二行", "标题\u0085后缀"],
)
def test_create_rejects_invalid_title(title: str) -> None:
    _, session_repository, project_service, session_service, auth_service = _build_services()

    with _client(
        project_service,
        session_service,
        auth_service,
        email="owner@example.com",
    ) as client:
        response = client.post("/api/v1/sessions", json={"title": title})

    assert response.status_code == 422
    assert response.json() == {"detail": "session_title_invalid"}
    assert session_repository.items == {}


def test_default_session_ids_use_prefixed_uuid4_hex() -> None:
    project_repository = InMemoryProjectRepository()
    session_repository = InMemorySessionRepository(project_repository)
    project_repository.session_repository = session_repository
    project_service = ProjectService(project_repository)
    session_service = SessionService(
        session_repository,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )
    auth_service = make_auth_service()

    with _client(
        project_service,
        session_service,
        auth_service,
        email="owner@example.com",
    ) as client:
        first = client.post("/api/v1/sessions", json={})
        second = client.post("/api/v1/sessions", json={})

    first_id = first.json()["id"]
    second_id = second.json()["id"]
    assert SESSION_ID_PATTERN.fullmatch(first_id)
    assert SESSION_ID_PATTERN.fullmatch(second_id)
    assert first_id != second_id
    assert len(first_id) <= 64


def test_create_rejects_extra_fields_and_unknown_project() -> None:
    _, session_repository, project_service, session_service, auth_service = _build_services()

    with _client(
        project_service,
        session_service,
        auth_service,
        email="owner@example.com",
    ) as client:
        extra = client.post(
            "/api/v1/sessions",
            json={
                "title": "会话",
                "owner_id": "attacker",
                "deleted_time": "2026-01-01T00:00:00Z",
            },
        )
        missing_project = client.post(
            "/api/v1/sessions",
            json={"project_id": "project_missing"},
        )

    assert extra.status_code == 422
    assert missing_project.status_code == 404
    assert missing_project.json() == {"detail": "project_not_found"}
    assert session_repository.items == {}


def test_sessions_are_isolated_by_authenticated_user_and_project_owner() -> None:
    _, session_repository, project_service, session_service, auth_service = _build_services()

    with _client(
        project_service,
        session_service,
        auth_service,
        email="user-one@example.com",
    ) as client:
        project = client.post("/api/v1/projects", json={"name": "用户一项目"})
        user_one_session = client.post(
            "/api/v1/sessions",
            json={"title": "用户一会话"},
        )
    with _client(
        project_service,
        session_service,
        auth_service,
        email="user-two@example.com",
    ) as client:
        cross_project = client.post(
            "/api/v1/sessions",
            json={"project_id": project.json()["id"]},
        )
        user_two_session = client.post(
            "/api/v1/sessions",
            json={"title": "用户二会话"},
        )
        get_user_one = client.get(f"/api/v1/sessions/{user_one_session.json()['id']}")
        patch_user_one = client.patch(
            f"/api/v1/sessions/{user_one_session.json()['id']}",
            json={"title": "越权改名"},
        )
        delete_user_one = client.delete(f"/api/v1/sessions/{user_one_session.json()['id']}")
        user_two_list = client.get("/api/v1/sessions")

    assert cross_project.status_code == 404
    assert cross_project.json() == {"detail": "project_not_found"}
    assert get_user_one.status_code == 404
    assert patch_user_one.status_code == 404
    assert delete_user_one.status_code == 404
    assert [item["id"] for item in user_two_list.json()["items"]] == [user_two_session.json()["id"]]
    assert session_repository.items[user_one_session.json()["id"]].title == "用户一会话"


def test_list_filters_paginates_and_orders_by_recent_update() -> None:
    _, _, project_service, session_service, auth_service = _build_services()

    with _client(
        project_service,
        session_service,
        auth_service,
        email="owner@example.com",
    ) as client:
        project = client.post("/api/v1/projects", json={"name": "项目"})
        first = client.post(
            "/api/v1/sessions",
            json={"title": "绑定会话", "project_id": project.json()["id"]},
        )
        second = client.post("/api/v1/sessions", json={"title": "未绑定会话"})
        client.patch(f"/api/v1/sessions/{first.json()['id']}", json={"title": "最近更新"})
        all_sessions = client.get("/api/v1/sessions")
        first_page = client.get("/api/v1/sessions", params={"page": 1, "page_size": 1})
        project_sessions = client.get(
            "/api/v1/sessions",
            params={"project_id": project.json()["id"]},
        )
        unbound_sessions = client.get(
            "/api/v1/sessions",
            params={"unbound": True},
        )
        invalid_filter = client.get(
            "/api/v1/sessions",
            params={"project_id": project.json()["id"], "unbound": True},
        )

    assert all_sessions.status_code == 200
    assert [item["title"] for item in all_sessions.json()["items"]] == [
        "最近更新",
        "未绑定会话",
    ]
    assert first_page.json()["total"] == 2
    assert first_page.json()["page_size"] == 1
    assert len(first_page.json()["items"]) == 1
    assert [item["id"] for item in project_sessions.json()["items"]] == [first.json()["id"]]
    assert [item["id"] for item in unbound_sessions.json()["items"]] == [second.json()["id"]]
    assert invalid_filter.status_code == 422
    assert invalid_filter.json() == {"detail": "invalid_session_filter"}


def test_patch_updates_title_and_binding_until_first_message() -> None:
    _, session_repository, project_service, session_service, auth_service = _build_services()

    with _client(
        project_service,
        session_service,
        auth_service,
        email="owner@example.com",
    ) as client:
        project = client.post("/api/v1/projects", json={"name": "项目"})
        session_response = client.post("/api/v1/sessions", json={})
        session_id = session_response.json()["id"]
        renamed = client.patch(
            f"/api/v1/sessions/{session_id}",
            json={"title": "  新标题  "},
        )
        bound = client.patch(
            f"/api/v1/sessions/{session_id}",
            json={"project_id": project.json()["id"]},
        )
        unbound = client.patch(
            f"/api/v1/sessions/{session_id}",
            json={"project_id": None, "title": None},
        )
        empty_update = client.patch(f"/api/v1/sessions/{session_id}", json={})

        session_repository.items[session_id] = replace(
            session_repository.items[session_id],
            project_id=project.json()["id"],
            last_message_sequence=1,
        )
        locked = client.patch(
            f"/api/v1/sessions/{session_id}",
            json={"project_id": None},
        )
        title_after_message = client.patch(
            f"/api/v1/sessions/{session_id}",
            json={"title": "消息后标题"},
        )

    assert renamed.status_code == 200
    assert renamed.json()["title"] == "新标题"
    assert bound.json()["project_id"] == project.json()["id"]
    assert unbound.json()["project_id"] is None
    assert unbound.json()["title"] is None
    assert empty_update.status_code == 422
    assert empty_update.json() == {"detail": "session_update_invalid"}
    assert locked.status_code == 409
    assert locked.json() == {"detail": "session_project_locked"}
    assert title_after_message.status_code == 200
    assert title_after_message.json()["title"] == "消息后标题"
    assert title_after_message.json()["project_id"] == project.json()["id"]


@pytest.mark.parametrize("target_project", ["project_missing", "foreign_project"])
def test_patch_invisible_session_returns_not_found_before_project_error(
    target_project: str,
) -> None:
    _, session_repository, project_service, session_service, auth_service = _build_services()

    with _client(
        project_service,
        session_service,
        auth_service,
        email="other@example.com",
    ) as other:
        foreign_project = other.post("/api/v1/projects", json={"name": "他人项目"})
        foreign_session = other.post("/api/v1/sessions", json={})

    with _client(
        project_service,
        session_service,
        auth_service,
        email="owner@example.com",
    ) as owner:
        owned_session = owner.post("/api/v1/sessions", json={})
        owned_session_id = owned_session.json()["id"]
        session_repository.items[owned_session_id] = replace(
            session_repository.items[owned_session_id],
            deleted_time=datetime(2026, 1, 2, tzinfo=UTC),
        )
        project_id = (
            "project_missing"
            if target_project == "project_missing"
            else foreign_project.json()["id"]
        )
        responses = (
            owner.patch(
                "/api/v1/sessions/session_missing",
                json={"project_id": project_id},
            ),
            owner.patch(
                f"/api/v1/sessions/{owned_session_id}",
                json={"project_id": project_id},
            ),
            owner.patch(
                f"/api/v1/sessions/{foreign_session.json()['id']}",
                json={"project_id": project_id},
            ),
        )

    for response in responses:
        assert response.status_code == 404
        assert response.json() == {"detail": "session_not_found"}


@pytest.mark.parametrize("session_kind", ["missing", "deleted", "foreign"])
@pytest.mark.parametrize(
    "payload",
    [
        {"title": ""},
        {"title": "a" * 121},
        {"project_id": ""},
        {"project_id": "p" * 65},
    ],
)
def test_patch_invisible_session_precedes_field_validation(
    session_kind: str,
    payload: dict[str, str],
) -> None:
    _, session_repository, project_service, session_service, auth_service = _build_services()

    with _client(
        project_service,
        session_service,
        auth_service,
        email="other@example.com",
    ) as other:
        foreign_session = other.post("/api/v1/sessions", json={})

    with _client(
        project_service,
        session_service,
        auth_service,
        email="owner@example.com",
    ) as owner:
        if session_kind == "missing":
            session_id = "session_missing"
        elif session_kind == "foreign":
            session_id = foreign_session.json()["id"]
        else:
            deleted_session = owner.post("/api/v1/sessions", json={})
            session_id = deleted_session.json()["id"]
            session_repository.items[session_id] = replace(
                session_repository.items[session_id],
                deleted_time=datetime(2026, 1, 2, tzinfo=UTC),
            )

        response = owner.patch(f"/api/v1/sessions/{session_id}", json=payload)

    assert response.status_code == 404
    assert response.json() == {"detail": "session_not_found"}


@pytest.mark.parametrize(
    ("payload", "status_code", "detail"),
    [
        ({"title": ""}, 422, "session_title_invalid"),
        ({"title": "a" * 121}, 422, "session_title_invalid"),
        ({"project_id": ""}, 404, "project_not_found"),
        ({"project_id": "p" * 65}, 404, "project_not_found"),
    ],
)
def test_patch_visible_session_keeps_existing_field_errors(
    payload: dict[str, str],
    status_code: int,
    detail: str,
) -> None:
    _, _, project_service, session_service, auth_service = _build_services()

    with _client(
        project_service,
        session_service,
        auth_service,
        email="owner@example.com",
    ) as client:
        created = client.post("/api/v1/sessions", json={})
        response = client.patch(
            f"/api/v1/sessions/{created.json()['id']}",
            json=payload,
        )

    assert response.status_code == status_code
    assert response.json() == {"detail": detail}


def test_patch_noop_preserves_timestamp_and_list_order_after_first_message() -> None:
    _, session_repository, project_service, session_service, auth_service = _build_services()

    with _client(
        project_service,
        session_service,
        auth_service,
        email="owner@example.com",
    ) as client:
        project = client.post("/api/v1/projects", json={"name": "项目"})
        first = client.post(
            "/api/v1/sessions",
            json={"title": "同值标题", "project_id": project.json()["id"]},
        )
        second = client.post("/api/v1/sessions", json={"title": "较新会话"})
        first_id = first.json()["id"]
        original_updated_at = first.json()["updated_at"]
        session_repository.items[first_id] = replace(
            session_repository.items[first_id],
            last_message_sequence=1,
        )

        noop = client.patch(
            f"/api/v1/sessions/{first_id}",
            json={"title": "同值标题", "project_id": project.json()["id"]},
        )
        listing = client.get("/api/v1/sessions")

    assert noop.status_code == 200
    assert noop.json()["title"] == "同值标题"
    assert noop.json()["project_id"] == project.json()["id"]
    assert noop.json()["updated_at"] == original_updated_at
    assert [item["id"] for item in listing.json()["items"]] == [
        second.json()["id"],
        first_id,
    ]


def test_delete_is_soft_idempotent_and_hides_session() -> None:
    _, session_repository, project_service, session_service, auth_service = _build_services()

    with _client(
        project_service,
        session_service,
        auth_service,
        email="owner@example.com",
    ) as client:
        created = client.post("/api/v1/sessions", json={"title": "待删除"})
        session_id = created.json()["id"]
        first_delete = client.delete(f"/api/v1/sessions/{session_id}")
        second_delete = client.delete(f"/api/v1/sessions/{session_id}")
        get_after_delete = client.get(f"/api/v1/sessions/{session_id}")
        missing_delete = client.delete("/api/v1/sessions/session_missing")

    assert first_delete.status_code == 204
    assert second_delete.status_code == 204
    assert get_after_delete.status_code == 404
    assert missing_delete.status_code == 404
    assert session_repository.items[session_id].deleted_time is not None


def test_project_delete_is_blocked_while_active_bound_session_exists() -> None:
    _, _, project_service, session_service, auth_service = _build_services()

    with _client(
        project_service,
        session_service,
        auth_service,
        email="owner@example.com",
    ) as client:
        project = client.post("/api/v1/projects", json={"name": "被占用项目"})
        session = client.post(
            "/api/v1/sessions",
            json={"project_id": project.json()["id"]},
        )
        blocked = client.delete(f"/api/v1/projects/{project.json()['id']}")
        client.delete(f"/api/v1/sessions/{session.json()['id']}")
        deleted = client.delete(f"/api/v1/projects/{project.json()['id']}")

    assert blocked.status_code == 409
    assert blocked.json() == {"detail": "project_has_sessions"}
    assert deleted.status_code == 204


def test_session_routes_require_authentication_and_reject_cross_origin_writes() -> None:
    _, _, project_service, session_service, auth_service = _build_services()
    with TestClient(_app(project_service, session_service, auth_service)) as client:
        unauthenticated_list = client.get("/api/v1/sessions")
        unauthenticated_create = client.post("/api/v1/sessions", json={})

    with _client(
        project_service,
        session_service,
        auth_service,
        email="owner@example.com",
    ) as client:
        cross_origin = client.post(
            "/api/v1/sessions",
            headers={"Origin": "https://evil.example.com"},
            json={},
        )

    assert unauthenticated_list.status_code == 401
    assert unauthenticated_create.status_code == 401
    assert cross_origin.status_code == 403
    assert cross_origin.json() == {"detail": "origin_not_allowed"}


def test_patch_rejects_unknown_project_without_changing_session() -> None:
    _, session_repository, project_service, session_service, auth_service = _build_services()

    with _client(
        project_service,
        session_service,
        auth_service,
        email="owner@example.com",
    ) as client:
        created = client.post("/api/v1/sessions", json={"title": "原会话"})
        session_id = created.json()["id"]
        response = client.patch(
            f"/api/v1/sessions/{session_id}",
            json={"project_id": "project_missing"},
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "project_not_found"}
    assert session_repository.items[session_id].title == "原会话"
    assert session_repository.items[session_id].project_id is None
