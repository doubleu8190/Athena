"""Run 仓储结构与命名重构契约单元测试。"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import DateTime, event, func, literal
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from adapter.auth.model import UserModel
from adapter.message.repository.message import SqlAlchemyMessageRepository
from adapter.models import Base
from adapter.run.model import RunModel
from adapter.run.repository import _shared, context_events, lifecycle, tool_calls
from adapter.run.repository.lifecycle import RunLifecycleMixin
from adapter.run.repository.run import SqlAlchemyRunRepository
from adapter.run.repository.tool_calls import RunToolCallsMixin
from adapter.session.model import SessionModel
from adapter.skill.model import SkillExecutionModel
from domain.auth import UserRole, UserStatus
from domain.file import PendingSessionFile
from domain.run.model import RunStatus, RunUsage
from domain.run.ports import RunRepository
from domain.skill import (
    SkillExecutionArtifact,
    SkillExecutionCompletion,
    SkillExecutionStatus,
)
from domain.tool import (
    InitializeRunToolsCommand,
    ReserveLlmRequestCommand,
    RuntimeToolSnapshot,
)


def test_settle_cancelled_contract_and_patch_removed() -> None:
    """验证 settle_cancelled 形参严格对齐 domain 协议且 RunRepository 删除了打补丁覆盖。"""
    # 1. 确保 SqlAlchemyRunRepository 本身没有重写 settle_cancelled
    assert "settle_cancelled" not in SqlAlchemyRunRepository.__dict__

    # 2. 检查形参对齐情况
    sig = inspect.signature(RunLifecycleMixin.settle_cancelled)
    params = list(sig.parameters.keys())
    assert "self" in params
    assert "run_id" in params
    assert "worker_id" in params
    assert "execution_epoch" in params
    assert "settled_at" in params
    assert "cancelled_at" not in params

    # 3. 严格契约类型验证
    domain_sig = inspect.signature(RunRepository.settle_cancelled)
    for p in ("run_id", "worker_id", "execution_epoch", "settled_at"):
        assert sig.parameters[p].name == domain_sig.parameters[p].name


def test_message_repository_property_and_attribute() -> None:
    """验证仓储内部持有的 _message_repo 命名及 _messages 兼容属性。"""
    mock_session_factory = MagicMock()
    mock_message_repo = MagicMock()

    repo = SqlAlchemyRunRepository(
        mock_session_factory,
        mock_message_repo,
    )
    # 验证主属性与兼容属性均指向传入的消息仓储实例
    assert repo._message_repo is mock_message_repo
    assert repo._messages is mock_message_repo


def test_shared_and_lifecycle_method_signatures() -> None:
    """验证生命周期及共享辅助方法的形参和命名。"""
    # _lock_default_profile_id 存在，旧命名不存在
    assert hasattr(SqlAlchemyRunRepository, "_lock_default_profile_id")
    assert not hasattr(SqlAlchemyRunRepository, "_lock_default_profile")

    # _validate_and_merge_input_file_ids 存在，旧命名不存在
    assert hasattr(SqlAlchemyRunRepository, "_validate_and_merge_input_file_ids")
    assert not hasattr(SqlAlchemyRunRepository, "_validated_input_file_ids")

    # _lock_worker_run 形参规范化
    lock_sig = inspect.signature(SqlAlchemyRunRepository._lock_worker_run)
    assert "execution_epoch" in lock_sig.parameters
    assert "epoch" not in lock_sig.parameters

    # _lease_is_valid 形参规范化
    lease_sig = inspect.signature(SqlAlchemyRunRepository._lease_is_valid)
    assert "execution_epoch" in lease_sig.parameters
    assert "epoch" not in lease_sig.parameters

    # _try_wake_parent_run 形参规范化
    wake_sig = inspect.signature(SqlAlchemyRunRepository._try_wake_parent_run)
    assert "sub_run" in wake_sig.parameters
    assert "sub_row" not in wake_sig.parameters


def test_tool_calls_structure_and_dead_constants() -> None:
    """验证 tool_calls 方法命名、双层事务重构、静态方法挂载及死常量清理。"""
    # 验证双层事务消除套娃：finish_tool_call 直接接入 _finish_tool_call_in_session
    assert hasattr(RunToolCallsMixin, "finish_tool_call")
    assert hasattr(RunToolCallsMixin, "_finish_tool_call_in_session")
    assert not hasattr(RunToolCallsMixin, "_finish_tool_call_transaction")
    assert not hasattr(RunToolCallsMixin, "_finish_tool_call_transaction_inner")

    # 验证产物校验方法动词原形
    assert hasattr(RunToolCallsMixin, "_validate_pending_artifacts")
    assert not hasattr(RunToolCallsMixin, "_validated_pending_artifacts")

    # 验证 _skill_execution_row_matches 挂载为静态方法
    assert hasattr(RunToolCallsMixin, "_skill_execution_row_matches")
    assert isinstance(
        inspect.getattr_static(RunToolCallsMixin, "_skill_execution_row_matches"),
        staticmethod,
    )
    assert not hasattr(_shared, "_skill_execution_row_matches")

    # 验证常量清理
    assert not hasattr(tool_calls, "_ACTIVE_STATUSES")
    assert not hasattr(tool_calls, "_STREAM_SNAPSHOT_MESSAGES")
    assert not hasattr(tool_calls, "_STREAM_SNAPSHOT_EVENTS")
    assert not hasattr(tool_calls, "_STREAM_SNAPSHOT_BYTES")
    assert not hasattr(tool_calls, "_STREAM_SNAPSHOT_PREFIX_RESERVE")
    assert hasattr(tool_calls, "_MAX_TOOL_BATCH_SIZE")

    assert not hasattr(_shared, "_STREAM_SNAPSHOT_MESSAGES")
    assert not hasattr(_shared, "_STREAM_SNAPSHOT_EVENTS")
    assert not hasattr(_shared, "_STREAM_SNAPSHOT_BYTES")
    assert not hasattr(_shared, "_STREAM_SNAPSHOT_PREFIX_RESERVE")
    assert not hasattr(_shared, "_MAX_TOOL_BATCH_SIZE")
    assert not hasattr(_shared, "_ACTIVE_STATUSES")


def test_context_events_dead_constants() -> None:
    """验证 context_events 中死常量清理。"""
    assert not hasattr(context_events, "_ACTIVE_STATUSES")
    assert not hasattr(context_events, "_MAX_TOOL_BATCH_SIZE")
    assert hasattr(context_events, "_STREAM_SNAPSHOT_MESSAGES")


def test_skill_execution_row_matches_logic() -> None:
    """验证 RunToolCallsMixin._skill_execution_row_matches 静态方法的比较逻辑。"""
    now = datetime.now(UTC)
    sha256_val = "a" * 64
    completion = SkillExecutionCompletion(
        execution_id="exec-1",
        status=SkillExecutionStatus.SUCCEEDED,
        log_object_key="logs/1.txt",
        log_truncated=False,
        log_error_code=None,
        error_code=None,
        cleanup_error_code=None,
        finished_at=now,
    )
    artifact = PendingSessionFile(
        id="file-1",
        filename="report.pdf",
        label="Report",
        mime_type="application/pdf",
        size_bytes=1024,
        sha256=sha256_val,
        object_key="files/report.pdf",
        output_index=0,
        created_at=now,
    )
    expected_artifact_json = [
        SkillExecutionArtifact(
            file_id="file-1",
            filename="report.pdf",
            label="Report",
            mime_type="application/pdf",
            size_bytes=1024,
            sha256=sha256_val,
            object_key="files/report.pdf",
            output_index=0,
        ).model_dump(mode="json")
    ]

    matching_row = SkillExecutionModel(
        id="exec-1",
        session_id="session-1",
        run_id="run-1",
        tool_call_record_id="tc-1",
        skill_id="skill-1",
        skill_version_id="ver-1",
        script_path="main.py",
        input_file_ids=[],
        parameters_sha256="b" * 64,
        python_version="3.12",
        dependency_check=True,
        status=SkillExecutionStatus.SUCCEEDED.value,
        log_object_key="logs/1.txt",
        log_truncated=False,
        log_error_code=None,
        error_code=None,
        cleanup_error_code=None,
        result_artifacts=expected_artifact_json,
        created_at=now,
        finished_at=now,
    )

    # 匹配测试
    assert RunToolCallsMixin._skill_execution_row_matches(matching_row, completion, (artifact,))

    # 状态不匹配
    mismatched_status_row = SkillExecutionModel(
        id="exec-1",
        session_id="session-1",
        run_id="run-1",
        tool_call_record_id="tc-1",
        skill_id="skill-1",
        skill_version_id="ver-1",
        script_path="main.py",
        input_file_ids=[],
        parameters_sha256="b" * 64,
        python_version="3.12",
        dependency_check=True,
        status=SkillExecutionStatus.FAILED.value,
        log_object_key="logs/1.txt",
        log_truncated=False,
        log_error_code=None,
        error_code=None,
        cleanup_error_code=None,
        result_artifacts=expected_artifact_json,
        created_at=now,
        finished_at=now,
    )
    assert not RunToolCallsMixin._skill_execution_row_matches(
        mismatched_status_row,
        completion,
        (artifact,),
    )

    # 产物不匹配
    assert not RunToolCallsMixin._skill_execution_row_matches(matching_row, completion, ())


def test_run_micro_ops_do_not_touch_or_lock_parent_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """heartbeat / initialize_tools / reserve_llm_request 仅锁 runs 行，绝不触碰 sessions。"""
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(
        func,
        "clock_timestamp",
        lambda: literal(now, type_=DateTime(timezone=True)),
    )
    # 同步确认 lifecycle 重复辅助与死常量均已收口。
    assert not hasattr(lifecycle, "_STREAM_SNAPSHOT_MESSAGES")
    assert not hasattr(lifecycle, "_MAX_TOOL_BATCH_SIZE")
    assert "_refresh_safe_skill_projection_in_session" not in RunLifecycleMixin.__dict__
    assert "_extract_subagent_task_summary" not in RunLifecycleMixin.__dict__
    assert "_load_subagent_details_in_session" not in RunLifecycleMixin.__dict__
    assert "list_visible_events" not in RunLifecycleMixin.__dict__

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        executed_sql: list[str] = []

        def _capture_sql(
            _conn: Any,
            _cursor: Any,
            statement: str,
            _parameters: Any,
            _context: Any,
            _executemany: bool,
        ) -> None:
            executed_sql.append(statement.lower())

        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            repo = SqlAlchemyRunRepository(factory, SqlAlchemyMessageRepository(factory))
            async with factory() as session, session.begin():
                session.add_all(
                    [
                        UserModel(
                            id="usr_lock",
                            email="lock@example.com",
                            password_hash="hash",
                            display_name="Lock",
                            role=UserRole.MEMBER.value,
                            status=UserStatus.ACTIVE.value,
                            created_at=now,
                            updated_at=now,
                        ),
                        SessionModel(
                            id="sess_lock",
                            owner_id="usr_lock",
                            title="Lock",
                            project_id=None,
                            last_message_sequence=1,
                            created_at=now,
                            updated_at=now,
                            deleted_time=None,
                        ),
                        RunModel(
                            id="run_lock",
                            session_id="sess_lock",
                            owner_id="usr_lock",
                            status=RunStatus.RUNNING.value,
                            trigger_message_id="msg_1",
                            context_cutoff_sequence=1,
                            retry_of_run_id=None,
                            model_config_version_id="prof_1",
                            main_prompt_version_id="main_1",
                            compression_prompt_version_id="comp_1",
                            prompt_template_version="v1",
                            event_sequence=1,
                            execution_epoch=1,
                            worker_id="worker-1",
                            lease_expires_at=now + timedelta(seconds=60),
                            execution_started_at=now,
                            execution_deadline_at=now + timedelta(seconds=300),
                            llm_request_count=0,
                            queued_at=now,
                            created_at=now,
                            updated_at=now,
                            usage=RunUsage().model_dump(mode="json"),
                        ),
                    ]
                )

            event.listen(engine.sync_engine, "before_cursor_execute", _capture_sql)
            try:
                executed_sql.clear()
                hb = await repo.heartbeat("run_lock", "worker-1", 1, lease_seconds=90)
                assert hb is not None
                assert not any("sessions" in sql for sql in executed_sql)

                executed_sql.clear()
                snap = await repo.initialize_tools(
                    InitializeRunToolsCommand(
                        run_id="run_lock",
                        worker_id="worker-1",
                        execution_epoch=1,
                        snapshot=RuntimeToolSnapshot(tools=()),
                    )
                )
                assert snap == RuntimeToolSnapshot(tools=())
                assert not any("sessions" in sql for sql in executed_sql)

                executed_sql.clear()
                seq = await repo.reserve_llm_request(
                    ReserveLlmRequestCommand(
                        run_id="run_lock",
                        worker_id="worker-1",
                        execution_epoch=1,
                        maximum_count=10,
                    )
                )
                assert seq == 1
                assert not any("sessions" in sql for sql in executed_sql)
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", _capture_sql)
        finally:
            await engine.dispose()

    asyncio.run(scenario())
