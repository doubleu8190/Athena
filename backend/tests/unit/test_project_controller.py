"""项目 HTTP 响应的日期时间序列化测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from application.projects import ProjectService
from bootstrap import create_app
from domain.projects import Project


class ProjectRepository:
    """为项目路由测试保存项目实体。"""

    def __init__(self) -> None:
        self.items: dict[str, Project] = {
            "project-1": Project(
                id="project-1",
                name="Existing",
                created_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
                updated_at=datetime(2026, 1, 2, 12, tzinfo=timezone.utc),
            )
        }

    async def create(self, project: Project) -> Project:
        """保存项目并返回相同实体。"""
        self.items[project.id] = project
        return project

    async def get(self, project_id: str) -> Project | None:
        """按 ID 返回项目；测试仓储不模拟软删除。"""
        return self.items.get(project_id)

    async def list(self) -> list[Project]:
        """返回所有测试项目。"""
        return list(self.items.values())

    async def rename(self, project_id: str, name: str) -> Project | None:
        """更新项目名称和修改时间。"""
        project = self.items.get(project_id)
        if project is None:
            return None
        updated = Project(
            id=project.id,
            name=name,
            created_at=project.created_at,
            updated_at=datetime(2026, 1, 3, 12, tzinfo=timezone.utc),
        )
        self.items[project_id] = updated
        return updated

    async def delete(self, project_id: str) -> bool:
        """删除项目并返回是否找到目标。"""
        return self.items.pop(project_id, None) is not None


def test_project_routes_serialize_datetime_fields_as_iso_strings() -> None:
    """列表、创建和重命名接口均应接收领域 datetime 并输出 ISO 时间。"""
    repository = ProjectRepository()
    service = ProjectService(
        repository,
        id_factory=lambda: "project-2",
        clock=lambda: datetime(2026, 2, 1, 9, tzinfo=timezone.utc),
    )
    client = TestClient(create_app(project_service=service))

    listed = client.get("/api/projects")
    assert listed.status_code == 200
    assert listed.json()[0]["created_at"] == "2026-01-01T12:00:00Z"
    assert listed.json()[0]["updated_at"] == "2026-01-02T12:00:00Z"

    created = client.post("/api/projects", json={"name": "New"})
    assert created.status_code == 200
    assert created.json()["created_at"] == "2026-02-01T09:00:00Z"
    assert created.json()["updated_at"] == "2026-02-01T09:00:00Z"

    renamed = client.patch("/api/projects/project-1", json={"name": "Renamed"})
    assert renamed.status_code == 200
    assert renamed.json()["created_at"] == "2026-01-01T12:00:00Z"
    assert renamed.json()["updated_at"] == "2026-01-03T12:00:00Z"
