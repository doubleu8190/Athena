"""审批路由的隔离功能测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from athena.contracts.commands import CommandType
from athena.gateway.routes.approval import router
from tests.fakes import install_runtime


def approval_row(
    approval_id: str,
    *,
    session_id: str = "session-1",
    batch_id: str | None = "batch-1",
    decision: str | None = None,
    created_at: str | None = None,
    decided_at: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        approval_id=approval_id,
        session_id=session_id,
        run_id="run-1",
        tool_call_id=f"tool-call-{approval_id}",
        tool_name="read_local_file",
        arguments_json='{"path":"README.md"}',
        risk_level="high",
        approval_batch_id=batch_id,
        created_at=created_at or datetime.now(timezone.utc).isoformat(),
        decided_at=decided_at,
        decision=decision,
    )


def make_client(
    *, pending: list[SimpleNamespace] | None = None,
    history: list[SimpleNamespace] | None = None,
    resolved: list[SimpleNamespace] | None = None,
    batch: list[SimpleNamespace] | None = None,
) -> tuple[TestClient, AsyncMock, AsyncMock]:
    app = FastAPI()
    app.include_router(router)
    approval_store = AsyncMock()
    approval_store.list_pending_approvals.return_value = pending or []
    approval_store.list_approval_history.return_value = history or []
    approval_store.list_resolved_approvals.return_value = resolved or []
    approval_store.list_approvals_by_batch.return_value = batch or []
    command_store = AsyncMock()
    command_store.enqueue_command.return_value = True
    install_runtime(
        app,
        approval_store=approval_store,
        command_store=command_store,
    )
    return TestClient(app), approval_store, command_store


def test_list_pending_approvals_maps_rows_and_session_filter() -> None:
    row = approval_row("approval-1")
    client, approval_store, _ = make_client(pending=[row])

    response = client.get("/approvals", params={"session_id": "session-1"})

    assert response.status_code == 200
    assert response.json() == [
        {
            "approval_id": "approval-1",
            "tool_name": "read_local_file",
            "arguments": {"path": "README.md"},
            "risk_level": "high",
            "created_at": row.created_at,
            "session_id": "session-1",
            "run_id": "run-1",
            "tool_call_id": "tool-call-approval-1",
            "approval_batch_id": "batch-1",
            "resolved": False,
            "resolution": "pending",
        }
    ]
    approval_store.list_pending_approvals.assert_awaited_once_with("session-1")


def test_approval_logs_and_stats_are_available() -> None:
    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    decided_at = datetime.now(timezone.utc).isoformat()
    history_row = approval_row(
        "approval-1",
        decision="approved",
        created_at=created_at,
        decided_at=decided_at,
    )
    denied_row = approval_row(
        "approval-2", decision="denied", created_at=created_at
    )
    client, approval_store, _ = make_client(
        history=[history_row],
        resolved=[history_row, denied_row],
    )

    logs = client.get("/approvals/logs", params={"limit": 1, "offset": 2})
    stats = client.get("/approvals/stats")

    assert logs.status_code == 200
    assert logs.json()[0]["decision"] == "approved"
    expected_duration = (
        datetime.fromisoformat(decided_at) - datetime.fromisoformat(created_at)
    ).total_seconds() * 1000
    assert logs.json()[0]["decision_time_ms"] == expected_duration
    approval_store.list_approval_history.assert_awaited_once_with(
        session_id=None,
        limit=1,
        offset=2,
    )
    assert stats.status_code == 200
    assert stats.json() == {
        "today_total": 2,
        "today_approved": 1,
        "today_denied": 1,
        "approval_rate": 0.5,
    }


def test_submit_approval_batch_enqueues_one_resolve_command() -> None:
    rows = [approval_row("approval-1"), approval_row("approval-2")]
    client, _, command_store = make_client(batch=rows)

    response = client.post(
        "/approvals/batches/batch-1/respond",
        json={"decisions": {"approval-1": "approved", "approval-2": "denied"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["approval_batch_id"] == "batch-1"
    command_store.enqueue_command.assert_awaited_once()
    command = command_store.enqueue_command.await_args.args[0]
    assert command.command_type == CommandType.APPROVAL_RESOLVE
    assert command.payload.approval_batch_id == "batch-1"
    assert command.payload.decisions == {
        "approval-1": "approved",
        "approval-2": "denied",
    }


def test_submit_approval_batch_rejects_incomplete_decisions() -> None:
    client, _, command_store = make_client(batch=[approval_row("approval-1")])

    response = client.post(
        "/approvals/batches/batch-1/respond",
        json={"decisions": {}},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "approval_batch_incomplete"
    command_store.enqueue_command.assert_not_awaited()


def test_removed_approval_routes_are_not_registered() -> None:
    client, _, _ = make_client()

    assert client.get("/approvals/logs/session-1").status_code == 404
    assert client.post(
        "/approvals/approval-1/respond", json={"action": "allow"}
    ).status_code == 404
    assert client.post("/approvals/approval-1/cancel").status_code == 404
    assert client.post("/approvals/session/session-1/cancel-all").status_code == 404
