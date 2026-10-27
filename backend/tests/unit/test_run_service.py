from __future__ import annotations

from datetime import datetime, timezone

import pytest

from application.runs import RunRejectedError, RunService
from domain.runs import RunCancellationResult, RunEnqueueResult, RunStatus


class Submissions:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.receipts: dict[str, tuple[dict[str, object], RunEnqueueResult]] = {}

    async def enqueue_run(self, **values):
        self.calls.append(values)
        request_id = str(values["request_id"])
        parameters = values["parameters"]
        existing = self.receipts.get(request_id)
        if existing:
            if existing[0] != parameters:
                raise ValueError("idempotency_conflict")
            value = existing[1]
            return RunEnqueueResult(
                value.session_id, value.run_id, value.message_id, value.sequence,
                value.status, True,
            ), True
        payload = values["payload"]
        value = RunEnqueueResult(
            str(values["session_id"]), str(values["run_id"]), payload.message_id,
            1, RunStatus.QUEUED, False,
        )
        self.receipts[request_id] = (parameters, value)
        return value, False


class Lifecycle:
    def __init__(self) -> None:
        self.stop_calls = []
        self.finalized = []
        self.missing_session = False

    async def stop_active(self, session_id, **values):
        self.stop_calls.append((session_id, values))
        if self.missing_session:
            raise LookupError("session_not_found")
        run_id = values["expected_run_id"]
        if run_id is None:
            return RunCancellationResult(RunStatus.CANCELLED, False, "none", no_op=True), False
        return RunCancellationResult(RunStatus.STOPPING, True, "unknown", run_id=run_id), False

    async def finalize_stop(self, run_id, *, failed=False):
        self.finalized.append((run_id, failed))
        return False


class Executor:
    def __init__(self):
        self.cancelled = []

    async def cancel(self, run_id):
        self.cancelled.append(run_id)


def make_service():
    submissions = Submissions()
    lifecycle = Lifecycle()
    executor = Executor()
    ids = iter([
        "session-1", "run-1", "message-1",
        "session-retry", "run-retry", "message-retry",
        "run-2", "message-2", "run-3", "message-3",
    ])
    service = RunService(
        submissions,
        lifecycle,
        id_factory=lambda: next(ids),
        executor=executor,
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    return service, submissions, lifecycle, executor


@pytest.mark.asyncio
async def test_submit_reuses_the_same_server_generated_ids_for_an_idempotent_retry():
    service, submissions, _, _ = make_service()
    first = await service.submit(
        None, request_id="request-1", project_id="project-1", message="hello"
    )
    retry = await service.submit(
        None, request_id="request-1", project_id="project-1", message="hello"
    )

    assert (first.session_id, first.run_id, first.message_id) == (
        retry.session_id, retry.run_id, retry.message_id
    )
    assert first.replayed is False
    assert retry.replayed is True
    assert len(submissions.calls) == 2
    assert submissions.calls[0]["payload"].message_id == "message-1"
    assert submissions.calls[0]["run_id"] == "run-1"
    assert submissions.calls[0]["new_session"].id == "session-1"


@pytest.mark.asyncio
async def test_same_message_with_a_new_request_id_is_a_new_run():
    service, _, _, _ = make_service()
    first = await service.submit("session-1", request_id="request-1", message="hello")
    second = await service.submit("session-1", request_id="request-2", message="hello")

    assert first.run_id != second.run_id
    assert first.message_id != second.message_id


@pytest.mark.asyncio
async def test_stop_is_bound_to_the_expected_active_run_and_finalizes_safely():
    service, _, lifecycle, executor = make_service()
    result = await service.stop_session(
        "session-1",
        request_id="stop-1",
        expected_run_id="run-active",
        displayed_session_seq=12,
    )

    assert result.run_id == "run-active"
    assert result.status == "stopping"
    assert executor.cancelled == ["run-active"]
    assert lifecycle.finalized == [("run-active", False)]
    assert lifecycle.stop_calls[0][1]["displayed_session_seq"] == 12


@pytest.mark.asyncio
async def test_stop_without_an_active_run_is_a_no_op():
    service, _, lifecycle, executor = make_service()
    result = await service.stop_session(
        "session-1", request_id="stop-empty", expected_run_id=None,
        displayed_session_seq=0,
    )

    assert result.no_op is True
    assert result.run_id is None
    assert executor.cancelled == []
    assert lifecycle.finalized == []


@pytest.mark.asyncio
async def test_stop_maps_a_missing_session_to_a_run_rejection():
    service, _, lifecycle, _ = make_service()
    lifecycle.missing_session = True

    with pytest.raises(RunRejectedError, match="session_not_found"):
        await service.stop_session(
            "missing-session",
            request_id="stop-missing",
            expected_run_id=None,
            displayed_session_seq=0,
        )
