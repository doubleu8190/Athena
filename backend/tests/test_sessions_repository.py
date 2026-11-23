"""会话目录 async SQLAlchemy 仓储与转换测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from adapter.auth.model import UserModel
from adapter.models import Base
from adapter.project.model import ProjectModel
from adapter.session.converter import session_to_domain
from adapter.session.model import SessionModel
from adapter.session.repository import SqlAlchemySessionRepository
from domain.project import ProjectNotFoundError
from domain.session import (
    Session,
    SessionProjectLockedError,
    SessionScope,
)


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


async def _make_repository() -> tuple[
    Any,
    async_sessionmaker[AsyncSession],
    SqlAlchemySessionRepository,
]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, session_factory, SqlAlchemySessionRepository(session_factory)


async def _seed_user(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: str,
) -> None:
    timestamp = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        async with session.begin():
            session.add(
                UserModel(
                    id=user_id,
                    email=f"{user_id}@example.com",
                    password_hash="hash:test",
                    display_name=None,
                    status="active",
                    created_at=timestamp,
                    updated_at=timestamp,
                    deleted_time=None,
                )
            )


async def _seed_project(
    session_factory: async_sessionmaker[AsyncSession],
    project_id: str,
    owner_id: str,
) -> None:
    timestamp = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        async with session.begin():
            session.add(
                ProjectModel(
                    id=project_id,
                    owner_id=owner_id,
                    name=f"项目 {project_id}",
                    created_at=timestamp,
                    updated_at=timestamp,
                    deleted_time=None,
                )
            )


def _session(
    session_id: str,
    *,
    owner_id: str = "user_1",
    title: str | None = None,
    project_id: str | None = None,
    last_message_sequence: int = 0,
    minute: int = 0,
    deleted: bool = False,
) -> Session:
    timestamp = datetime(2026, 1, 1, 12, minute, tzinfo=UTC)
    return Session(
        id=session_id,
        owner_id=owner_id,
        title=title,
        project_id=project_id,
        last_message_sequence=last_message_sequence,
        created_at=timestamp,
        updated_at=timestamp,
        deleted_time=timestamp if deleted else None,
    )


def test_repository_returns_only_active_session_for_matching_owner() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_user(session_factory, "user_1")
            await _seed_user(session_factory, "user_2")
            await repository.create(_session("session_active"))
            await repository.create(_session("session_deleted", deleted=True))
            await repository.create(_session("session_other", owner_id="user_2"))

            visible = await repository.get_visible(
                SessionScope(session_id="session_active", owner_id="user_1")
            )
            assert visible == _session("session_active")
            assert (
                await repository.get_visible(
                    SessionScope(session_id="session_deleted", owner_id="user_1")
                )
                is None
            )
            assert (
                await repository.get_visible(
                    SessionScope(session_id="session_other", owner_id="user_1")
                )
                is None
            )
            assert (
                await repository.get_visible(
                    SessionScope(session_id="session_missing", owner_id="user_1")
                )
                is None
            )
        finally:
            await engine.dispose()

    _run(scenario())


def test_converter_normalizes_deleted_timestamp_to_utc() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_user(session_factory, "user_1")
            await repository.create(_session("session_deleted", deleted=True))

            async with session_factory() as session:
                row = await session.get(SessionModel, "session_deleted")
            assert row is not None
            converted = session_to_domain(row)
            assert converted.id == "session_deleted"
            assert converted.owner_id == "user_1"
            assert converted.deleted_time == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        finally:
            await engine.dispose()

    _run(scenario())


def test_repository_creates_lists_and_filters_by_owner_and_project() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_user(session_factory, "user_1")
            await _seed_user(session_factory, "user_2")
            await _seed_project(session_factory, "project_1", "user_1")
            await _seed_project(session_factory, "project_2", "user_1")
            await repository.create(_session("session_bound", project_id="project_1", minute=1))
            await repository.create(_session("session_unbound", minute=2))
            await repository.create(_session("session_other", owner_id="user_2", minute=3))
            await repository.create(
                _session(
                    "session_deleted",
                    project_id="project_1",
                    minute=4,
                    deleted=True,
                )
            )

            active = await repository.list_active(
                "user_1",
                project_id=None,
                unbound=False,
                offset=0,
                limit=20,
            )
            project_sessions = await repository.list_active(
                "user_1",
                project_id="project_1",
                unbound=False,
                offset=0,
                limit=20,
            )
            unbound = await repository.list_active(
                "user_1",
                project_id=None,
                unbound=True,
                offset=0,
                limit=20,
            )

            assert active.total == 2
            assert [item.id for item in active.items] == [
                "session_unbound",
                "session_bound",
            ]
            assert project_sessions.total == 1
            assert [item.id for item in project_sessions.items] == ["session_bound"]
            assert unbound.total == 1
            assert [item.id for item in unbound.items] == ["session_unbound"]
        finally:
            await engine.dispose()

    _run(scenario())


def test_repository_updates_binding_only_before_first_message() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_user(session_factory, "user_1")
            await _seed_project(session_factory, "project_1", "user_1")
            await _seed_project(session_factory, "project_2", "user_1")
            await repository.create(_session("session_1"))
            scope = SessionScope(session_id="session_1", owner_id="user_1")
            timestamp = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)

            renamed = await repository.update(
                scope,
                update_title=True,
                title="新标题",
                update_project=False,
                project_id=None,
                updated_at=timestamp,
            )
            bound = await repository.update(
                scope,
                update_title=False,
                title=None,
                update_project=True,
                project_id="project_1",
                updated_at=timestamp,
            )

            assert renamed is not None
            assert renamed.title == "新标题"
            assert bound is not None
            assert bound.project_id == "project_1"

            async with session_factory() as session:
                async with session.begin():
                    row = await session.get(SessionModel, "session_1")
                    assert row is not None
                    row.last_message_sequence = 1

            with pytest.raises(SessionProjectLockedError):
                await repository.update(
                    scope,
                    update_title=False,
                    title=None,
                    update_project=True,
                    project_id="project_2",
                    updated_at=timestamp,
                )

            title_only = await repository.update(
                scope,
                update_title=True,
                title="消息后改名",
                update_project=False,
                project_id=None,
                updated_at=timestamp,
            )
            assert title_only is not None
            assert title_only.project_id == "project_1"
            assert title_only.title == "消息后改名"
        finally:
            await engine.dispose()

    _run(scenario())


def test_repository_noop_preserves_timestamp_after_first_message() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_user(session_factory, "user_1")
            await _seed_project(session_factory, "project_1", "user_1")
            original = _session(
                "session_1",
                title="同值标题",
                project_id="project_1",
            )
            await repository.create(original)
            async with session_factory() as session:
                async with session.begin():
                    row = await session.get(SessionModel, "session_1")
                    assert row is not None
                    row.last_message_sequence = 1

            result = await repository.update(
                SessionScope(session_id="session_1", owner_id="user_1"),
                update_title=True,
                title="同值标题",
                update_project=True,
                project_id="project_1",
                updated_at=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
            )

            assert result is not None
            assert result.title == original.title
            assert result.project_id == original.project_id
            assert result.updated_at == original.updated_at
            async with session_factory() as session:
                row = await session.get(SessionModel, "session_1")
                assert row is not None
                assert row.updated_at == original.updated_at.replace(tzinfo=None)
        finally:
            await engine.dispose()

    _run(scenario())


def test_repository_keeps_updated_at_monotonic_across_update_and_delete() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_user(session_factory, "user_1")
            await repository.create(_session("session_1"))
            scope = SessionScope(session_id="session_1", owner_id="user_1")
            latest = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
            stale = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)

            await repository.update(
                scope,
                update_title=True,
                title="最近标题",
                update_project=False,
                project_id=None,
                updated_at=latest,
            )
            stale_update = await repository.update(
                scope,
                update_title=True,
                title="较早标题",
                update_project=False,
                project_id=None,
                updated_at=stale,
            )
            assert stale_update is not None
            assert stale_update.updated_at == latest

            assert await repository.soft_delete("user_1", "session_1", stale) is True
            async with session_factory() as session:
                row = await session.get(SessionModel, "session_1")
            assert row is not None
            assert row.updated_at.replace(tzinfo=UTC) == latest
            assert row.deleted_time is not None
            assert row.deleted_time.replace(tzinfo=UTC) == stale
        finally:
            await engine.dispose()

    _run(scenario())


def test_repository_rejects_session_binding_to_another_owners_project() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_user(session_factory, "user_1")
            await _seed_user(session_factory, "user_2")
            await _seed_project(session_factory, "project_other", "user_2")

            with pytest.raises(ProjectNotFoundError):
                await repository.create(_session("session_1", project_id="project_other"))
            assert (
                await repository.get_visible(
                    SessionScope(session_id="session_1", owner_id="user_1")
                )
                is None
            )
        finally:
            await engine.dispose()

    _run(scenario())


def test_repository_soft_deletes_idempotently_and_isolates_owners() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_user(session_factory, "user_1")
            await _seed_user(session_factory, "user_2")
            await _seed_project(session_factory, "project_1", "user_1")
            await repository.create(_session("session_1", project_id="project_1"))
            timestamp = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)

            assert await repository.soft_delete("user_2", "session_1", timestamp) is False
            assert await repository.soft_delete("user_1", "session_1", timestamp) is True
            assert await repository.soft_delete("user_1", "session_1", timestamp) is True
            assert await repository.soft_delete("user_1", "missing", timestamp) is False
            assert (
                await repository.get_visible(
                    SessionScope(session_id="session_1", owner_id="user_1")
                )
                is None
            )
        finally:
            await engine.dispose()

    _run(scenario())
