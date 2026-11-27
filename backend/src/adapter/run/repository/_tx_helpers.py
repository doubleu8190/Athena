"""Run 仓储共享的无状态事务辅助函数：锁序、租约、事件、幂等请求、模型快照与用量聚合。"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from adapter.file.model import SessionFileModel
from adapter.llm.model import (
    CurrentCompressionPolicyModel,
    DefaultModelProfileModel,
    ModelProfileVersionModel,
)
from adapter.message.converter import conversation_to_domain
from adapter.message.model import ConversationMessageModel
from adapter.run.converter import event_payload_to_json, run_to_domain
from adapter.run.model import (
    RunEventModel,
    RunInputRequestModel,
    RunModel,
    RunRequestModel,
)
from adapter.session.model import SessionModel
from common.date import as_utc
from common.ids import new_id
from domain.message import ConversationMessage, SessionNotFoundError
from domain.run.model import (
    InputRequestStatus,
    Run,
    RunAttemptUsage,
    RunEventPayload,
    RunRequestOperation,
    RunRequestResult,
    RunUsage,
)
from domain.run.model.errors import (
    RunConflictError,
    RunInputResourceError,
    RunNotFoundError,
    RunStateError,
)


def _purpose_total(attempts: tuple[RunAttemptUsage, ...], purpose: str) -> int | None:
    """按调用用途汇总已知 Token 数。"""
    values = [
        item.total_tokens
        for item in attempts
        if item.purpose == purpose and item.total_tokens is not None
    ]
    return None if not values else sum(values)


def aggregate_usage(attempts: tuple[RunAttemptUsage, ...]) -> RunUsage:
    """将多条模型调用尝试记录汇总为单一 RunUsage。"""

    def total(field: str) -> int | None:
        values = [getattr(item, field) for item in attempts]
        return (
            None
            if not any(value is not None for value in values)
            else sum(value for value in values if value is not None)
        )

    return RunUsage(
        input_tokens=total("input_tokens"),
        output_tokens=total("output_tokens"),
        total_tokens=total("total_tokens"),
        complete=bool(attempts) and all(item.complete for item in attempts),
        attempts=attempts,
        answer_tokens=_purpose_total(attempts, "answer"),
        compression_tokens=_purpose_total(attempts, "compression"),
    )


def merge_usage(current_json: dict[str, object], incoming: RunUsage) -> RunUsage:
    """按尝试键单次幂等去重合并模型调用用量并重新汇总各维度计数。"""
    current = RunUsage.model_validate(current_json)
    if not incoming.attempts:
        if any(
            value is not None
            for value in (incoming.input_tokens, incoming.output_tokens, incoming.total_tokens)
        ):
            raise RunConflictError("run_usage_attempts_required")
        return current
    by_key = {
        (
            item.execution_epoch,
            item.purpose,
            item.operation_id,
            item.batch_index,
            item.attempt,
        ): item
        for item in current.attempts
    }
    changed = False
    for item in incoming.attempts:
        key = (
            item.execution_epoch,
            item.purpose,
            item.operation_id,
            item.batch_index,
            item.attempt,
        )
        existing = by_key.get(key)
        if existing is not None:
            if existing != item:
                raise RunConflictError("run_usage_attempt_conflict")
            continue
        by_key[key] = item
        changed = True
    if not changed:
        return current
    keys = sorted(
        by_key,
        key=lambda item: (item[0], item[1], item[2] or "", item[3], item[4]),
    )
    return aggregate_usage(tuple(by_key[key] for key in keys))


def sync_llm_request_count(row: RunModel, llm_request_count: int | None) -> None:
    """单调递增持久化进程内已消耗的 LLM 请求次数，严格守住 DB CHECK 上限（<= 50）。"""
    if llm_request_count is None:
        return
    if not 0 <= llm_request_count <= 50:
        raise ValueError("run_llm_request_count_invalid")
    if llm_request_count > row.llm_request_count:
        row.llm_request_count = llm_request_count


def build_model_snapshot_json(profile: ModelProfileVersionModel) -> dict[str, object]:
    """构造创建 Run 时的非密钥模型元数据快照，严禁写入任何明文或密文 api_key。"""
    return {
        "id": profile.id,
        "name": profile.name,
        "protocol": profile.protocol,
        "base_url": profile.base_url,
        "model": profile.model,
        "context_window_tokens": profile.context_window_tokens,
        "tokenizer_encoding": profile.tokenizer_encoding,
        "supports_stream": bool(profile.supports_stream),
        "supports_tools": bool(profile.supports_tools),
        "supports_parallel_tool_calls": bool(profile.supports_parallel_tool_calls),
        "max_output_tokens": profile.max_output_tokens,
        "request_timeout_seconds": profile.request_timeout_seconds,
        "execution_timeout_seconds": profile.execution_timeout_seconds,
        "max_concurrency": profile.max_concurrency,
        "temperature": profile.temperature,
        "scenario_label": profile.scenario_label,
        "supports_vision": bool(profile.supports_vision),
        "version": int(profile.version),
    }


def is_usable_profile(
    profile: ModelProfileVersionModel | None,
    *,
    require_visible: bool = False,
) -> bool:
    """统一校验模型版本未软删、已启用、支持流式与工具调用及必要可见性。"""
    if (
        profile is None
        or profile.deleted_at is not None
        or not profile.enabled
        or not profile.supports_stream
        or not profile.supports_tools
    ):
        return False
    if require_visible and not profile.visible_to_users:
        return False
    return True


def validate_usable_profile(
    profile: ModelProfileVersionModel | None,
    *,
    error: Exception,
    require_visible: bool = False,
) -> ModelProfileVersionModel:
    """复核模型版本可用性，不满足条件时抛出指定领域异常。"""
    if not is_usable_profile(profile, require_visible=require_visible):
        raise error
    assert profile is not None
    return profile


async def load_default_profile_row(session: AsyncSession) -> ModelProfileVersionModel:
    """单次无锁 JOIN 读取默认模型版本并复核可用性。"""
    profile = await session.scalar(
        select(ModelProfileVersionModel)
        .join(
            DefaultModelProfileModel,
            DefaultModelProfileModel.profile_id == ModelProfileVersionModel.id,
        )
        .where(
            DefaultModelProfileModel.singleton_id == 1,
            ModelProfileVersionModel.deleted_at.is_(None),
        )
    )
    return validate_usable_profile(
        profile,
        error=RunStateError("llm_default_profile_unavailable"),
    )


async def load_selected_profile_row(
    session: AsyncSession, profile_id: str
) -> ModelProfileVersionModel:
    """无锁读取用户显式选择的模型版本，复核可见、启用、未删及能力/凭据完整。"""
    profile = await session.scalar(
        select(ModelProfileVersionModel).where(
            ModelProfileVersionModel.id == profile_id,
            ModelProfileVersionModel.deleted_at.is_(None),
        )
    )
    return validate_usable_profile(
        profile,
        error=RunInputResourceError("model_profile_unavailable"),
        require_visible=True,
    )


async def load_retry_profile_row(
    session: AsyncSession, profile_id: str
) -> ModelProfileVersionModel:
    """无锁复核原 Run 绑定的模型版本；若已隐藏仅当仍为当前默认模型时允许重试。"""
    profile = await session.scalar(
        select(ModelProfileVersionModel).where(
            ModelProfileVersionModel.id == profile_id,
            ModelProfileVersionModel.deleted_at.is_(None),
        )
    )
    validated = validate_usable_profile(
        profile,
        error=RunInputResourceError("model_profile_unavailable"),
    )
    if not validated.visible_to_users:
        default_profile_id = await session.scalar(
            select(DefaultModelProfileModel.profile_id).where(
                DefaultModelProfileModel.singleton_id == 1
            )
        )
        if default_profile_id != validated.id:
            raise RunInputResourceError("model_profile_unavailable")
    return validated


async def current_compression_policy_id(session: AsyncSession) -> str | None:
    """读取当前策略版本，供新 Run 固定配置快照。"""
    return await session.scalar(
        select(CurrentCompressionPolicyModel.policy_version_id).where(
            CurrentCompressionPolicyModel.singleton_id == 1
        )
    )


async def database_now(session: AsyncSession) -> datetime:
    """读取 PostgreSQL 当前时钟并统一归约至 UTC 时区。"""
    db_now = cast(datetime | None, await session.scalar(select(func.clock_timestamp())))
    if db_now is None:
        raise RuntimeError("run_database_clock_unavailable")
    return as_utc(db_now)


async def validate_and_merge_input_file_ids(
    session: AsyncSession,
    session_id: str,
    owner_id: str,
    *,
    existing: tuple[str, ...],
    incoming: tuple[str, ...],
) -> tuple[str, ...]:
    """在同一 Run 事务中验证文件归属，并按首次提供顺序合并。"""
    if len(incoming) != len(set(incoming)):
        raise RunInputResourceError("run_input_file_ids_duplicate")
    ordered = tuple(dict.fromkeys((*existing, *incoming)))
    if len(ordered) != len(existing) + len(incoming):
        raise RunInputResourceError("run_input_file_ids_duplicate")
    if not incoming:
        return ordered
    visible = set(
        (
            await session.scalars(
                select(SessionFileModel.id).where(
                    SessionFileModel.id.in_(incoming),
                    SessionFileModel.session_id == session_id,
                    SessionFileModel.owner_id == owner_id,
                )
            )
        ).all()
    )
    if visible != set(incoming):
        raise RunInputResourceError("run_input_file_unavailable")
    return ordered


async def lock_session(session: AsyncSession, session_id: str, owner_id: str) -> SessionModel:
    """按 owner 归属锁定未软删会话行（FOR UPDATE）。"""
    row = await session.scalar(
        select(SessionModel)
        .where(
            SessionModel.id == session_id,
            SessionModel.owner_id == owner_id,
            SessionModel.deleted_time.is_(None),
        )
        .with_for_update()
    )
    if row is None:
        raise SessionNotFoundError("session_not_found")
    return row


async def lock_session_by_id(session: AsyncSession, session_id: str) -> SessionModel:
    """按会话 ID 锁定未软删会话行（FOR UPDATE）。"""
    row = await session.scalar(
        select(SessionModel)
        .where(SessionModel.id == session_id, SessionModel.deleted_time.is_(None))
        .with_for_update()
    )
    if row is None:
        raise SessionNotFoundError("session_not_found")
    return row


async def lock_run(session: AsyncSession, session_id: str, run_id: str) -> RunModel:
    """在已锁会话前提下锁定 Run 行（Session -> Run 锁序）。"""
    row = await session.scalar(
        select(RunModel)
        .where(RunModel.id == run_id, RunModel.session_id == session_id)
        .with_for_update()
    )
    if row is None:
        raise RunNotFoundError("run_not_found")
    return row


async def lock_worker_run(
    session: AsyncSession, run_id: str, worker_id: str, execution_epoch: int
) -> RunModel:
    """按 Session -> Run 锁序加锁并执行 Worker 租约 fencing 校验。"""
    session_id = await session.scalar(select(RunModel.session_id).where(RunModel.id == run_id))
    if session_id is None:
        raise RunNotFoundError("run_not_found")
    await lock_session_by_id(session, session_id)
    row = await lock_run(session, session_id, run_id)
    if row.worker_id != worker_id or row.execution_epoch != execution_epoch:
        raise RunStateError("run_execution_lease_lost")
    return row


async def lock_worker_run_only(
    session: AsyncSession, run_id: str, worker_id: str, execution_epoch: int
) -> RunModel:
    """仅锁 runs 单行并校验租约归属（不锁父表 sessions），消除微操作与同会话操作锁争用。"""
    row = await session.scalar(select(RunModel).where(RunModel.id == run_id).with_for_update())
    if row is None:
        raise RunNotFoundError("run_not_found")
    if row.worker_id != worker_id or row.execution_epoch != execution_epoch:
        raise RunStateError("run_execution_lease_lost")
    return row


async def get_visible_run(
    session: AsyncSession, session_id: str, owner_id: str, run_id: str
) -> RunModel | None:
    """在未软删会话与所有者归属约束下读取可见 Run，供只读查询复用。"""
    return await session.scalar(
        select(RunModel)
        .join(SessionModel, SessionModel.id == RunModel.session_id)
        .where(
            RunModel.id == run_id,
            RunModel.session_id == session_id,
            RunModel.owner_id == owner_id,
            SessionModel.owner_id == owner_id,
            SessionModel.deleted_time.is_(None),
        )
    )


def lease_is_valid(
    row: RunModel, worker_id: str, execution_epoch: int, db_now: datetime | None
) -> bool:
    """基于数据库时钟校验当前执行代次与租约有效期。"""
    return bool(
        db_now is not None
        and row.worker_id == worker_id
        and row.execution_epoch == execution_epoch
        and row.lease_expires_at is not None
        and row.lease_expires_at > db_now
    )


def work_deadline_is_valid(row: RunModel, db_now: datetime | None) -> bool:
    """判断当前执行是否仍可继续产生业务结果。"""
    return bool(
        db_now is not None
        and row.execution_deadline_at is not None
        and row.execution_deadline_at > db_now
    )


def clear_lease(row: RunModel) -> None:
    """清理 Run 行上的活跃 Worker 归属与执行期限字段。"""
    row.worker_id = None
    row.lease_expires_at = None
    row.execution_started_at = None
    row.execution_deadline_at = None


async def append_event(
    session: AsyncSession,
    run: RunModel,
    payload: RunEventPayload,
    created_at: datetime,
) -> None:
    """递增 Run 事件序号并同事务追加一条持久化事件。"""
    run.event_sequence += 1
    event = RunEventModel(
        id=new_id("runevt"),
        run_id=run.id,
        session_id=run.session_id,
        sequence=run.event_sequence,
        type=payload.type,
        created_at=created_at,
        payload=event_payload_to_json(payload),
    )
    session.add(event)
    await session.flush()


async def close_pending_input(session: AsyncSession, run: RunModel, closed_at: datetime) -> None:
    """在终态收敛事务内关闭仍处于 PENDING 状态的澄清输入请求。"""
    request = await session.scalar(
        select(RunInputRequestModel)
        .where(
            RunInputRequestModel.run_id == run.id,
            RunInputRequestModel.status == InputRequestStatus.PENDING.value,
        )
        .with_for_update()
    )
    if request is not None:
        request.status = InputRequestStatus.CLOSED.value
        request.closed_at = closed_at


async def find_request(
    session: AsyncSession,
    owner_id: str,
    session_id: str,
    operation: RunRequestOperation,
    request_id: str,
) -> RunRequestModel | None:
    """按操作类型与 request_id 查询已有幂等记录。"""
    return await session.scalar(
        select(RunRequestModel).where(
            RunRequestModel.owner_id == owner_id,
            RunRequestModel.session_id == session_id,
            RunRequestModel.operation == operation.value,
            RunRequestModel.request_id == request_id,
        )
    )


def check_fingerprint(row: RunRequestModel, fingerprint: str) -> None:
    """校验同一 request_id 重放时的参数指纹一致性。"""
    if row.fingerprint != fingerprint:
        raise RunConflictError("run_request_id_conflict")


async def store_request(
    session: AsyncSession,
    owner_id: str,
    session_id: str,
    operation: RunRequestOperation,
    request_id: str,
    fingerprint: str,
    result: RunRequestResult,
    created_at: datetime,
) -> None:
    """在同一事务内持久化幂等请求台账。"""
    session.add(
        RunRequestModel(
            id=new_id("runreq"),
            owner_id=owner_id,
            session_id=session_id,
            operation=operation.value,
            request_id=request_id,
            fingerprint=fingerprint,
            result=result.model_dump(mode="json", exclude_none=True),
            created_at=created_at,
        )
    )
    await session.flush()


async def load_submit_result(
    session: AsyncSession, request: RunRequestModel
) -> tuple[Run, ConversationMessage]:
    """加载 submit 幂等重放对应的 Run 与触发消息。"""
    result = RunRequestResult.model_validate(request.result)
    run = await session.get(RunModel, result.run_id)
    message = await session.get(ConversationMessageModel, result.message_id)
    if run is None or message is None:
        raise RunNotFoundError("run_request_result_missing")
    return run_to_domain(run), conversation_to_domain(message)
