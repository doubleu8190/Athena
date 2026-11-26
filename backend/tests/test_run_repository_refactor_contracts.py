"""Run 仓储结构与命名重构契约单元测试。"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from pg_unit_harness import create_pg_unit_db
from sqlalchemy import DateTime, event, func, literal

from adapter.auth.model import UserModel
from adapter.llm.model import ModelProfileVersionModel
from adapter.message.model import ConversationMessageModel, ExecutionMessageModel
from adapter.message.repository.message import SqlAlchemyMessageRepository
from adapter.run.model import RunModel, RunToolCallModel
from adapter.run.repository import _shared, context_events, lifecycle, tool_calls
from adapter.run.repository.lifecycle import RunLifecycleMixin
from adapter.run.repository.run import SqlAlchemyRunRepository
from adapter.run.repository.tool_calls import RunToolCallsMixin
from adapter.session.model import SessionModel
from adapter.system_prompt.model import SystemPromptVersionModel
from domain.auth import UserRole, UserStatus
from domain.run.model import RecordRunUsageCommand, RunStatus, RunUsage
from domain.run.ports import RunRepository
from domain.tool import (
    AdmitToolCallCommand,
    InitializeRunToolsCommand,
    RuntimeToolSnapshot,
    ToolCallStatus,
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
    assert "llm_request_count" in params
    assert "cancelled_at" not in params

    # 3. 严格契约类型验证
    domain_sig = inspect.signature(RunRepository.settle_cancelled)
    for p in ("run_id", "worker_id", "execution_epoch", "settled_at", "llm_request_count"):
        assert sig.parameters[p].name == domain_sig.parameters[p].name
        assert sig.parameters[p].kind == domain_sig.parameters[p].kind
        assert sig.parameters[p].default == domain_sig.parameters[p].default


def test_message_repository_property_and_attribute() -> None:
    """验证仓储内部统一持有 _message_repo，已移除旧过渡兼容属性 _messages。"""
    mock_session_factory = MagicMock()
    mock_message_repo = MagicMock()

    repo = SqlAlchemyRunRepository(
        mock_session_factory,
        mock_message_repo,
    )
    assert repo._message_repo is mock_message_repo
    assert not hasattr(repo, "_messages")


def test_shared_and_lifecycle_method_signatures() -> None:
    """验证生命周期及共享辅助方法的形参和命名。"""
    # 模型配置已改为无锁读取与统一可用性校验，3 个旧 *_id 死封装已移除
    assert hasattr(SqlAlchemyRunRepository, "_load_default_profile_row")
    assert hasattr(SqlAlchemyRunRepository, "_validate_usable_profile")
    assert hasattr(SqlAlchemyRunRepository, "_database_now")
    assert not hasattr(SqlAlchemyRunRepository, "_lock_default_profile")
    assert not hasattr(SqlAlchemyRunRepository, "_lock_default_profile_id")
    assert not hasattr(SqlAlchemyRunRepository, "_lock_selected_profile_id")
    assert not hasattr(SqlAlchemyRunRepository, "_lock_retry_profile_id")
    assert not hasattr(SqlAlchemyRunRepository, "_next_run_created_at")
    assert not hasattr(SqlAlchemyRunRepository, "reserve_llm_request")

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
    """验证 tool_calls 方法命名、双层事务重构、深比对重放清理及死常量清理。"""
    # 验证双层事务消除套娃：finish_tool_call 直接单层落库，无多余私有包装方法
    assert hasattr(RunToolCallsMixin, "finish_tool_call")
    assert not hasattr(RunToolCallsMixin, "_finish_tool_call_in_session")
    assert not hasattr(RunToolCallsMixin, "_finish_tool_call_transaction")
    assert not hasattr(RunToolCallsMixin, "_finish_tool_call_transaction_inner")

    # 验证产物校验方法动词原形
    assert hasattr(RunToolCallsMixin, "_validate_pending_artifacts")
    assert not hasattr(RunToolCallsMixin, "_validated_pending_artifacts")

    # 验证已删除深比对重放、converge_tool_batch 与死查询 list_retry_context
    assert not hasattr(RunToolCallsMixin, "_skill_execution_row_matches")
    assert not hasattr(RunToolCallsMixin, "_skill_completion_matches")
    assert not hasattr(RunToolCallsMixin, "_delivery_message_matches")
    assert not hasattr(RunToolCallsMixin, "_artifact_rows_match")
    assert not hasattr(RunToolCallsMixin, "converge_tool_batch")
    assert not hasattr(RunToolCallsMixin, "list_retry_context")
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


def test_run_micro_ops_do_not_touch_or_lock_parent_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """heartbeat / initialize_tools / record_usage / admit_tool_call 仅锁 runs，不触碰 sessions。"""
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
        engine, factory = await create_pg_unit_db()
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
            repo = SqlAlchemyRunRepository(factory, SqlAlchemyMessageRepository(factory))
            # 按真实 PostgreSQL 外键依赖顺序落库用户、模型、系统提示词、会话、消息、Run 与工具调用。
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
                        ModelProfileVersionModel(
                            id="prof_1",
                            name="Lock Model",
                            protocol="chat_completions",
                            base_url="https://llm.example.test/v1",
                            model="gpt-test",
                            api_key_ciphertext=b"ciphertext",
                            api_key_nonce=b"0123456789ab",
                            api_key_key_version=1,
                            context_window_tokens=128000,
                            tokenizer_encoding="cl100k_base",
                            max_output_tokens=4096,
                            request_timeout_seconds=30,
                            execution_timeout_seconds=120,
                            max_concurrency=4,
                            created_at=now,
                            updated_at=now,
                        ),
                        SystemPromptVersionModel(
                            id="main_1",
                            kind="main",
                            content="main prompt",
                            created_at=now,
                        ),
                        SystemPromptVersionModel(
                            id="comp_1",
                            kind="compression",
                            content="compression prompt",
                            created_at=now,
                        ),
                    ]
                )
                await session.flush()
                session.add(
                    SessionModel(
                        id="sess_lock",
                        owner_id="usr_lock",
                        title="Lock",
                        project_id=None,
                        last_message_sequence=2,
                        created_at=now,
                        updated_at=now,
                        deleted_time=None,
                    )
                )
                await session.flush()
                session.add_all(
                    [
                        ConversationMessageModel(
                            id="msg_1",
                            session_id="sess_lock",
                            role="user",
                            status="completed",
                            sequence=1,
                            parts=[{"type": "text", "text": "hello"}],
                            run_id="run_lock",
                            context_turn_id="msg_1",
                            created_at=now,
                            completed_at=now,
                            metadata_json={},
                        ),
                        ExecutionMessageModel(
                            id="exec_call_1",
                            session_id="sess_lock",
                            run_id="run_lock",
                            thread_id="root:run_lock",
                            role="assistant",
                            sequence=2,
                            parts=[
                                {
                                    "type": "tool_call",
                                    "tool_call_id": "call_lock_1",
                                    "name": "demo",
                                    "arguments": {},
                                }
                            ],
                            created_at=now,
                            metadata_json={},
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
                            model_snapshot_json={
                                "id": "prof_1",
                                "name": "Lock Model",
                                "protocol": "chat_completions",
                                "base_url": "https://llm.example.test/v1",
                                "model": "gpt-test",
                                "context_window_tokens": 128000,
                                "tokenizer_encoding": "cl100k_base",
                                "supports_stream": True,
                                "supports_tools": True,
                                "supports_parallel_tool_calls": False,
                                "max_output_tokens": 4096,
                                "request_timeout_seconds": 30,
                                "execution_timeout_seconds": 120,
                                "max_concurrency": 4,
                                "temperature": None,
                                "scenario_label": None,
                                "supports_vision": False,
                                "version": 1,
                            },
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
                await session.flush()
                session.add(
                    RunToolCallModel(
                        id="tc_lock_1",
                        run_id="run_lock",
                        session_id="sess_lock",
                        tool_call_id="call_lock_1",
                        ordinal=1,
                        tool_id="builtin_demo",
                        tool_kind="builtin",
                        model_name="demo",
                        execution_epoch=1,
                        round=1,
                        status=ToolCallStatus.APPROVED.value,
                        call_message_id="exec_call_1",
                        requires_approval=False,
                        requested_at=now,
                    )
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
                recorded = await repo.record_usage(
                    RecordRunUsageCommand(
                        run_id="run_lock",
                        worker_id="worker-1",
                        execution_epoch=1,
                        attempt=1,
                        input_tokens=10,
                        output_tokens=5,
                        total_tokens=15,
                        complete=True,
                        recorded_at=now,
                        llm_request_count=3,
                    )
                )
                assert recorded is not None
                assert recorded.llm_request_count == 3
                assert recorded.usage.total_tokens == 15
                assert not any("sessions" in sql for sql in executed_sql)

                executed_sql.clear()
                admitted = await repo.admit_tool_call(
                    AdmitToolCallCommand(
                        run_id="run_lock",
                        worker_id="worker-1",
                        execution_epoch=1,
                        record_id="tc_lock_1",
                        admitted_at=now,
                    )
                )
                assert admitted is not None
                assert admitted.status is ToolCallStatus.EXECUTING
                assert not any("sessions" in sql for sql in executed_sql)
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", _capture_sql)
        finally:
            await engine.dispose()

    asyncio.run(scenario())
