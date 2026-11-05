"""Session 与 Project 数据库行锁的真实 PostgreSQL 并发测试。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_hex
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from adapter.auth.model import UserModel
from adapter.message.model import ConversationMessageModel
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.project.model import ProjectModel
from adapter.project.repository import SqlAlchemyProjectRepository
from adapter.session.model import SessionModel
from adapter.session.repository import SqlAlchemySessionRepository
from app.message.service import MessageService
from app.project.service import ProjectService
from app.session.service import SessionService
from domain.message import (
    ConversationMessage,
    ConversationMessageDraft,
    SessionNotFoundError,
    TextPart,
)
from domain.project import ProjectHasSessionsError, ProjectNotFoundError
from domain.session import SessionProjectLockedError

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
ALEMBIC_INI = Path(__file__).resolve().parents[3] / "backend" / "alembic.ini"
LOCK_WAIT_TIMEOUT = 30.0
TASK_TIMEOUT = 30.0
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="ATHENA_TEST_DATABASE_URL is not configured",
    ),
]


class PausingMessageRepository(SqlAlchemyMessageRepository):
    """在取得 Session 行锁后暂停，便于确定性地制造并发交错。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)
        self.session_locked = asyncio.Event()
        self.release_session = asyncio.Event()
        self._paused = False

    async def _find_conversation(
        self,
        session: AsyncSession,
        draft: ConversationMessageDraft,
    ) -> ConversationMessage | None:
        if not self._paused:
            self._paused = True
            self.session_locked.set()
            await self.release_session.wait()
        return await super()._find_conversation(session, draft)


class PausingSessionRepository(SqlAlchemySessionRepository):
    """在取得 Project 共享行锁后暂停，便于观察删除侧阻塞。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)
        self.project_shared_locked = asyncio.Event()
        self.release_project = asyncio.Event()
        self._paused = False

    async def _lock_active_project(
        self,
        database_session: AsyncSession,
        owner_id: str,
        project_id: str,
    ) -> None:
        await super()._lock_active_project(database_session, owner_id, project_id)
        if not self._paused:
            self._paused = True
            self.project_shared_locked.set()
            await self.release_project.wait()


@pytest.fixture(scope="module")
def migrated_schema() -> Iterator[str]:
    """为整个模块创建一次临时 schema，并在结束后删除。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"athena_v2_row_locks_{token_hex(8)}"

    async def create_schema() -> None:
        admin_engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            migration_config = Config(str(ALEMBIC_INI))
            migration_config.set_main_option(
                "sqlalchemy.url",
                database_url.replace("%", "%%"),
            )
            migration_config.attributes["connect_args"] = {
                "server_settings": {"search_path": schema}
            }
            await asyncio.to_thread(command.upgrade, migration_config, "head")
        except BaseException:
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            raise
        finally:
            await admin_engine.dispose()

    async def drop_schema() -> None:
        admin_engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            await admin_engine.dispose()

    asyncio.run(create_schema())
    try:
        yield schema
    finally:
        asyncio.run(drop_schema())


def _engine(database_url: str, schema: str, application_name: str) -> AsyncEngine:
    """创建带独立 application_name 的临时 schema 连接引擎。"""
    return create_async_engine(
        database_url,
        poolclass=NullPool,
        connect_args={
            "server_settings": {
                "search_path": schema,
                "application_name": application_name,
            }
        },
    )


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


async def _blocked_query_count(
    engine: AsyncEngine,
    application_name: str,
    query_fragment: str,
) -> int:
    """统计当前应用名下正在等待数据库行锁的语句。"""
    async with engine.connect() as connection:
        count = await connection.scalar(
            text(
                """
                SELECT count(*)
                FROM pg_stat_activity
                WHERE application_name = :application_name
                  AND wait_event_type = 'Lock'
                  AND query ILIKE :query_fragment
                """
            ),
            {
                "application_name": application_name,
                "query_fragment": f"%{query_fragment}%",
            },
        )
    return int(count or 0)


async def _wait_for_blocked_task(
    task: asyncio.Task[Any],
    engine: AsyncEngine,
    application_name: str,
    query_fragment: str,
) -> None:
    """等到目标任务确实进入数据库行锁等待，而不是依赖固定 sleep。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + LOCK_WAIT_TIMEOUT
    while loop.time() < deadline:
        if task.done():
            if task.cancelled():
                raise AssertionError("任务在进入行锁等待前被取消")
            error = task.exception()
            if error is not None:
                raise AssertionError("任务在进入行锁等待前异常结束") from error
            raise AssertionError("任务未等待行锁便已完成")
        if await _blocked_query_count(engine, application_name, query_fragment) > 0:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"等待数据库行锁超时: {query_fragment}")


async def _cancel_tasks(*tasks: asyncio.Task[Any]) -> None:
    """取消并收敛测试任务，确保事务和连接得到清理。"""
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _session_row(
    session_factory: async_sessionmaker[AsyncSession],
    session_id: str,
) -> SessionModel:
    async with session_factory() as session:
        row = await session.get(SessionModel, session_id)
    assert row is not None
    return row


async def _conversation_count(
    session_factory: async_sessionmaker[AsyncSession],
    session_id: str,
) -> int:
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ConversationMessageModel)
            .where(ConversationMessageModel.session_id == session_id)
        )
    return int(count or 0)


def test_session_rebind_and_first_message_serialize_on_session_row(
    migrated_schema: str,
) -> None:
    """首条消息先锁 Session 时，改绑必须等待并在提交后返回 409 对应错误。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    async def scenario() -> None:
        suffix = token_hex(6)
        application_name = f"athena_rebind_{suffix}"
        engine = _engine(database_url, migrated_schema, application_name)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        owner_id = f"user_{suffix}"
        project_id = f"project_{suffix}"
        message_repository = PausingMessageRepository(session_factory)
        message_service = MessageService(
            message_repository,
            clock=lambda: datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
        )
        session_service = SessionService(SqlAlchemySessionRepository(session_factory))
        message_task: asyncio.Task[Any] | None = None
        update_task: asyncio.Task[Any] | None = None
        try:
            await _seed_user(session_factory, owner_id)
            await _seed_project(session_factory, project_id, owner_id)
            created = await session_service.create(owner_id)

            message_task = asyncio.create_task(
                message_service.append_user_message(
                    created.id,
                    (TextPart("首条消息"),),
                    owner_id=owner_id,
                    run_id=f"run_{suffix}",
                    client_message_id=f"client_{suffix}",
                )
            )
            await asyncio.wait_for(
                message_repository.session_locked.wait(),
                timeout=TASK_TIMEOUT,
            )

            update_task = asyncio.create_task(
                session_service.update(
                    owner_id,
                    created.id,
                    update_project=True,
                    project_id=project_id,
                )
            )
            await _wait_for_blocked_task(update_task, engine, application_name, "FROM sessions")

            message_repository.release_session.set()
            await asyncio.wait_for(message_task, timeout=TASK_TIMEOUT)
            with pytest.raises(SessionProjectLockedError):
                await asyncio.wait_for(update_task, timeout=TASK_TIMEOUT)

            row = await _session_row(session_factory, created.id)
            assert row.last_message_sequence == 1
            assert row.project_id is None
            assert await _conversation_count(session_factory, created.id) == 1
        finally:
            message_repository.release_session.set()
            await _cancel_tasks(*(task for task in (message_task, update_task) if task is not None))
            await engine.dispose()

    asyncio.run(scenario())


def test_session_patch_keeps_updated_at_after_blocked_message_write(
    migrated_schema: str,
) -> None:
    """消息先以较晚时间持锁提交后，等待中的标题修改不得把时间写回更早值。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    async def scenario() -> None:
        suffix = token_hex(6)
        application_name = f"athena_monotonic_{suffix}"
        engine = _engine(database_url, migrated_schema, application_name)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        owner_id = f"user_{suffix}"
        created_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        stale_update_at = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)
        later_message_at = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
        pausing_message_repository = PausingMessageRepository(session_factory)
        message_service = MessageService(
            pausing_message_repository,
            clock=lambda: later_message_at,
        )
        session_service = SessionService(
            SqlAlchemySessionRepository(session_factory),
            clock=lambda: stale_update_at,
        )
        message_task: asyncio.Task[Any] | None = None
        update_task: asyncio.Task[Any] | None = None
        try:
            await _seed_user(session_factory, owner_id)
            created = await SessionService(
                SqlAlchemySessionRepository(session_factory),
                clock=lambda: created_at,
            ).create(owner_id)

            message_task = asyncio.create_task(
                message_service.append_user_message(
                    created.id,
                    (TextPart("较晚消息"),),
                    owner_id=owner_id,
                    run_id=f"run_{suffix}",
                    client_message_id=f"client_{suffix}",
                )
            )
            await asyncio.wait_for(
                pausing_message_repository.session_locked.wait(),
                timeout=TASK_TIMEOUT,
            )

            update_task = asyncio.create_task(
                session_service.update(
                    owner_id,
                    created.id,
                    update_title=True,
                    title="较早标题修改",
                )
            )
            await _wait_for_blocked_task(update_task, engine, application_name, "FROM sessions")

            pausing_message_repository.release_session.set()
            await asyncio.wait_for(message_task, timeout=TASK_TIMEOUT)
            updated = await asyncio.wait_for(update_task, timeout=TASK_TIMEOUT)

            assert updated.updated_at == later_message_at
            row = await _session_row(session_factory, created.id)
            assert row.updated_at == later_message_at
            assert row.last_message_sequence == 1
            assert row.title == "较早标题修改"
        finally:
            pausing_message_repository.release_session.set()
            await _cancel_tasks(*(task for task in (message_task, update_task) if task is not None))
            await engine.dispose()

    asyncio.run(scenario())


def test_project_delete_and_session_create_serialize_on_project_row(
    migrated_schema: str,
) -> None:
    """覆盖创建先持共享锁与删除先持排他锁两种确定性顺序。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    async def scenario() -> None:
        suffix = token_hex(6)
        application_name = f"athena_project_{suffix}"
        engine = _engine(database_url, migrated_schema, application_name)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        owner_id = f"user_{suffix}"
        create_project_id = f"project_create_{suffix}"
        deleted_project_id = f"project_deleted_{suffix}"
        pausing_repository = PausingSessionRepository(session_factory)
        session_service = SessionService(pausing_repository)
        project_service = ProjectService(SqlAlchemyProjectRepository(session_factory))
        create_task: asyncio.Task[Any] | None = None
        delete_task: asyncio.Task[Any] | None = None
        reverse_create_task: asyncio.Task[Any] | None = None
        deleting_connection: Any = None
        deleting_transaction: Any = None
        try:
            await _seed_user(session_factory, owner_id)
            await _seed_project(session_factory, create_project_id, owner_id)

            create_task = asyncio.create_task(
                session_service.create(owner_id, project_id=create_project_id)
            )
            await asyncio.wait_for(
                pausing_repository.project_shared_locked.wait(),
                timeout=TASK_TIMEOUT,
            )

            delete_task = asyncio.create_task(project_service.delete(owner_id, create_project_id))
            await _wait_for_blocked_task(delete_task, engine, application_name, "FROM projects")

            pausing_repository.release_project.set()
            created = await asyncio.wait_for(create_task, timeout=TASK_TIMEOUT)
            with pytest.raises(ProjectHasSessionsError):
                await asyncio.wait_for(delete_task, timeout=TASK_TIMEOUT)

            created_row = await _session_row(session_factory, created.id)
            assert created_row.deleted_time is None
            assert created_row.project_id == create_project_id

            await _seed_project(session_factory, deleted_project_id, owner_id)
            deleting_connection = await engine.connect()
            deleting_transaction = await deleting_connection.begin()
            timestamp = datetime(2026, 1, 2, tzinfo=UTC)
            await deleting_connection.execute(
                text(
                    """
                    SELECT id
                    FROM projects
                    WHERE id = :project_id AND owner_id = :owner_id
                    FOR UPDATE
                    """
                ),
                {"project_id": deleted_project_id, "owner_id": owner_id},
            )
            await deleting_connection.execute(
                text(
                    """
                    UPDATE projects
                    SET deleted_time = :timestamp, updated_at = :timestamp
                    WHERE id = :project_id AND owner_id = :owner_id
                    """
                ),
                {
                    "timestamp": timestamp,
                    "project_id": deleted_project_id,
                    "owner_id": owner_id,
                },
            )

            reverse_create_task = asyncio.create_task(
                session_service.create(owner_id, project_id=deleted_project_id)
            )
            await _wait_for_blocked_task(
                reverse_create_task,
                engine,
                application_name,
                "FROM projects",
            )
            await deleting_transaction.commit()
            deleting_transaction = None
            with pytest.raises(ProjectNotFoundError):
                await asyncio.wait_for(reverse_create_task, timeout=TASK_TIMEOUT)

            async with session_factory() as session:
                active_count = await session.scalar(
                    select(func.count())
                    .select_from(SessionModel)
                    .where(
                        SessionModel.project_id == deleted_project_id,
                        SessionModel.deleted_time.is_(None),
                    )
                )
            assert int(active_count or 0) == 0
        finally:
            pausing_repository.release_project.set()
            if deleting_transaction is not None:
                await deleting_transaction.rollback()
            if deleting_connection is not None:
                await deleting_connection.close()
            await _cancel_tasks(
                *(
                    task
                    for task in (create_task, delete_task, reverse_create_task)
                    if task is not None
                )
            )
            await engine.dispose()

    asyncio.run(scenario())


def test_project_delete_and_session_rebind_serialize_on_project_row(
    migrated_schema: str,
) -> None:
    """覆盖改绑先持共享锁与删除先持排他锁两种确定性顺序。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    async def scenario() -> None:
        suffix = token_hex(6)
        application_name = f"athena_project_rebind_{suffix}"
        engine = _engine(database_url, migrated_schema, application_name)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        owner_id = f"user_{suffix}"
        source_project_id = f"project_source_{suffix}"
        shared_target_id = f"project_shared_{suffix}"
        exclusive_target_id = f"project_exclusive_{suffix}"
        pausing_repository = PausingSessionRepository(session_factory)
        session_service = SessionService(pausing_repository)
        project_service = ProjectService(SqlAlchemyProjectRepository(session_factory))
        rebind_task: asyncio.Task[Any] | None = None
        delete_task: asyncio.Task[Any] | None = None
        blocked_rebind_task: asyncio.Task[Any] | None = None
        deleting_connection: Any = None
        deleting_transaction: Any = None
        try:
            await _seed_user(session_factory, owner_id)
            await _seed_project(session_factory, source_project_id, owner_id)
            await _seed_project(session_factory, shared_target_id, owner_id)
            created = await SessionService(SqlAlchemySessionRepository(session_factory)).create(
                owner_id, project_id=source_project_id
            )

            rebind_task = asyncio.create_task(
                session_service.update(
                    owner_id,
                    created.id,
                    update_project=True,
                    project_id=shared_target_id,
                )
            )
            await asyncio.wait_for(
                pausing_repository.project_shared_locked.wait(),
                timeout=TASK_TIMEOUT,
            )

            delete_task = asyncio.create_task(project_service.delete(owner_id, shared_target_id))
            await _wait_for_blocked_task(delete_task, engine, application_name, "FROM projects")

            pausing_repository.release_project.set()
            rebound = await asyncio.wait_for(rebind_task, timeout=TASK_TIMEOUT)
            with pytest.raises(ProjectHasSessionsError):
                await asyncio.wait_for(delete_task, timeout=TASK_TIMEOUT)

            rebound_row = await _session_row(session_factory, created.id)
            assert rebound.id == created.id
            assert rebound_row.project_id == shared_target_id
            assert rebound_row.deleted_time is None
            async with session_factory() as session:
                shared_project_deleted_time = await session.scalar(
                    select(ProjectModel.deleted_time).where(
                        ProjectModel.id == shared_target_id,
                        ProjectModel.owner_id == owner_id,
                    )
                )
                active_target_count = await session.scalar(
                    select(func.count())
                    .select_from(SessionModel)
                    .where(
                        SessionModel.project_id == shared_target_id,
                        SessionModel.deleted_time.is_(None),
                    )
                )
            assert shared_project_deleted_time is None
            assert int(active_target_count or 0) == 1

            await _seed_project(session_factory, exclusive_target_id, owner_id)
            deleting_connection = await engine.connect()
            deleting_transaction = await deleting_connection.begin()
            timestamp = datetime(2026, 1, 2, tzinfo=UTC)
            await deleting_connection.execute(
                text(
                    """
                    SELECT id
                    FROM projects
                    WHERE id = :project_id AND owner_id = :owner_id
                    FOR UPDATE
                    """
                ),
                {"project_id": exclusive_target_id, "owner_id": owner_id},
            )
            await deleting_connection.execute(
                text(
                    """
                    UPDATE projects
                    SET deleted_time = :timestamp, updated_at = :timestamp
                    WHERE id = :project_id AND owner_id = :owner_id
                    """
                ),
                {
                    "timestamp": timestamp,
                    "project_id": exclusive_target_id,
                    "owner_id": owner_id,
                },
            )

            blocked_rebind_task = asyncio.create_task(
                session_service.update(
                    owner_id,
                    created.id,
                    update_project=True,
                    project_id=exclusive_target_id,
                )
            )
            await _wait_for_blocked_task(
                blocked_rebind_task,
                engine,
                application_name,
                "FROM projects",
            )
            await deleting_transaction.commit()
            deleting_transaction = None
            with pytest.raises(ProjectNotFoundError):
                await asyncio.wait_for(blocked_rebind_task, timeout=TASK_TIMEOUT)

            final_row = await _session_row(session_factory, created.id)
            assert final_row.project_id == shared_target_id
            assert final_row.deleted_time is None
            async with session_factory() as session:
                exclusive_project_deleted_time = await session.scalar(
                    select(ProjectModel.deleted_time).where(
                        ProjectModel.id == exclusive_target_id,
                        ProjectModel.owner_id == owner_id,
                    )
                )
                active_exclusive_count = await session.scalar(
                    select(func.count())
                    .select_from(SessionModel)
                    .where(
                        SessionModel.project_id == exclusive_target_id,
                        SessionModel.deleted_time.is_(None),
                    )
                )
            assert exclusive_project_deleted_time is not None
            assert int(active_exclusive_count or 0) == 0
        finally:
            pausing_repository.release_project.set()
            if deleting_transaction is not None:
                await deleting_transaction.rollback()
            if deleting_connection is not None:
                await deleting_connection.close()
            await _cancel_tasks(
                *(
                    task
                    for task in (rebind_task, delete_task, blocked_rebind_task)
                    if task is not None
                )
            )
            await engine.dispose()

    asyncio.run(scenario())


def test_session_delete_and_message_append_serialize_on_session_row(
    migrated_schema: str,
) -> None:
    """覆盖删除先提交与消息先提交两种顺序，已删除会话不得再写入消息。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    async def scenario() -> None:
        suffix = token_hex(6)
        application_name = f"athena_delete_{suffix}"
        engine = _engine(database_url, migrated_schema, application_name)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        owner_id = f"user_{suffix}"
        session_repository = SqlAlchemySessionRepository(session_factory)
        session_service = SessionService(session_repository)
        pausing_message_repository = PausingMessageRepository(session_factory)
        pausing_message_service = MessageService(
            pausing_message_repository,
            clock=lambda: datetime(2026, 1, 1, 14, 0, tzinfo=UTC),
        )
        deleting_connection: Any = None
        deleting_transaction: Any = None
        blocked_message_task: asyncio.Task[Any] | None = None
        message_first_task: asyncio.Task[Any] | None = None
        delete_task: asyncio.Task[Any] | None = None
        try:
            await _seed_user(session_factory, owner_id)

            deleted_first = await session_service.create(owner_id)
            deleting_connection = await engine.connect()
            deleting_transaction = await deleting_connection.begin()
            timestamp = datetime(2026, 1, 2, tzinfo=UTC)
            await deleting_connection.execute(
                text(
                    """
                    UPDATE sessions
                    SET deleted_time = :timestamp, updated_at = :timestamp
                    WHERE id = :session_id AND owner_id = :owner_id
                    """
                ),
                {
                    "timestamp": timestamp,
                    "session_id": deleted_first.id,
                    "owner_id": owner_id,
                },
            )
            blocked_message_task = asyncio.create_task(
                pausing_message_service.append_user_message(
                    deleted_first.id,
                    (TextPart("不应写入"),),
                    owner_id=owner_id,
                    run_id=f"run_deleted_{suffix}",
                    client_message_id=f"client_deleted_{suffix}",
                )
            )
            await _wait_for_blocked_task(
                blocked_message_task,
                engine,
                application_name,
                "FROM sessions",
            )
            await deleting_transaction.commit()
            deleting_transaction = None
            with pytest.raises(SessionNotFoundError):
                await asyncio.wait_for(blocked_message_task, timeout=TASK_TIMEOUT)

            deleted_first_row = await _session_row(session_factory, deleted_first.id)
            assert deleted_first_row.last_message_sequence == 0
            assert await _conversation_count(session_factory, deleted_first.id) == 0

            message_first = await session_service.create(owner_id)
            message_first_task = asyncio.create_task(
                pausing_message_service.append_user_message(
                    message_first.id,
                    (TextPart("先写入"),),
                    owner_id=owner_id,
                    run_id=f"run_message_{suffix}",
                    client_message_id=f"client_message_{suffix}",
                )
            )
            await asyncio.wait_for(
                pausing_message_repository.session_locked.wait(),
                timeout=TASK_TIMEOUT,
            )
            delete_task = asyncio.create_task(session_service.delete(owner_id, message_first.id))
            await _wait_for_blocked_task(delete_task, engine, application_name, "FROM sessions")

            pausing_message_repository.release_session.set()
            await asyncio.wait_for(message_first_task, timeout=TASK_TIMEOUT)
            await asyncio.wait_for(delete_task, timeout=TASK_TIMEOUT)

            with pytest.raises(SessionNotFoundError):
                await pausing_message_service.append_user_message(
                    message_first.id,
                    (TextPart("删除后不应写入"),),
                    owner_id=owner_id,
                    run_id=f"run_after_delete_{suffix}",
                    client_message_id=f"client_after_delete_{suffix}",
                )

            message_first_row = await _session_row(session_factory, message_first.id)
            assert message_first_row.last_message_sequence == 1
            assert message_first_row.deleted_time is not None
            assert await _conversation_count(session_factory, message_first.id) == 1
        finally:
            pausing_message_repository.release_session.set()
            if deleting_transaction is not None:
                await deleting_transaction.rollback()
            if deleting_connection is not None:
                await deleting_connection.close()
            await _cancel_tasks(
                *(
                    task
                    for task in (
                        blocked_message_task,
                        message_first_task,
                        delete_task,
                    )
                    if task is not None
                )
            )
            await engine.dispose()

    asyncio.run(scenario())
