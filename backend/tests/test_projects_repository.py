"""项目 async SQLAlchemy 仓储测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from adapter.auth.model import UserModel
from adapter.models import Base
from adapter.project.model import ProjectModel
from adapter.project.repository import SqlAlchemyProjectRepository
from adapter.session.model import SessionModel
from domain.project import Project, ProjectHasSessionsError


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


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


async def _make_repository() -> tuple[
    Any,
    async_sessionmaker[AsyncSession],
    SqlAlchemyProjectRepository,
]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, session_factory, SqlAlchemyProjectRepository(session_factory)


def test_repository_orders_filters_and_paginates_active_projects() -> None:
    async def scenario() -> None:
        engine, _, repository = await _make_repository()
        try:
            await repository.create(_project("project_old", "旧项目", minute=0))
            await repository.create(_project("project_new", "新项目", minute=2))
            await repository.create(_project("project_middle", "中间项目", minute=1))
            await repository.create(_project("project_deleted", "已删除", minute=3, deleted=True))

            first_page = await repository.list_active("user_1", offset=0, limit=2)
            second_page = await repository.list_active("user_1", offset=2, limit=2)

            assert first_page.total == 3
            assert [project.id for project in first_page.items] == [
                "project_new",
                "project_middle",
            ]
            assert second_page.total == 3
            assert [project.id for project in second_page.items] == ["project_old"]
            assert await repository.get("user_1", "project_deleted") is None
        finally:
            await engine.dispose()

    _run(scenario())


def test_repository_allows_duplicate_names_and_renames_atomically() -> None:
    async def scenario() -> None:
        engine, _, repository = await _make_repository()
        try:
            created = await repository.create(_project("project_1", "同名项目", minute=1))
            await repository.create(_project("project_2", "同名项目", minute=2))
            updated_at = created.updated_at + timedelta(minutes=5)

            renamed = await repository.rename(
                "user_1",
                created.id,
                "改名后",
                updated_at,
            )

            assert renamed is not None
            assert renamed.name == "改名后"
            assert renamed.created_at == created.created_at
            assert renamed.updated_at == updated_at
            assert (
                await repository.rename(
                    "user_1",
                    "project_missing",
                    "不存在",
                    updated_at,
                )
                is None
            )
        finally:
            await engine.dispose()

    _run(scenario())


def test_repository_soft_deletes_idempotently_and_keeps_row() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            original = await repository.create(_project("project_1", "待删除项目", minute=1))
            deleted_at = original.updated_at + timedelta(minutes=4)

            first_delete = await repository.soft_delete("user_1", original.id, deleted_at)
            second_delete = await repository.soft_delete(
                "user_1",
                original.id,
                deleted_at + timedelta(minutes=4),
            )

            assert first_delete is True
            assert second_delete is True
            assert await repository.get("user_1", original.id) is None
            assert (
                await repository.rename(
                    "user_1",
                    original.id,
                    "不能复活",
                    deleted_at,
                )
                is None
            )
            page = await repository.list_active("user_1", offset=0, limit=20)
            assert page.total == 0

            async with session_factory() as session:
                row = await session.scalar(
                    select(ProjectModel).where(ProjectModel.id == original.id)
                )
            assert row is not None
            assert row.deleted_time is not None
            assert row.deleted_time.replace(tzinfo=UTC) == deleted_at
            assert row.updated_at.replace(tzinfo=UTC) == deleted_at
            assert (
                await repository.soft_delete(
                    "user_1",
                    "project_missing",
                    deleted_at,
                )
                is False
            )
        finally:
            await engine.dispose()

    _run(scenario())


def test_repository_isolates_projects_by_owner() -> None:
    async def scenario() -> None:
        engine, _, repository = await _make_repository()
        try:
            user_one = await repository.create(
                _project("project_1", "用户一项目", owner_id="user_1", minute=1)
            )
            user_two = await repository.create(
                _project("project_2", "用户二项目", owner_id="user_2", minute=2)
            )

            page_one = await repository.list_active("user_1", offset=0, limit=20)
            page_two = await repository.list_active("user_2", offset=0, limit=20)
            assert [project.id for project in page_one.items] == [user_one.id]
            assert [project.id for project in page_two.items] == [user_two.id]

            assert await repository.get("user_1", user_two.id) is None
            assert (
                await repository.rename(
                    "user_1",
                    user_two.id,
                    "跨用户改名",
                    user_two.updated_at,
                )
                is None
            )
            assert (
                await repository.soft_delete(
                    "user_1",
                    user_two.id,
                    user_two.updated_at,
                )
                is False
            )
            assert await repository.get("user_2", user_two.id) == user_two
            assert (
                await repository.soft_delete(
                    "user_2",
                    user_two.id,
                    user_two.updated_at,
                )
                is True
            )
            assert (
                await repository.soft_delete(
                    "user_2",
                    user_two.id,
                    user_two.updated_at,
                )
                is True
            )
        finally:
            await engine.dispose()

    _run(scenario())


def test_repository_refuses_to_delete_project_with_active_bound_session() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            timestamp = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
            async with session_factory() as session:
                async with session.begin():
                    session.add(
                        UserModel(
                            id="user_1",
                            email="user_1@example.com",
                            password_hash="hash:test",
                            display_name=None,
                            status="active",
                            created_at=timestamp,
                            updated_at=timestamp,
                            deleted_time=None,
                        )
                    )
            project = await repository.create(_project("project_1", "占用项目", minute=1))
            async with session_factory() as session:
                async with session.begin():
                    session.add(
                        SessionModel(
                            id="session_1",
                            owner_id="user_1",
                            title=None,
                            project_id=project.id,
                            last_message_sequence=0,
                            created_at=timestamp,
                            updated_at=timestamp,
                            deleted_time=None,
                        )
                    )

            with pytest.raises(ProjectHasSessionsError):
                await repository.soft_delete("user_1", project.id, timestamp + timedelta(hours=1))

            assert await repository.get("user_1", project.id) == project

            async with session_factory() as session:
                async with session.begin():
                    row = await session.get(SessionModel, "session_1")
                    assert row is not None
                    row.deleted_time = timestamp + timedelta(hours=2)

            assert await repository.soft_delete(
                "user_1",
                project.id,
                timestamp + timedelta(hours=3),
            )
        finally:
            await engine.dispose()

    _run(scenario())
