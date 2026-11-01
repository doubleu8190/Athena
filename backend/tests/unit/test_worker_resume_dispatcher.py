"""审批恢复命令投递器的协议和退避单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from domain.orchestration.worker_entities import WorkerDispatchLease
from infrastructure.integrations.worker_http import WorkerTaskPostResponse
from workers.worker_resume_dispatcher import WorkerResumeDispatcher


class _Repository:
    """记录恢复调度器对持久层的裁决调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def record_resume_receipt(self, lease, receipt) -> bool:
        self.calls.append(("accepted", receipt))
        return True

    async def retry_dispatch_command(self, lease, *, delay_seconds: float) -> bool:
        self.calls.append(("retry", delay_seconds))
        return True

    async def exhaust_dispatch_retries(self, lease) -> bool:
        self.calls.append(("exhaust", None))
        return True

    async def await_dispatch_result(self, lease) -> bool:
        self.calls.append(("await", None))
        return True


def _dispatcher(repository: _Repository) -> WorkerResumeDispatcher:
    return WorkerResumeDispatcher(
        repository,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        worker_id="resume-test",
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
        lease_owner="resume-dispatcher",
        lease_token=str(uuid4()),
        attempt=attempt,
        command={},
        command_type="resume",
    )


@pytest.mark.asyncio
async def test_resume_receipt_requires_matching_command_and_accepts_duplicate() -> None:
    repository = _Repository()
    dispatcher = _dispatcher(repository)
    lease = _lease()
    received_at = datetime.now(timezone.utc).isoformat()

    await dispatcher._handle_response(
        lease,
        WorkerTaskPostResponse(
            status_code=202,
            body={
                "protocol_version": "1.0",
                "command_id": lease.command_id,
                "receipt_id": str(uuid4()),
                "disposition": "accepted",
                "received_at": received_at,
            },
            retry_after_seconds=None,
        ),
    )
    await dispatcher._handle_response(
        lease,
        WorkerTaskPostResponse(
            status_code=200,
            body={
                "protocol_version": "1.0",
                "command_id": lease.command_id,
                "receipt_id": str(uuid4()),
                "disposition": "duplicate",
                "received_at": received_at,
            },
            retry_after_seconds=None,
        ),
    )
    assert [name for name, _ in repository.calls] == ["accepted", "accepted"]

    await dispatcher._handle_response(
        lease,
        WorkerTaskPostResponse(
            status_code=202,
            body={
                "protocol_version": "1.0",
                "command_id": str(uuid4()),
                "receipt_id": str(uuid4()),
                "disposition": "accepted",
                "received_at": received_at,
            },
            retry_after_seconds=None,
        ),
    )
    assert repository.calls[-1][0] == "retry"


@pytest.mark.asyncio
async def test_resume_retry_after_is_lower_bound_and_unknown_error_waits() -> None:
    repository = _Repository()
    dispatcher = _dispatcher(repository)
    lease = _lease()
    await dispatcher._handle_response(
        lease,
        WorkerTaskPostResponse(
            status_code=429,
            body={
                "protocol_version": "1.0",
                "request_id": str(uuid4()),
                "code": "RATE_LIMITED",
                "message": "slow down",
                "directive": "retry_same_command",
                "retry_after_seconds": 90,
            },
            retry_after_seconds=30,
        ),
    )
    assert repository.calls == [("retry", 90)]

    await dispatcher._handle_response(
        lease,
        WorkerTaskPostResponse(
            status_code=422,
            body={
                "protocol_version": "1.0",
                "request_id": str(uuid4()),
                "code": "UNLISTED_ERROR",
                "message": "unknown",
                "directive": "root_replan",
            },
            retry_after_seconds=None,
        ),
    )
    assert repository.calls[-1][0] == "retry"


@pytest.mark.asyncio
async def test_resume_malformed_receipt_retries_same_command() -> None:
    repository = _Repository()
    dispatcher = _dispatcher(repository)
    await dispatcher._handle_response(
        _lease(),
        WorkerTaskPostResponse(
            status_code=202,
            body={"protocol_version": "1.0", "command_id": str(uuid4())},
            retry_after_seconds=None,
        ),
    )
    assert repository.calls == [("retry", 1.0)]


@pytest.mark.asyncio
async def test_resume_malformed_error_response_retries_same_command() -> None:
    """损坏的错误响应无法证明 Worker 是否已接收，必须保留容量重投。"""
    repository = _Repository()
    dispatcher = _dispatcher(repository)
    await dispatcher._handle_response(
        _lease(),
        WorkerTaskPostResponse(
            status_code=503,
            body={"protocol_version": "1.0", "code": "BROKEN"},
            retry_after_seconds=None,
        ),
    )
    assert repository.calls == [("retry", 1.0)]
