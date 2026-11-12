"""Run 提交、澄清、查询和控制用例。"""

from __future__ import annotations

import base64
import hashlib
import json
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime

from app.run.model.result import RunDetailsResult, RunListResult
from common.ids import new_id, now_utc
from domain.message import (
    ConversationMessageDraft,
    ConversationRole,
    ExecutionMessageDraft,
    ExecutionRole,
    MessageMetadata,
    MessageStatus,
    TextPart,
    ToolResultPart,
    ToolResultStatus,
)
from domain.run.model import (
    AnswerRunCommand,
    Run,
    RunEvent,
    RunInputFiles,
    SubmitRunCommand,
    SubmitRunResult,
)
from domain.run.model.errors import RunConflictError, RunNotFoundError, RunStateError
from domain.run.ports import RunRepository
from domain.session import SessionNotFoundError
from domain.skill import SkillConflictError
from domain.tool import (
    MAX_TOOL_CALL_ID_LENGTH,
    DecideToolApprovalCommand,
    RunToolCallDetails,
    RunToolCallPage,
    ToolApprovalDecision,
)

MAX_RUN_TEXT_LENGTH = 32_000
MAX_RUN_REQUEST_ID_LENGTH = 128
MAX_RUN_LIST_SIZE = 100
MAX_EVENT_PAGE_SIZE = 100
CURRENT_PROMPT_TEMPLATE_VERSION = "run-v2"


class InvalidRunInputError(ValueError):
    """Run HTTP 用例输入不符合业务约束。"""


def _utc_value(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _fingerprint(*, operation: str, fields: dict[str, str]) -> str:
    content = json.dumps(
        {"operation": operation, **fields},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _validate_text(text: str) -> str:
    if not isinstance(text, str):
        raise InvalidRunInputError("run_text_invalid")
    normalized = text.strip()
    if (
        not normalized
        or len(normalized) > MAX_RUN_TEXT_LENGTH
        or any(
            unicodedata.category(character) == "Cc" and character not in "\n\r\t"
            for character in normalized
        )
    ):
        raise InvalidRunInputError("run_text_invalid")
    return text


def _validate_request_id(request_id: str) -> str:
    if (
        not isinstance(request_id, str)
        or not request_id.strip()
        or len(request_id) > MAX_RUN_REQUEST_ID_LENGTH
        or any(unicodedata.category(character) == "Cc" for character in request_id)
    ):
        raise InvalidRunInputError("run_request_id_invalid")
    return request_id


def _validate_resource_ids(values: tuple[str, ...], *, code: str) -> tuple[str, ...]:
    """校验可选技能/文件 ID 列表，拒绝重复、空白和控制字符。"""
    if len(values) > 100 or len(values) != len(set(values)):
        raise InvalidRunInputError(code)
    if any(
        not value
        or len(value) > 128
        or any(unicodedata.category(character) == "Cc" for character in value)
        for value in values
    ):
        raise InvalidRunInputError(code)
    return values


def _encode_cursor(run: Run) -> str:
    value = f"{run.created_at.isoformat()}|{run.id}".encode()
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[datetime | None, str | None]:
    if cursor is None:
        return None, None
    if not cursor or len(cursor) > 512:
        raise InvalidRunInputError("run_cursor_invalid")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        timestamp, run_id = decoded.rsplit("|", 1)
        parsed = datetime.fromisoformat(timestamp)
        if not run_id or len(run_id) > 128:
            raise ValueError
        return _utc_value(parsed), run_id
    except (ValueError, UnicodeError) as error:
        raise InvalidRunInputError("run_cursor_invalid") from error


class RunService:
    """构造 typed Run 命令并将仓储结果整理为应用结果。"""

    def __init__(
        self,
        repository: RunRepository,
        *,
        id_factory: Callable[[str], str] = new_id,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self._repository = repository
        self._id_factory = id_factory
        self._clock = clock

    async def submit(
        self,
        session_id: str,
        owner_id: str,
        request_id: str,
        text: str,
        *,
        skill_ids: tuple[str, ...] = (),
        input_file_ids: tuple[str, ...] = (),
        auth_session_id: str | None = None,
    ) -> SubmitRunResult:
        request_id = _validate_request_id(request_id)
        text = _validate_text(text)
        skill_ids = _validate_resource_ids(skill_ids, code="run_skill_ids_invalid")
        input_file_ids = _validate_resource_ids(
            input_file_ids,
            code="run_input_file_ids_invalid",
        )
        run_id = self._id_factory("run")
        message_id = self._id_factory("msg")
        now = _utc_value(self._clock())
        draft = ConversationMessageDraft(
            id=message_id,
            session_id=session_id,
            owner_id=owner_id,
            role=ConversationRole.USER,
            status=MessageStatus.COMPLETED,
            parts=(TextPart(text),),
            reply_to_message_id=None,
            run_id=run_id,
            client_message_id=None,
            created_at=now,
            completed_at=now,
            metadata=MessageMetadata(),
            context_turn_id=message_id,
        )
        try:
            return await self._repository.submit(
                session_id,
                owner_id,
                SubmitRunCommand(
                    run_id=run_id,
                    request_id=request_id,
                    fingerprint=_fingerprint(
                        operation="submit",
                        fields={
                            "session_id": session_id,
                            "text": text,
                            "skill_ids": json.dumps(
                                skill_ids,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            "input_file_ids": json.dumps(
                                input_file_ids,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    ),
                    prompt_template_version=CURRENT_PROMPT_TEMPLATE_VERSION,
                    message=draft,
                    queued_at=now,
                    selected_skill_ids=skill_ids,
                    input_files=RunInputFiles(file_ids=input_file_ids),
                    auth_session_id=auth_session_id,
                ),
            )
        except SkillConflictError as error:
            raise RunConflictError(str(error)) from error

    async def answer(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        input_request_id: str,
        request_id: str,
        text: str,
        *,
        input_file_ids: tuple[str, ...] = (),
        auth_session_id: str | None = None,
    ) -> Run:
        input_request_id = _validate_request_id(input_request_id)
        request_id = _validate_request_id(request_id)
        text = _validate_text(text)
        input_file_ids = _validate_resource_ids(
            input_file_ids,
            code="run_input_file_ids_invalid",
        )
        run = await self._repository.get(session_id, owner_id, run_id)
        if run is None:
            raise RunNotFoundError("run_not_found")
        input_call = await self._repository.get_input_call(
            session_id, owner_id, run_id, input_request_id
        )
        if input_call is None:
            raise RunStateError("run_input_call_reference_invalid")
        now = _utc_value(self._clock())
        answer = ConversationMessageDraft(
            id=self._id_factory("msg"),
            session_id=session_id,
            owner_id=owner_id,
            role=ConversationRole.USER,
            status=MessageStatus.COMPLETED,
            parts=(TextPart(text),),
            reply_to_message_id=None,
            run_id=run_id,
            client_message_id=None,
            created_at=now,
            completed_at=now,
            metadata=MessageMetadata(),
            context_turn_id=run.trigger_message_id,
        )
        execution = ExecutionMessageDraft(
            id=self._id_factory("exec"),
            session_id=session_id,
            owner_id=owner_id,
            run_id=run_id,
            thread_id=input_call.thread_id,
            role=ExecutionRole.TOOL,
            parts=(
                ToolResultPart(
                    tool_call_id=input_call.tool_call_id,
                    status=ToolResultStatus.SUCCESS,
                    output=text,
                ),
            ),
            tool_call_id=input_call.tool_call_id,
            source_message_id=None,
            created_at=now,
            metadata=MessageMetadata(),
        )
        return await self._repository.answer(
            session_id,
            owner_id,
            AnswerRunCommand(
                run_id=run_id,
                input_request_id=input_request_id,
                request_id=request_id,
                fingerprint=_fingerprint(
                    operation="answer",
                    fields={
                        "session_id": session_id,
                        "run_id": run_id,
                        "input_request_id": input_request_id,
                        "text": text,
                        "input_file_ids": json.dumps(
                            input_file_ids,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                ),
                answer_message=answer,
                execution_message=execution,
                queued_at=now,
                input_files=RunInputFiles(file_ids=input_file_ids),
                auth_session_id=auth_session_id,
            ),
        )

    async def cancel(self, session_id: str, owner_id: str, run_id: str) -> Run:
        result = await self._repository.cancel(
            session_id, owner_id, run_id, _utc_value(self._clock())
        )
        if result is None:
            raise RunNotFoundError("run_not_found")
        return result

    async def retry(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        request_id: str,
        *,
        confirm_unknown: bool = False,
        auth_session_id: str | None = None,
    ) -> Run:
        request_id = _validate_request_id(request_id)
        new_run_id = self._id_factory("run")
        now = _utc_value(self._clock())
        try:
            result = await self._repository.retry(
                session_id=session_id,
                owner_id=owner_id,
                source_run_id=run_id,
                request_id=request_id,
                fingerprint=_fingerprint(
                    operation="retry", fields={"session_id": session_id, "run_id": run_id}
                ),
                new_run_id=new_run_id,
                prompt_template_version=CURRENT_PROMPT_TEMPLATE_VERSION,
                now=now,
                confirm_unknown=confirm_unknown,
                auth_session_id=auth_session_id,
            )
        except SkillConflictError as error:
            raise RunConflictError(str(error)) from error
        if result is None:
            raise RunNotFoundError("run_not_found")
        return result

    async def get(self, session_id: str, owner_id: str, run_id: str) -> RunDetailsResult:
        run = await self._repository.get(session_id, owner_id, run_id)
        if run is None:
            raise RunNotFoundError("run_not_found")
        pending = await self._repository.get_pending_input(session_id, owner_id, run_id)
        pending_approvals = await self._repository.list_pending_approvals(
            session_id, owner_id, run_id
        )
        if pending_approvals is None:
            raise RunNotFoundError("run_not_found")
        return RunDetailsResult(run, pending, pending_approvals)

    async def decide_tool_approval(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        call_id: str,
        request_id: str,
        decision: ToolApprovalDecision,
        *,
        auth_session_id: str | None = None,
    ) -> Run:
        """Apply one owner-scoped tool approval decision with an idempotency key."""
        request_id = _validate_request_id(request_id)
        if not call_id or len(call_id) > MAX_TOOL_CALL_ID_LENGTH:
            raise InvalidRunInputError("run_tool_call_id_invalid")
        if not run_id or len(run_id) > 128:
            raise InvalidRunInputError("run_id_invalid")
        result = await self._repository.decide_tool_approval(
            DecideToolApprovalCommand(
                session_id=session_id,
                owner_id=owner_id,
                run_id=run_id,
                record_id=call_id,
                request_id=request_id,
                fingerprint=_fingerprint(
                    operation="decide_tool_approval",
                    fields={
                        "session_id": session_id,
                        "run_id": run_id,
                        "call_id": call_id,
                        "decision": decision.value,
                    },
                ),
                decision=decision,
                decided_at=_utc_value(self._clock()),
                auth_session_id=auth_session_id,
            )
        )
        if result is None:
            raise RunNotFoundError("run_not_found")
        return result

    async def list(
        self, session_id: str, owner_id: str, *, cursor: str | None, limit: int
    ) -> RunListResult:
        if not 1 <= limit <= MAX_RUN_LIST_SIZE:
            raise InvalidRunInputError("run_list_limit_invalid")
        before_created_at, before_id = _decode_cursor(cursor)
        items = await self._repository.list_runs(
            session_id,
            owner_id,
            before_created_at=before_created_at,
            before_id=before_id,
            limit=limit + 1,
        )
        if items is None:
            raise SessionNotFoundError("session_not_found")
        has_more = len(items) > limit
        visible = items[:limit]
        return RunListResult(
            visible,
            _encode_cursor(visible[-1]) if has_more and visible else None,
        )

    async def events(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        *,
        after_sequence: int,
        limit: int,
    ) -> tuple[RunEvent, ...]:
        if after_sequence < 0 or not 1 <= limit <= MAX_EVENT_PAGE_SIZE:
            raise InvalidRunInputError("run_event_page_invalid")
        await self.get(session_id, owner_id, run_id)
        return await self._repository.list_events(
            session_id,
            owner_id,
            run_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    async def list_tool_calls(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        *,
        after_ordinal: int,
        limit: int,
    ) -> RunToolCallPage:
        """Read one ordinal-ordered, owner-visible call page."""
        if after_ordinal < 0 or not 1 <= limit <= MAX_EVENT_PAGE_SIZE:
            raise InvalidRunInputError("run_tool_call_page_invalid")
        page = await self._repository.list_tool_calls(
            session_id,
            owner_id,
            run_id,
            after_ordinal=after_ordinal,
            limit=limit,
        )
        if page is None:
            raise RunNotFoundError("run_not_found")
        return page

    async def get_tool_call(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        call_id: str,
    ) -> RunToolCallDetails:
        """Read bounded arguments and normalized output for one call."""
        if not call_id or len(call_id) > MAX_TOOL_CALL_ID_LENGTH:
            raise InvalidRunInputError("run_tool_call_id_invalid")
        details = await self._repository.get_tool_call(
            session_id,
            owner_id,
            run_id,
            call_id,
        )
        if details is None:
            raise RunNotFoundError("run_tool_call_not_found")
        return details


__all__ = ["InvalidRunInputError", "RunService"]
