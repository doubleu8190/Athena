"""Run HTTP API 的真实 ASGI、Cookie 与业务行为测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

from auth_support import AuthTestClient, make_auth_service
from fastapi.testclient import TestClient
from system_prompt_support import (
    TEST_COMPRESSION_PROMPT_ID,
    TEST_MAIN_PROMPT_ID,
)

from app.session.service import SessionService
from domain.message import ConversationMessage, TextPart
from domain.run.model import (
    AnswerRunCommand,
    InputRequestStatus,
    Run,
    RunApprovalResumedPayload,
    RunCancelledPayload,
    RunCancelRequestedPayload,
    RunDetails,
    RunEvent,
    RunInputCall,
    RunInputRequest,
    RunQueuedPayload,
    RunRequestOperation,
    RunRequestResult,
    RunResumedPayload,
    RunStatus,
    RunUsage,
    SubmitRunResult,
)
from domain.run.model.errors import (
    RunConflictError,
    RunNotFoundError,
    RunStateError,
    SessionHasActiveRunError,
    ToolRetryConfirmationRequiredError,
)
from domain.run.ports import RunRepository
from domain.tool import (
    DecideToolApprovalCommand,
    PendingToolApproval,
    RunToolCallDetails,
    RunToolCallPage,
    RunToolCallRecord,
    ToolApprovalDecision,
    ToolCallStatus,
    ToolKind,
    ToolOutcome,
    ToolOutcomeStatus,
)
from main import create_app


class InMemoryRunRepository:
    """供 HTTP 测试使用的最小状态化仓储替身。"""

    def __init__(self) -> None:
        self.sessions: set[tuple[str, str]] = set()
        self.deleted_sessions: set[tuple[str, str]] = set()
        self.runs: dict[tuple[str, str, str], Run] = {}
        self.messages: dict[str, ConversationMessage] = {}
        self.events_by_run: dict[str, list[RunEvent]] = {}
        self.requests: dict[tuple[str, str, RunRequestOperation, str], tuple[str, object]] = {}
        self.inputs: dict[tuple[str, str], RunInputRequest] = {}
        self.calls: dict[tuple[str, str], RunInputCall] = {}
        self.last_answer_auth_session_id: str | None = None
        self.tool_calls: list[RunToolCallDetails] = []
        self.pending_approvals: dict[str, list[PendingToolApproval]] = {}
        self.decided_approval_calls: dict[str, ToolApprovalDecision] = {}
        self.sequence = 0
        self.next_id = 0

    def _id(self, prefix: str) -> str:
        self.next_id += 1
        return f"{prefix}_{self.next_id}"

    def _append_event(self, run: Run, event_type: str, payload: object) -> Run:
        event = RunEvent(
            id=self._id("event"),
            run_id=run.id,
            sequence=run.event_sequence + 1,
            type=event_type,
            created_at=datetime.now(UTC),
            payload=payload,  # type: ignore[arg-type]
        )
        self.events_by_run.setdefault(run.id, []).append(event)
        updated = replace(run, event_sequence=event.sequence)
        self.runs[(run.owner_id, run.session_id, run.id)] = updated
        return updated

    def _visible(self, owner_id: str, session_id: str) -> bool:
        key = (owner_id, session_id)
        return key in self.sessions and key not in self.deleted_sessions

    async def submit(self, session_id: str, owner_id: str, command: object) -> SubmitRunResult:
        from domain.run.model import SubmitRunCommand

        assert isinstance(command, SubmitRunCommand)
        if not self._visible(owner_id, session_id):
            from domain.session import SessionNotFoundError

            raise SessionNotFoundError("session_not_found")
        key = (owner_id, session_id, RunRequestOperation.SUBMIT, command.request_id)
        replay = self.requests.get(key)
        if replay is not None:
            if replay[0] != command.fingerprint:
                raise RunConflictError("run_request_id_conflict")
            result = replay[1]
            assert isinstance(result, RunRequestResult)
            run = self.runs[(owner_id, session_id, result.run_id or "")]
            return SubmitRunResult(run, self.messages[result.message_id or ""], True)
        if any(
            run.session_id == session_id
            and run.owner_id == owner_id
            and run.status
            in {
                RunStatus.QUEUED,
                RunStatus.RUNNING,
                RunStatus.WAITING_FOR_INPUT,
                RunStatus.WAITING_FOR_APPROVAL,
                RunStatus.CANCELLING,
            }
            for run in self.runs.values()
        ):
            raise RunConflictError("run_already_active")
        now = command.queued_at
        message = ConversationMessage(
            id=command.message.id,
            session_id=session_id,
            role=command.message.role,
            status=command.message.status,
            sequence=self.sequence + 1,
            parts=command.message.parts,
            reply_to_message_id=None,
            run_id=command.run_id,
            client_message_id=None,
            created_at=now,
            completed_at=now,
            metadata=command.message.metadata,
        )
        self.sequence += 1
        run = Run(
            id=command.run_id,
            session_id=session_id,
            owner_id=owner_id,
            status=RunStatus.QUEUED,
            trigger_message_id=message.id,
            context_cutoff_sequence=message.sequence,
            retry_of_run_id=None,
            model_config_version_id="profile-public-id",
            main_prompt_version_id=TEST_MAIN_PROMPT_ID,
            compression_prompt_version_id=TEST_COMPRESSION_PROMPT_ID,
            prompt_template_version=command.prompt_template_version,
            event_sequence=0,
            execution_epoch=0,
            worker_id=None,
            lease_expires_at=None,
            execution_started_at=None,
            execution_deadline_at=None,
            queued_at=now,
            first_started_at=None,
            finished_at=None,
            created_at=now,
            updated_at=now,
            cancel_requested_at=None,
            safe_error_code=None,
            usage=RunUsage(),
        )
        self.messages[message.id] = message
        self.runs[(owner_id, session_id, run.id)] = run
        run = self._append_event(run, "queued", RunQueuedPayload(trigger_message_id=message.id))
        result = RunRequestResult(run_id=run.id, message_id=message.id)
        self.requests[key] = (command.fingerprint, result)
        return SubmitRunResult(run, message, False)

    async def answer(self, session_id: str, owner_id: str, command: AnswerRunCommand) -> Run:
        self.last_answer_auth_session_id = command.auth_session_id
        key = (owner_id, session_id, RunRequestOperation.ANSWER, command.request_id)
        replay = self.requests.get(key)
        if replay is not None:
            if replay[0] != command.fingerprint:
                raise RunConflictError("run_request_id_conflict")
            return self.runs[(owner_id, session_id, command.run_id)]
        run_key = (owner_id, session_id, command.run_id)
        run = self.runs.get(run_key)
        if run is None:
            raise RunNotFoundError("run_not_found")
        request = self.inputs.get((run.id, command.input_request_id))
        if run.status is not RunStatus.WAITING_FOR_INPUT or request is None:
            raise RunStateError("run_input_request_not_pending")
        answered = replace(request, status=InputRequestStatus.ANSWERED)
        self.inputs[(run.id, request.id)] = answered
        self.messages[command.answer_message.id] = ConversationMessage(
            id=command.answer_message.id,
            session_id=session_id,
            role=command.answer_message.role,
            status=command.answer_message.status,
            sequence=self.sequence + 1,
            parts=command.answer_message.parts,
            reply_to_message_id=None,
            run_id=run.id,
            client_message_id=None,
            created_at=command.queued_at,
            completed_at=command.queued_at,
            metadata=command.answer_message.metadata,
        )
        self.sequence += 1
        resumed = replace(
            run,
            status=RunStatus.QUEUED,
            queued_at=command.queued_at,
            auth_session_id=command.auth_session_id or run.auth_session_id,
        )
        resumed = self._append_event(
            resumed,
            "resumed",
            RunResumedPayload(
                input_request_id=request.id,
                answer_message_id=command.answer_message.id,
            ),
        )
        self.requests[key] = (
            command.fingerprint,
            RunRequestResult(
                run_id=run.id,
                message_id=command.answer_message.id,
                input_request_id=request.id,
            ),
        )
        return resumed

    async def get_input_call(
        self, session_id: str, owner_id: str, run_id: str, input_request_id: str
    ) -> RunInputCall | None:
        request = self.inputs.get((run_id, input_request_id))
        if (
            not self._visible(owner_id, session_id)
            or request is None
            or request.status not in {InputRequestStatus.PENDING, InputRequestStatus.ANSWERED}
        ):
            return None
        return self.calls.get((run_id, input_request_id))

    async def get(self, session_id: str, owner_id: str, run_id: str) -> Run | None:
        if not self._visible(owner_id, session_id):
            return None
        return self.runs.get((owner_id, session_id, run_id))

    async def get_details(self, session_id: str, owner_id: str, run_id: str) -> RunDetails | None:
        run = await self.get(session_id, owner_id, run_id)
        if run is None:
            return None
        pending_input = await self.get_pending_input(session_id, owner_id, run_id)
        pending_approvals = await self.list_pending_approvals(session_id, owner_id, run_id) or ()
        return RunDetails(
            run=run,
            pending_input=pending_input,
            pending_approvals=pending_approvals,
            subagent_tasks=(),
            pending_subagent_inputs=(),
            pending_subagent_approvals=(),
        )

    async def get_pending_input(
        self, session_id: str, owner_id: str, run_id: str
    ) -> RunInputRequest | None:
        if not self._visible(owner_id, session_id):
            return None
        return next(
            (
                item
                for (found_run_id, _), item in self.inputs.items()
                if found_run_id == run_id and item.status is InputRequestStatus.PENDING
            ),
            None,
        )

    async def list_pending_approvals(
        self, session_id: str, owner_id: str, run_id: str
    ) -> tuple[PendingToolApproval, ...] | None:
        if await self.get(session_id, owner_id, run_id) is None:
            return None
        return tuple(self.pending_approvals.get(run_id, ()))

    async def decide_tool_approval(self, command: DecideToolApprovalCommand) -> Run | None:
        key = (
            command.owner_id,
            command.session_id,
            RunRequestOperation.DECIDE_TOOL_APPROVAL,
            command.request_id,
        )
        replay = self.requests.get(key)
        if replay is not None:
            if replay[0] != command.fingerprint:
                raise RunConflictError("run_request_id_conflict")
            result = replay[1]
            assert isinstance(result, RunRequestResult)
            return self.runs.get((command.owner_id, command.session_id, result.run_id or ""))
        run = await self.get(command.session_id, command.owner_id, command.run_id)
        if run is None:
            return None
        pending = self.pending_approvals.get(run.id, [])
        matched = next((item for item in pending if item.call_id == command.record_id), None)
        if matched is None:
            previous = self.decided_approval_calls.get(command.record_id)
            if previous is None:
                raise RunNotFoundError("run_tool_call_not_found")
            if previous is not command.decision:
                raise RunConflictError("run_tool_approval_already_decided")
            return run
        if run.status is not RunStatus.WAITING_FOR_APPROVAL:
            raise RunStateError("run_tool_approval_not_pending")
        remaining = [item for item in pending if item.call_id != command.record_id]
        self.pending_approvals[run.id] = remaining
        self.decided_approval_calls[command.record_id] = command.decision
        updated = replace(
            run,
            auth_session_id=command.auth_session_id or run.auth_session_id,
        )
        if not remaining:
            updated = replace(updated, status=RunStatus.QUEUED, queued_at=command.decided_at)
            updated = self._append_event(
                updated,
                "approval_resumed",
                RunApprovalResumedPayload(batch_message_id=matched.batch_message_id),
            )
        else:
            self.runs[(command.owner_id, command.session_id, run.id)] = updated
        self.requests[key] = (command.fingerprint, RunRequestResult(run_id=run.id))
        return updated

    async def list_runs(
        self,
        session_id: str,
        owner_id: str,
        *,
        before_created_at: datetime | None,
        before_id: str | None,
        limit: int,
    ) -> tuple[Run, ...] | None:
        if not self._visible(owner_id, session_id):
            return None
        runs = sorted(
            (
                run
                for run in self.runs.values()
                if run.owner_id == owner_id and run.session_id == session_id
            ),
            key=lambda run: (run.created_at, run.id),
            reverse=True,
        )
        if before_created_at is not None and before_id is not None:
            runs = [
                run for run in runs if (run.created_at, run.id) < (before_created_at, before_id)
            ]
        return tuple(runs[:limit])

    async def list_events(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        *,
        after_sequence: int,
        limit: int,
        through_sequence: int | None = None,
    ) -> tuple[RunEvent, ...]:
        visible = await self.list_visible_events(
            session_id,
            owner_id,
            run_id,
            after_sequence=after_sequence,
            limit=limit,
            through_sequence=through_sequence,
        )
        return () if visible is None else visible

    async def list_visible_events(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        *,
        after_sequence: int,
        limit: int,
        through_sequence: int | None = None,
    ) -> tuple[RunEvent, ...] | None:
        if await self.get(session_id, owner_id, run_id) is None:
            return None
        return tuple(
            event
            for event in self.events_by_run.get(run_id, [])
            if event.sequence > after_sequence
            and (through_sequence is None or event.sequence <= through_sequence)
        )[:limit]

    async def cancel(
        self, session_id: str, owner_id: str, run_id: str, now: datetime
    ) -> Run | None:
        run = await self.get(session_id, owner_id, run_id)
        if run is None:
            return None
        if run.status in {
            RunStatus.QUEUED,
            RunStatus.WAITING_FOR_INPUT,
            RunStatus.WAITING_FOR_APPROVAL,
        }:
            return self._append_event(
                replace(run, status=RunStatus.CANCELLED),
                "cancelled",
                RunCancelledPayload(),
            )
        if run.status is RunStatus.RUNNING:
            return self._append_event(
                replace(run, status=RunStatus.CANCELLING),
                "cancel_requested",
                RunCancelRequestedPayload(),
            )
        return run

    async def retry(
        self,
        session_id: str,
        owner_id: str,
        source_run_id: str,
        request_id: str,
        fingerprint: str,
        new_run_id: str,
        prompt_template_version: str,
        now: datetime,
        confirm_unknown: bool = False,
        auth_session_id: str | None = None,
    ) -> Run | None:
        source = await self.get(session_id, owner_id, source_run_id)
        if source is None:
            return None
        key = (owner_id, session_id, RunRequestOperation.RETRY, request_id)
        replay = self.requests.get(key)
        if replay is not None:
            if replay[0] != fingerprint:
                raise RunConflictError("run_request_id_conflict")
            result = replay[1]
            assert isinstance(result, RunRequestResult)
            return self.runs.get((owner_id, session_id, result.run_id or ""))
        if source.status is not RunStatus.FAILED:
            raise RunStateError("run_not_retryable")
        has_unknown = any(
            item.record.run_id == source.id and item.record.status is ToolCallStatus.UNKNOWN
            for item in self.tool_calls
        )
        if has_unknown and not confirm_unknown:
            raise ToolRetryConfirmationRequiredError("tool_retry_confirmation_required")
        new_run = replace(
            source,
            id=new_run_id,
            status=RunStatus.QUEUED,
            retry_of_run_id=source.id,
            prompt_template_version=prompt_template_version,
            event_sequence=0,
            execution_epoch=0,
            queued_at=now,
            created_at=now,
            updated_at=now,
            finished_at=None,
            safe_error_code=None,
            usage=RunUsage(),
            tool_snapshot=None,
            auth_session_id=auth_session_id,
            llm_request_count=0,
        )
        self.runs[(owner_id, session_id, new_run.id)] = new_run
        self.requests[key] = (fingerprint, RunRequestResult(run_id=new_run.id))
        return new_run

    async def list_subagent_tasks(
        self, session_id: str, owner_id: str, parent_run_id: str
    ) -> tuple[object, ...]:
        del session_id, owner_id, parent_run_id
        return ()

    async def list_pending_subagent_inputs(
        self, session_id: str, owner_id: str, parent_run_id: str
    ) -> tuple[object, ...]:
        del session_id, owner_id, parent_run_id
        return ()

    async def list_pending_subagent_approvals(
        self, session_id: str, owner_id: str, parent_run_id: str
    ) -> tuple[object, ...]:
        del session_id, owner_id, parent_run_id
        return ()

    async def list_tool_calls(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        *,
        after_ordinal: int,
        limit: int,
    ) -> RunToolCallPage | None:
        if await self.get(session_id, owner_id, run_id) is None:
            return None
        items = sorted(
            (
                item
                for item in self.tool_calls
                if item.record.run_id == run_id and item.record.ordinal > after_ordinal
            ),
            key=lambda item: item.record.ordinal,
        )
        has_more = len(items) > limit
        visible = tuple(item.record for item in items[:limit])
        return RunToolCallPage(
            visible,
            visible[-1].ordinal if has_more and visible else None,
        )

    async def get_tool_call(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        call_id: str,
    ) -> RunToolCallDetails | None:
        if await self.get(session_id, owner_id, run_id) is None:
            return None
        return next(
            (
                item
                for item in self.tool_calls
                if item.record.run_id == run_id and item.record.id == call_id
            ),
            None,
        )


def test_run_http_auth_idempotency_clarification_and_control() -> None:
    repository = InMemoryRunRepository()
    from app.run.service import RunService

    service = RunService(cast(RunRepository, repository))
    app = create_app(
        auth_service=make_auth_service(),
        run_service=service,
        public_origin="https://app.example.com",
    )
    with TestClient(app) as unauthenticated:
        assert unauthenticated.get("/api/v1/sessions/session-run/runs").status_code == 401
    auth_client = AuthTestClient(app, email="run-owner@example.com")
    with auth_client as client:
        assert client.get("/api/v1/sessions/session-run/runs").status_code == 404
        repository.sessions.add((auth_client.user_id or "", "session-run"))
        response = client.post(
            "/api/v1/sessions/session-run/runs",
            headers={"Origin": "https://evil.example.com"},
            json={"request_id": "req-1", "text": "hello"},
        )
        assert response.status_code == 403
        first = client.post(
            "/api/v1/sessions/session-run/runs",
            json={"request_id": "req-1", "text": "hello\n\tworld"},
        )
        replay = client.post(
            "/api/v1/sessions/session-run/runs",
            json={"request_id": "req-1", "text": "hello\n\tworld"},
        )
        conflict = client.post(
            "/api/v1/sessions/session-run/runs",
            json={"request_id": "req-1", "text": "different"},
        )
        assert first.status_code == 201
        assert replay.status_code == 201 and replay.json()["replayed"] is True
        assert conflict.status_code == 409
        run_id = first.json()["id"]
        trigger_part = repository.messages[first.json()["trigger_message_id"]].parts[0]
        assert isinstance(trigger_part, TextPart)
        assert trigger_part.text == "hello\n\tworld"
        assert "base_url" not in first.json() and "credential_ref" not in first.json()

        listed = client.get("/api/v1/sessions/session-run/runs")
        details = client.get(f"/api/v1/sessions/session-run/runs/{run_id}")
        events = client.get(f"/api/v1/sessions/session-run/runs/{run_id}/events")
        invalid = client.post(
            "/api/v1/sessions/session-run/runs",
            json={"request_id": "req-invalid", "text": "   "},
        )
        invalid_control = client.post(
            "/api/v1/sessions/session-run/runs",
            json={"request_id": "req-control", "text": "bad\u0000text"},
        )
        assert listed.status_code == details.status_code == events.status_code == 200
        assert invalid.status_code == invalid_control.status_code == 422

        run_key = (auth_client.user_id or "", "session-run", run_id)
        run = repository.runs[run_key]
        request = RunInputRequest(
            id="input-request-1",
            run_id=run_id,
            session_id="session-run",
            question_message_id="msg-question-1",
            status=InputRequestStatus.PENDING,
            answer_message_id=None,
            created_at=datetime.now(UTC),
            answered_at=None,
            closed_at=None,
        )
        repository.inputs[(run_id, request.id)] = request
        repository.calls[(run_id, request.id)] = RunInputCall(
            tool_call_id="model-call-9",
            thread_id="thread-run-1",
            question_message_id=request.question_message_id,
        )
        repository.runs[run_key] = replace(run, status=RunStatus.WAITING_FOR_INPUT)
        first_answer = client.post(
            f"/api/v1/sessions/session-run/runs/{run_id}/input-requests/{request.id}/answer",
            json={"request_id": "answer-command-1", "text": "option one\r\ncontinued\ttext"},
        )
        answer_replay = client.post(
            f"/api/v1/sessions/session-run/runs/{run_id}/input-requests/{request.id}/answer",
            json={"request_id": "answer-command-1", "text": "option one\r\ncontinued\ttext"},
        )
        assert first_answer.status_code == answer_replay.status_code == 200
        assert first_answer.json()["status"] == "queued"
        assert repository.last_answer_auth_session_id
        assert repository.runs[run_key].auth_session_id == repository.last_answer_auth_session_id
        assert repository.inputs[(run_id, request.id)].status is InputRequestStatus.ANSWERED
        answer_request_key = (
            auth_client.user_id or "",
            "session-run",
            RunRequestOperation.ANSWER,
            "answer-command-1",
        )
        answer_result = repository.requests[answer_request_key][1]
        assert isinstance(answer_result, RunRequestResult)
        answer_part = repository.messages[answer_result.message_id or ""].parts[0]
        assert isinstance(answer_part, TextPart)
        assert answer_part.text == "option one\r\ncontinued\ttext"
        answer_conflict = client.post(
            f"/api/v1/sessions/session-run/runs/{run_id}/input-requests/{request.id}/answer",
            json={"request_id": "answer-command-different", "text": "late answer"},
        )
        assert answer_conflict.status_code == 409

        second_request = replace(
            request,
            id="input-request-2",
            question_message_id="msg-question-2",
            created_at=datetime.now(UTC),
            answer_message_id=None,
            answered_at=None,
            closed_at=None,
            status=InputRequestStatus.PENDING,
        )
        repository.inputs[(run_id, second_request.id)] = second_request
        repository.calls[(run_id, second_request.id)] = RunInputCall(
            tool_call_id="model-call-10",
            thread_id="thread-run-1",
            question_message_id=second_request.question_message_id,
        )
        repository.runs[run_key] = replace(
            repository.runs[run_key], status=RunStatus.WAITING_FOR_INPUT
        )
        pending_details = client.get(f"/api/v1/sessions/session-run/runs/{run_id}")
        assert pending_details.json()["pending_input"]["id"] == second_request.id
        second_answer = client.post(
            f"/api/v1/sessions/session-run/runs/{run_id}/input-requests/{second_request.id}/answer",
            json={"request_id": "answer-command-2", "text": "option two"},
        )
        assert second_answer.status_code == 200

        cancelled = client.post(f"/api/v1/sessions/session-run/runs/{run_id}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        second_run_response = client.post(
            "/api/v1/sessions/session-run/runs",
            json={"request_id": "req-2", "text": "second task"},
        )
        assert second_run_response.status_code == 201
        client.post(f"/api/v1/sessions/session-run/runs/{second_run_response.json()['id']}/cancel")
        first_page = client.get("/api/v1/sessions/session-run/runs?limit=1")
        assert first_page.status_code == 200 and first_page.json()["next_cursor"]
        second_page = client.get(
            "/api/v1/sessions/session-run/runs",
            params={"limit": 1, "cursor": first_page.json()["next_cursor"]},
        )
        assert second_page.status_code == 200 and len(second_page.json()["items"]) == 1
        repository.runs[run_key] = replace(repository.runs[run_key], status=RunStatus.FAILED)
        retried = client.post(
            f"/api/v1/sessions/session-run/runs/{run_id}/retry",
            json={"request_id": "retry-1"},
        )
        retry_replay = client.post(
            f"/api/v1/sessions/session-run/runs/{run_id}/retry",
            json={"request_id": "retry-1"},
        )
        assert retried.status_code == retry_replay.status_code == 200
        assert retried.json()["retry_of_run_id"] == run_id
        assert retried.json()["trigger_message_id"] == first.json()["trigger_message_id"]
        assert retried.json()["retry_warning"]


def test_run_details_projects_persisted_context_safety_report_reference() -> None:
    """失败终态详情返回报告消息 ID，客户端不依赖瞬时 SSE 文本定位报告。"""
    repository = InMemoryRunRepository()
    from app.run.service import RunService

    app = create_app(
        auth_service=make_auth_service(),
        run_service=RunService(cast(RunRepository, repository)),
        public_origin="https://app.example.com",
    )
    auth_client = AuthTestClient(app, email="context-stop@example.com")
    with auth_client as client:
        session_id = "session-context-stop"
        owner_id = auth_client.user_id or ""
        repository.sessions.add((owner_id, session_id))
        submitted = client.post(
            f"/api/v1/sessions/{session_id}/runs",
            json={"request_id": "request-context-stop", "text": "large context"},
        )
        assert submitted.status_code == 201
        run_id = submitted.json()["id"]
        key = (owner_id, session_id, run_id)
        repository.runs[key] = replace(
            repository.runs[key],
            status=RunStatus.FAILED,
            safe_error_code="run_context_safety_stop",
            report_message_id="msg_context_safety_report",
        )

        details = client.get(f"/api/v1/sessions/{session_id}/runs/{run_id}")

    assert details.status_code == 200
    assert details.json()["report_message_id"] == "msg_context_safety_report"


def test_run_http_cross_owner_is_hidden_and_auth_is_real() -> None:
    repository = InMemoryRunRepository()
    from app.run.service import RunService

    app = create_app(
        auth_service=make_auth_service(),
        run_service=RunService(cast(RunRepository, repository)),
    )
    with AuthTestClient(app, email="run-other-owner@example.com") as client:
        assert client.get("/api/v1/sessions/foreign-session/runs/foreign-run").status_code == 404
        assert (
            client.post(
                "/api/v1/sessions/foreign-session/runs",
                json={"request_id": "req", "text": "hello"},
            ).status_code
            == 404
        )


def test_unknown_retry_confirmation_tool_call_projection_and_warning() -> None:
    repository = InMemoryRunRepository()
    from app.run.service import RunService

    app = create_app(
        auth_service=make_auth_service(),
        run_service=RunService(cast(RunRepository, repository)),
    )
    auth_client = AuthTestClient(app, email="tool-call-owner@example.com")
    with auth_client as client:
        owner_id = auth_client.user_id or ""
        session_id = "session-tool-calls"
        repository.sessions.add((owner_id, session_id))
        submitted = client.post(
            f"/api/v1/sessions/{session_id}/runs",
            json={"request_id": "unknown-source", "text": "perform operation"},
        )
        run_id = submitted.json()["id"]
        key = (owner_id, session_id, run_id)
        repository.runs[key] = replace(
            repository.runs[key],
            status=RunStatus.FAILED,
            safe_error_code="tool_outcome_unknown",
        )
        now = datetime.now(UTC)
        for ordinal, status, outcome_status, code in (
            (1, ToolCallStatus.COMPLETED, ToolOutcomeStatus.OK, "completed"),
            (2, ToolCallStatus.UNKNOWN, ToolOutcomeStatus.UNKNOWN, "tool_timeout"),
        ):
            record = RunToolCallRecord(
                id=f"call-record-{ordinal}",
                run_id=run_id,
                session_id=session_id,
                tool_call_id=f"model-call-{ordinal}",
                ordinal=ordinal,
                tool_id="tool_safe",
                tool_kind=ToolKind.MCP,
                model_name="safe_tool",
                execution_epoch=1,
                status=status,
                call_message_id=f"exec-call-{ordinal}",
                result_message_id=f"exec-result-{ordinal}",
                started_at=now,
                finished_at=now,
                safe_error_code=None if outcome_status is ToolOutcomeStatus.OK else code,
                outcome=ToolOutcome(
                    status=outcome_status,
                    code=code,
                    message="safe result",
                ),
            )
            repository.tool_calls.append(
                RunToolCallDetails(record, {"query": f"value-{ordinal}"}, "safe result")
            )

        source_details = client.get(f"/api/v1/sessions/{session_id}/runs/{run_id}")
        first_page = client.get(
            f"/api/v1/sessions/{session_id}/runs/{run_id}/tool-calls",
            params={"limit": 1},
        )
        second_page = client.get(
            f"/api/v1/sessions/{session_id}/runs/{run_id}/tool-calls",
            params={"limit": 1, "after_ordinal": 1},
        )
        unknown_detail = client.get(
            f"/api/v1/sessions/{session_id}/runs/{run_id}/tool-calls/call-record-2"
        )
        unconfirmed = client.post(
            f"/api/v1/sessions/{session_id}/runs/{run_id}/retry",
            json={"request_id": "retry-unconfirmed"},
        )
        confirmed = client.post(
            f"/api/v1/sessions/{session_id}/runs/{run_id}/retry",
            json={"request_id": "retry-confirmed", "confirm_unknown": True},
        )

        assert source_details.json()["retry_warning"]
        assert first_page.status_code == 200
        assert first_page.json()["next_after_ordinal"] == 1
        assert second_page.json()["items"][0]["status"] == "unknown"
        assert unknown_detail.status_code == 200
        assert unknown_detail.json()["arguments"] == {"query": "value-2"}
        assert unknown_detail.json()["outcome"]["code"] == "tool_timeout"
        assert "server_id" not in unknown_detail.text
        assert unconfirmed.status_code == 409
        assert unconfirmed.json() == {"detail": "tool_retry_confirmation_required"}
        assert confirmed.status_code == 200
        assert confirmed.json()["retry_warning"]
        retried_key = (owner_id, session_id, confirmed.json()["id"])
        assert repository.runs[retried_key].llm_request_count == 0
        assert repository.runs[key].status is RunStatus.FAILED


def test_tool_approval_api_is_owner_scoped_origin_checked_and_idempotent() -> None:
    """审批端点逐调用投影、同源校验、幂等和部分决定语义保持隔离。"""

    repository = InMemoryRunRepository()
    from app.run.service import RunService

    app = create_app(
        auth_service=make_auth_service(),
        run_service=RunService(cast(RunRepository, repository)),
        public_origin="https://app.example.com",
    )
    auth_client = AuthTestClient(app, email="approval-owner@example.com")
    with auth_client as client:
        owner_id = auth_client.user_id or ""
        session_id = "session-approval"
        repository.sessions.add((owner_id, session_id))
        submitted = client.post(
            f"/api/v1/sessions/{session_id}/runs",
            json={"request_id": "approval-source", "text": "需要审批的任务"},
        )
        assert submitted.status_code == 201
        run_id = submitted.json()["id"]
        now = datetime.now(UTC)
        repository.pending_approvals[run_id] = [
            PendingToolApproval(
                call_id="record-call-1",
                batch_message_id="exec-batch-1",
                ordinal=1,
                model_name="mcp_lookup",
                tool_kind=ToolKind.MCP,
                requested_at=now,
            ),
            PendingToolApproval(
                call_id="record-call-2",
                batch_message_id="exec-batch-1",
                ordinal=2,
                model_name="mcp_write",
                tool_kind=ToolKind.MCP,
                requested_at=now,
            ),
        ]
        run_key = (owner_id, session_id, run_id)
        repository.runs[run_key] = replace(
            repository.runs[run_key], status=RunStatus.WAITING_FOR_APPROVAL
        )

        details = client.get(f"/api/v1/sessions/{session_id}/runs/{run_id}")
        assert details.status_code == 200
        assert [item["call_id"] for item in details.json()["pending_approvals"]] == [
            "record-call-1",
            "record-call-2",
        ]
        # 待审批投影仅暴露脱敏后的 formatted_arguments，严禁出现原始未处理 arguments 字段。
        assert all("arguments" not in item for item in details.json()["pending_approvals"])

        evil_origin = client.post(
            f"/api/v1/sessions/{session_id}/runs/{run_id}/tool-calls/record-call-1/approval",
            headers={"Origin": "https://evil.example.com"},
            json={"request_id": "approve-evil", "decision": "approve"},
        )
        missing_call = client.post(
            f"/api/v1/sessions/{session_id}/runs/{run_id}/tool-calls/missing/approval",
            json={"request_id": "approve-missing", "decision": "approve"},
        )
        extra_field = client.post(
            f"/api/v1/sessions/{session_id}/runs/{run_id}/tool-calls/record-call-1/approval",
            json={"request_id": "approve-extra", "decision": "approve", "arguments": {}},
        )
        assert evil_origin.status_code == 403
        assert missing_call.status_code == 404
        assert extra_field.status_code == 422

        first = client.post(
            f"/api/v1/sessions/{session_id}/runs/{run_id}/tool-calls/record-call-1/approval",
            headers={"Origin": "https://app.example.com"},
            json={"request_id": "approve-1", "decision": "approve"},
        )
        replay = client.post(
            f"/api/v1/sessions/{session_id}/runs/{run_id}/tool-calls/record-call-1/approval",
            headers={"Origin": "https://app.example.com"},
            json={"request_id": "approve-1", "decision": "approve"},
        )
        contradictory_replay = client.post(
            f"/api/v1/sessions/{session_id}/runs/{run_id}/tool-calls/record-call-1/approval",
            headers={"Origin": "https://app.example.com"},
            json={"request_id": "approve-1", "decision": "reject"},
        )
        assert first.status_code == replay.status_code == 200
        assert first.json()["status"] == "waiting_for_approval"
        assert [item["call_id"] for item in first.json()["pending_approvals"]] == ["record-call-2"]
        assert contradictory_replay.status_code == 409

        last = client.post(
            f"/api/v1/sessions/{session_id}/runs/{run_id}/tool-calls/record-call-2/approval",
            headers={"Origin": "https://app.example.com"},
            json={"request_id": "reject-2", "decision": "reject"},
        )
        assert last.status_code == 200
        assert last.json()["status"] == "queued"
        assert last.json()["pending_approvals"] == []
        late = client.post(
            f"/api/v1/sessions/{session_id}/runs/{run_id}/tool-calls/record-call-2/approval",
            headers={"Origin": "https://app.example.com"},
            json={"request_id": "late-opposite", "decision": "approve"},
        )
        assert late.status_code == 409

    with AuthTestClient(app, email="approval-intruder@example.com") as intruder:
        hidden = intruder.get(f"/api/v1/sessions/{session_id}/runs/{run_id}")
        denied = intruder.post(
            f"/api/v1/sessions/{session_id}/runs/{run_id}/tool-calls/record-call-1/approval",
            json={"request_id": "intruder", "decision": "approve"},
        )
        assert hidden.status_code == denied.status_code == 404


class InMemorySessionDeleteRepository:
    """将 Run 状态映射成 Session 删除保护的 API 测试端口。"""

    def __init__(self, runs: InMemoryRunRepository) -> None:
        self._runs = runs

    async def soft_delete(self, owner_id: str, session_id: str, deleted_at: datetime) -> bool:
        if not self._runs._visible(owner_id, session_id):
            return False
        if any(
            run.owner_id == owner_id
            and run.session_id == session_id
            and run.status
            in {
                RunStatus.QUEUED,
                RunStatus.RUNNING,
                RunStatus.WAITING_FOR_INPUT,
                RunStatus.WAITING_FOR_APPROVAL,
                RunStatus.CANCELLING,
            }
            for run in self._runs.runs.values()
        ):
            raise SessionHasActiveRunError("session_has_active_run")
        self._runs.deleted_sessions.add((owner_id, session_id))
        return True


def test_session_delete_active_run_conflict_and_terminal_visibility() -> None:
    repository = InMemoryRunRepository()
    from app.run.service import RunService

    session_id = "session-delete"
    session_service = SessionService(InMemorySessionDeleteRepository(repository))  # type: ignore[arg-type]
    app = create_app(
        auth_service=make_auth_service(),
        run_service=RunService(cast(RunRepository, repository)),
        session_service=session_service,
    )
    auth_client = AuthTestClient(app, email="run-delete-owner@example.com")
    with auth_client as client:
        owner_id = auth_client.user_id or ""
        repository.sessions.add((owner_id, session_id))
        submitted = client.post(
            f"/api/v1/sessions/{session_id}/runs",
            json={"request_id": "delete-guard-1", "text": "active"},
        )
        run_id = submitted.json()["id"]
        blocked = client.delete(f"/api/v1/sessions/{session_id}")
        assert blocked.status_code == 409
        assert blocked.json() == {"detail": "session_has_active_run"}
        assert (owner_id, session_id) not in repository.deleted_sessions

        repository.runs[(owner_id, session_id, run_id)] = replace(
            repository.runs[(owner_id, session_id, run_id)], status=RunStatus.COMPLETED
        )
        deleted = client.delete(f"/api/v1/sessions/{session_id}")
        assert deleted.status_code == 204
        assert client.get(f"/api/v1/sessions/{session_id}/runs").status_code == 404
        assert client.get(f"/api/v1/sessions/{session_id}/runs/{run_id}").status_code == 404
        assert client.post(f"/api/v1/sessions/{session_id}/runs/{run_id}/cancel").status_code == 404


def test_run_details_projects_completed_assistant_message_id_in_single_session() -> None:
    """completed 终态 Run 详情返回 assistant_message_id，与 report_message_id 对称。"""
    from domain.run.model import RunDetails

    repository = InMemoryRunRepository()
    from app.run.service import RunService

    async def get_details(session_id: str, owner_id: str, run_id: str) -> RunDetails | None:
        run = await repository.get(session_id, owner_id, run_id)
        if run is None:
            return None
        return RunDetails(
            run=run,
            pending_input=None,
            assistant_message_id=(
                "msg_completed_reply" if run.status is RunStatus.COMPLETED else None
            ),
        )

    repository.get_details = get_details  # type: ignore[method-assign]
    app = create_app(
        auth_service=make_auth_service(),
        run_service=RunService(cast(RunRepository, repository)),
    )
    auth_client = AuthTestClient(app, email="completed-assistant-id@example.com")
    with auth_client as client:
        session_id = "session-completed-assistant-id"
        owner_id = auth_client.user_id or ""
        repository.sessions.add((owner_id, session_id))
        submitted = client.post(
            f"/api/v1/sessions/{session_id}/runs",
            json={"request_id": "req-completed-id", "text": "hello"},
        )
        assert submitted.status_code == 201
        assert submitted.json()["assistant_message_id"] is None
        run_id = submitted.json()["id"]
        key = (owner_id, session_id, run_id)
        repository.runs[key] = replace(repository.runs[key], status=RunStatus.COMPLETED)

        details = client.get(f"/api/v1/sessions/{session_id}/runs/{run_id}")

    assert details.status_code == 200
    assert details.json()["assistant_message_id"] == "msg_completed_reply"
