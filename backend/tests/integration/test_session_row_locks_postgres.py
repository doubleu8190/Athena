"""Session 与 Project 数据库行锁的真实 PostgreSQL 并发测试。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from secrets import token_hex
from typing import Any

import pytest
from integration.postgres_harness import sync_isolated_postgres_schema
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from adapter.auth.model import AuthSessionModel, UserModel
from adapter.auth.repository import (
    SqlAlchemyAuthRepository,
    SqlAlchemyAuthTransactionGuard,
)
from adapter.llm.model import ModelProfileVersionModel
from adapter.message.model import ConversationMessageModel
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.project.model import ProjectModel
from adapter.project.repository import SqlAlchemyProjectRepository
from adapter.run.model import RunModel
from adapter.session.model import SessionModel
from adapter.session.repository import SqlAlchemySessionRepository
from app.project.service import ProjectService
from app.session.service import SessionService
from common.ids import now_utc
from domain.message import (
    ConversationMessage,
    ConversationMessageDraft,
    ConversationRole,
    MessageMetadata,
    MessageStatus,
    SessionNotFoundError,
    TextPart,
)
from domain.project import ProjectHasSessionsError, ProjectNotFoundError
from domain.system_prompt.defaults import INITIAL_SYSTEM_PROMPT_IDS
from domain.system_prompt.model import SystemPromptKind

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
LOCK_WAIT_TIMEOUT = 30.0
TASK_TIMEOUT = 30.0
_LOCK_TEST_PROFILE_ID = "profile_row_lock_test"
_LOCK_TEST_MODEL_SNAPSHOT: dict[str, object] = {
    "id": _LOCK_TEST_PROFILE_ID,
    "name": "行锁测试模型",
    "protocol": "chat_completions",
    "base_url": "https://api.example.com/v1",
    "model": "gpt-4o-mini",
    "context_window_tokens": 128000,
    "tokenizer_encoding": "cl100k_base",
    "supports_stream": True,
    "supports_tools": True,
    "supports_parallel_tool_calls": False,
    "max_output_tokens": 2048,
    "request_timeout_seconds": 30,
    "execution_timeout_seconds": 120,
    "max_concurrency": 4,
    "temperature": None,
    "scenario_label": None,
    "supports_vision": False,
    "version": 1,
}
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

    async def append_user_message_with_completed_run(
        self,
        session_id: str,
        text_content: str,
        *,
        owner_id: str,
        run_id: str,
        client_message_id: str,
        created_at: datetime,
    ) -> ConversationMessage:
        """在 Session 行锁内原子写入用户消息及关联的已完成 Run，模拟真实写入事务。"""
        message_id = f"msg_{run_id}"
        draft = ConversationMessageDraft(
            id=message_id,
            session_id=session_id,
            owner_id=owner_id,
            role=ConversationRole.USER,
            status=MessageStatus.COMPLETED,
            parts=(TextPart(text_content),),
            reply_to_message_id=None,
            run_id=run_id,
            context_turn_id=message_id,
            client_message_id=client_message_id,
            created_at=created_at,
            completed_at=created_at,
            metadata=MessageMetadata(),
        )
        async with self._session_factory() as session:
            async with session.begin():
                locked_session = await session.scalar(
                    select(SessionModel)
                    .where(
                        SessionModel.id == session_id,
                        SessionModel.owner_id == owner_id,
                        SessionModel.deleted_time.is_(None),
                    )
                    .with_for_update()
                )
                if locked_session is None:
                    raise SessionNotFoundError("session_not_found")
                message = await self.append_conversation_in_session(
                    session,
                    draft,
                    locked_session=locked_session,
                )
                session.add(
                    RunModel(
                        id=run_id,
                        session_id=session_id,
                        owner_id=owner_id,
                        status="completed",
                        trigger_message_id=message.id,
                        context_cutoff_sequence=message.sequence,
                        model_config_version_id=_LOCK_TEST_PROFILE_ID,
                        model_snapshot_json=dict(_LOCK_TEST_MODEL_SNAPSHOT),
                        main_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS[SystemPromptKind.MAIN],
                        compression_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS[
                            SystemPromptKind.COMPRESSION
                        ],
                        prompt_template_version="run-v2",
                        event_sequence=0,
                        execution_epoch=0,
                        queued_at=created_at,
                        created_at=created_at,
                        updated_at=created_at,
                        finished_at=created_at,
                        usage={},
                        depth=0,
                    )
                )
                await session.flush()
                return message


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

    with sync_isolated_postgres_schema(database_url, prefix="athena_v2_row_locks") as schema:
        yield schema


def _engine(database_url: str, schema: str, application_name: str) -> AsyncEngine:
    """创建带独立 application_name 的临时 schema 连接引擎。"""
    return create_async_engine(
        database_url,
        poolclass=NullPool,
        connect_args={
            "server_settings": {
                "search_path": f"{schema},public",
                "application_name": application_name,
            }
        },
    )


async def _ensure_model_profile(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """幂等预置共享模型配置，避免并发写入 Run 外键时重复插入。"""
    timestamp = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        async with session.begin():
            if await session.get(ModelProfileVersionModel, _LOCK_TEST_PROFILE_ID) is None:
                session.add(
                    ModelProfileVersionModel(
                        id=_LOCK_TEST_PROFILE_ID,
                        name="行锁测试模型",
                        protocol="chat_completions",
                        base_url="https://api.example.com/v1",
                        model="gpt-4o-mini",
                        api_key_ciphertext=b"ciphertext",
                        api_key_nonce=b"0123456789ab",
                        api_key_key_version=1,
                        context_window_tokens=128000,
                        tokenizer_encoding="cl100k_base",
                        supports_stream=True,
                        supports_tools=True,
                        supports_parallel_tool_calls=False,
                        max_output_tokens=2048,
                        request_timeout_seconds=30,
                        execution_timeout_seconds=120,
                        max_concurrency=4,
                        temperature=None,
                        enabled=True,
                        visible_to_users=True,
                        supports_vision=False,
                        version=1,
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
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


async def _seed_auth_session(
    session_factory: async_sessionmaker[AsyncSession],
    session_id: str,
    user_id: str,
    token_hash: str,
    *,
    expires_at: datetime | None = None,
) -> None:
    """创建仍有效的认证会话供管理员复核并发测试使用。"""
    timestamp = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        async with session.begin():
            session.add(
                AuthSessionModel(
                    id=session_id,
                    user_id=user_id,
                    token_hash=token_hash,
                    expires_at=expires_at or datetime(2027, 1, 1, tzinfo=UTC),
                    revoked_time=None,
                    created_at=timestamp,
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
    """首条消息先锁 Session 时，改绑必须等待其释放行锁；无活跃 Run 时随后可成功改绑。"""
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
        session_service = SessionService(SqlAlchemySessionRepository(session_factory))
        message_task: asyncio.Task[Any] | None = None
        update_task: asyncio.Task[Any] | None = None
        try:
            await _ensure_model_profile(session_factory)
            await _seed_user(session_factory, owner_id)
            await _seed_project(session_factory, project_id, owner_id)
            created = await session_service.create(owner_id)

            message_task = asyncio.create_task(
                message_repository.append_user_message_with_completed_run(
                    created.id,
                    "首条消息",
                    owner_id=owner_id,
                    run_id=f"run_{suffix}",
                    client_message_id=f"client_{suffix}",
                    created_at=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
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
            rebound = await asyncio.wait_for(update_task, timeout=TASK_TIMEOUT)
            assert rebound.project_id == project_id

            row = await _session_row(session_factory, created.id)
            assert row.last_message_sequence == 1
            assert row.project_id == project_id
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
        session_service = SessionService(
            SqlAlchemySessionRepository(session_factory),
            clock=lambda: stale_update_at,
        )
        message_task: asyncio.Task[Any] | None = None
        update_task: asyncio.Task[Any] | None = None
        try:
            await _ensure_model_profile(session_factory)
            await _seed_user(session_factory, owner_id)
            created = await SessionService(
                SqlAlchemySessionRepository(session_factory),
                clock=lambda: created_at,
            ).create(owner_id)

            message_task = asyncio.create_task(
                pausing_message_repository.append_user_message_with_completed_run(
                    created.id,
                    "较晚消息",
                    owner_id=owner_id,
                    run_id=f"run_{suffix}",
                    client_message_id=f"client_{suffix}",
                    created_at=later_message_at,
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
        message_created_at = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
        deleting_connection: Any = None
        deleting_transaction: Any = None
        blocked_message_task: asyncio.Task[Any] | None = None
        message_first_task: asyncio.Task[Any] | None = None
        delete_task: asyncio.Task[Any] | None = None
        try:
            await _ensure_model_profile(session_factory)
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
                pausing_message_repository.append_user_message_with_completed_run(
                    deleted_first.id,
                    "不应写入",
                    owner_id=owner_id,
                    run_id=f"run_deleted_{suffix}",
                    client_message_id=f"client_deleted_{suffix}",
                    created_at=message_created_at,
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
                pausing_message_repository.append_user_message_with_completed_run(
                    message_first.id,
                    "先写入",
                    owner_id=owner_id,
                    run_id=f"run_message_{suffix}",
                    client_message_id=f"client_message_{suffix}",
                    created_at=message_created_at,
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
                await pausing_message_repository.append_user_message_with_completed_run(
                    message_first.id,
                    "删除后不应写入",
                    owner_id=owner_id,
                    run_id=f"run_after_delete_{suffix}",
                    client_message_id=f"client_after_delete_{suffix}",
                    created_at=message_created_at,
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


def test_admin_guard_serializes_with_session_revocation_and_role_changes(
    migrated_schema: str,
) -> None:
    """最终管理员复核与撤销、角色修改按 user/session 行锁线性化。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    async def scenario() -> None:
        suffix = token_hex(6)
        holder_name = f"athena_guard_holder_{suffix}"
        worker_name = f"athena_guard_worker_{suffix}"
        monitor_engine = _engine(database_url, migrated_schema, f"athena_guard_monitor_{suffix}")
        holder_engine = _engine(database_url, migrated_schema, holder_name)
        worker_engine = _engine(database_url, migrated_schema, worker_name)
        holder_factory = async_sessionmaker(holder_engine, expire_on_commit=False)
        worker_factory = async_sessionmaker(worker_engine, expire_on_commit=False)
        auth_repository = SqlAlchemyAuthRepository(worker_factory)
        now = datetime(2026, 1, 2, tzinfo=UTC)
        revoke_guard_user = f"user_guard_revoke_{suffix}"
        revoke_guard_session = f"ses_guard_revoke_{suffix}"
        revoke_first_user = f"user_revoke_first_{suffix}"
        revoke_first_session = f"ses_revoke_first_{suffix}"
        role_guard_user = f"user_guard_role_{suffix}"
        role_guard_session = f"ses_guard_role_{suffix}"
        role_first_user = f"user_role_first_{suffix}"
        role_first_session = f"ses_role_first_{suffix}"
        guard_acquired = asyncio.Event()
        release_guard = asyncio.Event()
        holder_task: asyncio.Task[Any] | None = None
        operation_task: asyncio.Task[Any] | None = None
        expiry_task: asyncio.Task[Any] | None = None
        observed: dict[str, Any] = {}

        async def hold_guard(user_id: str, session_id: str, label: str) -> None:
            async with holder_factory() as database_session:
                async with database_session.begin():
                    user = await SqlAlchemyAuthTransactionGuard(
                        database_session
                    ).lock_current_session_user(user_id, session_id)
                    observed[label] = user
                    guard_acquired.set()
                    if user is not None:
                        await database_session.execute(
                            text("UPDATE users SET updated_at = :updated_at WHERE id = :user_id"),
                            {
                                "updated_at": datetime(2026, 1, 2, 0, 1, tzinfo=UTC),
                                "user_id": user_id,
                            },
                        )
                    await release_guard.wait()

        try:
            for user_id in (
                revoke_guard_user,
                revoke_first_user,
                role_guard_user,
                role_first_user,
            ):
                await _seed_user(holder_factory, user_id)
            for user_id, session_id in (
                (revoke_guard_user, revoke_guard_session),
                (revoke_first_user, revoke_first_session),
                (role_guard_user, role_guard_session),
                (role_first_user, role_first_session),
            ):
                token_hash = (session_id.encode().hex() * 64)[:64]
                await _seed_auth_session(holder_factory, session_id, user_id, token_hash)
            async with holder_factory() as database_session:
                async with database_session.begin():
                    await database_session.execute(
                        text("UPDATE users SET role = 'super_admin' WHERE id IN (:first, :second)"),
                        {"first": role_guard_user, "second": role_first_user},
                    )

            # 复核先持有 user、session 锁；撤销必须等待复核事务提交。
            holder_task = asyncio.create_task(
                hold_guard(revoke_guard_user, revoke_guard_session, "revoke_guard")
            )
            await asyncio.wait_for(guard_acquired.wait(), timeout=TASK_TIMEOUT)
            operation_task = asyncio.create_task(
                auth_repository.revoke_session(
                    (revoke_guard_session.encode().hex() * 64)[:64],
                    now,
                )
            )
            await _wait_for_blocked_task(
                operation_task,
                monitor_engine,
                worker_name,
                "UPDATE auth_sessions",
            )
            release_guard.set()
            await asyncio.wait_for(holder_task, timeout=TASK_TIMEOUT)
            assert observed["revoke_guard"].role.value == "member"
            assert await asyncio.wait_for(operation_task, timeout=TASK_TIMEOUT) is True

            # 撤销先提交时，后续事务 guard 不得返回用户。
            assert await auth_repository.revoke_session(
                (revoke_first_session.encode().hex() * 64)[:64],
                now,
            )
            async with holder_factory() as database_session:
                async with database_session.begin():
                    assert (
                        await SqlAlchemyAuthTransactionGuard(
                            database_session
                        ).lock_current_session_user(revoke_first_user, revoke_first_session)
                        is None
                    )

            # 复核先锁用户时，角色降级等待；最终写事务先提交，降级随后生效。
            guard_acquired.clear()
            release_guard.clear()
            holder_task = asyncio.create_task(
                hold_guard(role_guard_user, role_guard_session, "role_guard")
            )
            await asyncio.wait_for(guard_acquired.wait(), timeout=TASK_TIMEOUT)

            async def demote_role(user_id: str) -> None:
                async with worker_factory() as database_session:
                    async with database_session.begin():
                        await database_session.execute(
                            text("UPDATE users SET role = 'member' WHERE id = :user_id"),
                            {"user_id": user_id},
                        )

            operation_task = asyncio.create_task(demote_role(role_guard_user))
            await _wait_for_blocked_task(
                operation_task,
                monitor_engine,
                worker_name,
                "UPDATE users",
            )
            release_guard.set()
            await asyncio.wait_for(holder_task, timeout=TASK_TIMEOUT)
            assert observed["role_guard"].role.value == "super_admin"
            await asyncio.wait_for(operation_task, timeout=TASK_TIMEOUT)

            # 角色变更先提交时，guard 读取已变更的普通用户角色。
            await demote_role(role_first_user)
            async with holder_factory() as database_session:
                async with database_session.begin():
                    demoted = await SqlAlchemyAuthTransactionGuard(
                        database_session
                    ).lock_current_session_user(role_first_user, role_first_session)
                    assert demoted is not None and demoted.role.value == "member"

            # 会话在 guard 等待 user 行锁期间过期，锁后 clock 必须使最终复核拒绝。
            expiring_user = f"user_expiring_guard_{suffix}"
            expiring_session = f"ses_expiring_guard_{suffix}"
            await _seed_user(holder_factory, expiring_user)
            await _seed_auth_session(
                holder_factory,
                expiring_session,
                expiring_user,
                (expiring_session.encode().hex() * 64)[:64],
            )
            expiry_result: dict[str, Any] = {}

            async def guard_after_wait() -> None:
                async with worker_factory() as database_session:
                    async with database_session.begin():
                        expiry_result["user"] = await SqlAlchemyAuthTransactionGuard(
                            database_session,
                            clock=now_utc,
                        ).lock_current_session_user(expiring_user, expiring_session)

            async with holder_factory() as blocking_session:
                async with blocking_session.begin():
                    await blocking_session.execute(
                        text("UPDATE users SET updated_at = :updated_at WHERE id = :user_id"),
                        {"updated_at": now, "user_id": expiring_user},
                    )
                    expiry_task = asyncio.create_task(guard_after_wait())
                    await _wait_for_blocked_task(
                        expiry_task,
                        monitor_engine,
                        worker_name,
                        "FROM users",
                    )
                    # 先确认 guard 已等待 user，再设定到期时刻；准备阶段不消耗有效窗口。
                    expiration = now_utc() + timedelta(seconds=2)
                    await blocking_session.execute(
                        text(
                            "UPDATE auth_sessions SET expires_at = :expires_at "
                            "WHERE id = :session_id AND user_id = :user_id"
                        ),
                        {
                            "expires_at": expiration,
                            "session_id": expiring_session,
                            "user_id": expiring_user,
                        },
                    )
                    assert now_utc() < expiration
                    remaining = (expiration - now_utc()).total_seconds()
                    await asyncio.sleep(max(0.0, remaining) + 0.1)
                    assert now_utc() > expiration
            await asyncio.wait_for(expiry_task, timeout=TASK_TIMEOUT)
            assert expiry_result["user"] is None
        finally:
            release_guard.set()
            await _cancel_tasks(
                *(task for task in (holder_task, operation_task, expiry_task) if task is not None)
            )
            await holder_engine.dispose()
            await worker_engine.dispose()
            await monitor_engine.dispose()

    asyncio.run(scenario())
