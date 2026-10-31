"""远程任务下发的 HTTP 协议裁决单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from domain.orchestration.worker_entities import WorkerDispatchLease
from application.orchestration.worker_protocol_models import WorkerHttpErrorResponseModel
from workers.worker_task_dispatcher import WorkerTaskDispatcher


class _Repository:
    """记录 Dispatcher 的事务裁决调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def record_dispatch_receipt(self, lease, receipt) -> bool:
        self.calls.append(("accepted", {"receipt": receipt}))
        return True

    async def retry_dispatch_command(self, lease, *, delay_seconds: float) -> bool:
        self.calls.append(("retry", {"delay": delay_seconds}))
        return True

    async def exhaust_dispatch_retries(self, lease) -> bool:
        self.calls.append(("exhaust", {}))
        return True

    async def fail_dispatch_command(self, lease, **kwargs) -> bool:
        self.calls.append(("failed", kwargs))
        return True

    async def await_dispatch_result(self, lease) -> bool:
        self.calls.append(("await", {}))
        return True


def _dispatcher(repository: _Repository) -> WorkerTaskDispatcher:
    return WorkerTaskDispatcher(
        repository,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        worker_id="test-dispatcher",
        max_concurrency=1,
        request_timeout_seconds=5,
        max_response_bytes=1024,
        max_retries=8,
        retry_initial_seconds=1,
        retry_max_seconds=30,
        retry_jitter_ratio=0,
    )


def _lease(*, attempt: int = 0) -> WorkerDispatchLease:
    return WorkerDispatchLease(
        command_id=str(uuid4()),
        execution_id=str(uuid4()),
        lease_owner="dispatcher",
        lease_token=str(uuid4()),
        attempt=attempt,
        command={},
    )


@pytest.mark.asyncio
async def test_task_receipt_requires_status_and_command_identity_to_match() -> None:
    repository = _Repository()
    dispatcher = _dispatcher(repository)
    lease = _lease()
    now = datetime.now(timezone.utc).isoformat()
    await dispatcher._handle_response(
        lease,
        202,
        {
            "protocol_version": "1.0",
            "command_id": lease.command_id,
            "receipt_id": str(uuid4()),
            "disposition": "accepted",
            "received_at": now,
        },
        None,
    )
    assert [name for name, _ in repository.calls] == ["accepted"]

    await dispatcher._handle_response(
        lease,
        200,
        {
            "protocol_version": "1.0",
            "command_id": lease.command_id,
            "receipt_id": str(uuid4()),
            "disposition": "accepted",
            "received_at": now,
        },
        None,
    )
    assert [name for name, _ in repository.calls] == ["accepted", "accepted"]

    await dispatcher._handle_response(
        lease,
        202,
        {
            "protocol_version": "1.0",
            "command_id": str(uuid4()),
            "receipt_id": str(uuid4()),
            "disposition": "accepted",
            "received_at": now,
        },
        None,
    )
    assert [name for name, _ in repository.calls] == ["accepted", "accepted", "await"]


@pytest.mark.asyncio
async def test_only_named_contract_errors_request_root_replan() -> None:
    repository = _Repository()
    dispatcher = _dispatcher(repository)
    lease = _lease()
    body = {
        "protocol_version": "1.0",
        "request_id": str(uuid4()),
        "message": "invalid input",
        "directive": "root_replan",
    }
    await dispatcher._handle_response(
        lease, 422, {**body, "code": "TASK_INPUT_INVALID"}, None
    )
    await dispatcher._handle_response(
        lease,
        422,
        {**body, "code": "VALIDATION_ERROR", "directive": "operator_action"},
        None,
    )
    assert repository.calls[0][0] == "failed"
    assert repository.calls[0][1]["root_replan"] is True
    assert repository.calls[1][0] == "failed"
    assert repository.calls[1][1]["root_replan"] is False


@pytest.mark.asyncio
async def test_retry_after_is_a_minimum_and_unknown_error_waits_for_result() -> None:
    repository = _Repository()
    dispatcher = _dispatcher(repository)
    lease = _lease()
    await dispatcher._handle_response(
        lease,
        503,
        {
            "protocol_version": "1.0",
            "request_id": str(uuid4()),
            "code": "TEMPORARY_UNAVAILABLE",
            "message": "temporary",
            "directive": "retry_same_command",
        },
        90,
    )
    await dispatcher._handle_response(
        lease,
        422,
        {
            "protocol_version": "1.0",
            "request_id": str(uuid4()),
            "code": "UNLISTED_ERROR",
            "message": "unknown",
            "directive": "root_replan",
        },
        None,
    )
    assert repository.calls[0] == ("retry", {"delay": 90})
    assert repository.calls[1][0] == "await"


@pytest.mark.asyncio
async def test_body_retry_after_uses_maximum_header_or_body_lower_bound() -> None:
    repository = _Repository()
    dispatcher = _dispatcher(repository)
    lease = _lease()
    body = {
        "protocol_version": "1.0",
        "request_id": str(uuid4()),
        "code": "RATE_LIMITED",
        "message": "slow down",
        "directive": "retry_same_command",
        "retry_after_seconds": 90,
    }
    error = WorkerHttpErrorResponseModel.model_validate(body)
    assert error.retry_after_seconds == 90
    await dispatcher._handle_response(lease, 429, body, 30)

    assert repository.calls == [("retry", {"delay": 90})]
