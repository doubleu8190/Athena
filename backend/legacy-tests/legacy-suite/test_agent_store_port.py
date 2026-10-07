"""拆分存储端口与 PostgreSQL 适配器之间的契约测试。"""

from __future__ import annotations

import inspect
from pathlib import Path

from athena.contracts.ports import (
    ApprovalStorePort,
    CommandStorePort,
    EventStorePort,
    RunStorePort,
)
from athena.infrastructure.postgre.repositories.agent_store import AgentStore
from athena.infrastructure.postgre.models import (
    AgentCommandModel,
    AgentEventModel,
    AgentRunModel,
    ApprovalRecordModel,
)
from athena.infrastructure.postgre.repositories.model_converters import (
    _row_to_approval,
    _row_to_command_status,
    _row_to_event,
    _row_to_run,
)


def test_agent_store_implements_each_narrow_store_port() -> None:
    """PostgreSQL 适配器必须实现每个拆分后的能力端口。"""

    for port in (RunStorePort, CommandStorePort, ApprovalStorePort, EventStorePort):
        methods = {
            name
            for name, member in inspect.getmembers(port, inspect.isfunction)
            if not name.startswith("_")
        }
        missing = sorted(name for name in methods if not hasattr(AgentStore, name))
        assert missing == [], (port.__name__, missing)


def test_command_consumer_has_no_optional_store_method_lookup() -> None:
    """存储端口方法不可通过动态属性探测来隐式降级。"""

    source = Path(__file__).parents[1].joinpath(
        "athena", "runtime", "command_consumer.py"
    ).read_text()
    assert "getattr(self.store" not in source
    assert "hasattr(self.store" not in source


def test_agent_store_read_converters_do_not_expose_orm_rows() -> None:
    """读取转换器只返回公开记录，不把 SQLAlchemy 行泄漏给调用方。"""
    run = _row_to_run(
        AgentRunModel(
            run_id="run-1",
            session_id="session-1",
            status="running",
            root_thread_id="thread-1",
            error_json=None,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:01+00:00",
        )
    )
    command = _row_to_command_status(
        AgentCommandModel(
            command_id="command-1",
            session_id="session-1",
            run_id="run-1",
            command_type="run.start",
            status="queued",
            result_json=None,
            error_json=None,
        )
    )
    event = _row_to_event(
        AgentEventModel(
            session_id="session-1",
            session_seq=1,
            event_type="run.started",
            durability="durable",
            payload_json="{}",
            occurred_at="2026-01-01T00:00:00+00:00",
            is_complete=0,
        )
    )
    approval = _row_to_approval(
        ApprovalRecordModel(
            approval_id="approval-1",
            session_id="session-1",
            run_id="run-1",
            tool_call_id="call-1",
            tool_name="write_file",
            arguments_json='{"path":"a.txt"}',
            risk_level="high",
            status="pending",
            created_at="2026-01-01T00:00:00+00:00",
        )
    )

    assert not isinstance(run, AgentRunModel)
    assert not isinstance(command, AgentCommandModel)
    assert not isinstance(event, AgentEventModel)
    assert not isinstance(approval, ApprovalRecordModel)
    assert (run.run_id, command.command_id, event.session_seq, approval.approval_id) == (
        "run-1",
        "command-1",
        1,
        "approval-1",
    )
