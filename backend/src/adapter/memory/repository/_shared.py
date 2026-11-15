"""记忆生命周期仓储子模块共用的锁辅助、投影与持久化基础方法。"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, TypeAdapter
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from adapter.memory.model import (
    MemoryJobModel,
    MemoryModel,
    MemoryScopeStateModel,
    MemorySettingModel,
    MemorySourceModel,
)
from adapter.memory.tx_helpers import (
    disable_project_memory_scope as disable_project_memory_scope,
)
from adapter.memory.tx_helpers import (
    enqueue_cleanup_job as enqueue_cleanup_job,
)
from adapter.memory.tx_helpers import (
    invalidate_session_memory_generation as invalidate_session_memory_generation,
)
from adapter.memory.tx_helpers import (
    lock_scope_state as lock_scope_state,
)
from adapter.memory.tx_helpers import (
    lock_scope_states as lock_scope_states,
)
from adapter.memory.tx_helpers import (
    lock_user_memory_setting as lock_user_memory_setting,
)
from adapter.message.model import ConversationMessageModel
from adapter.run.model import RunModel
from common.date import as_utc
from domain.memory.lifecycle import (
    ExtractBatchPayload,
    ExtractionStatus,
    JobKind,
    JobStatus,
    MemoryJob,
    MemoryJobPayload,
    MemoryJobResult,
    MemorySourceMessage,
    PreparedMemoryCreate,
)
from domain.message import TextPart


class _PayloadEnvelope(BaseModel):
    """仅供 TypeAdapter 校验任务 payload 判别联合的根对象。"""

    model_config = ConfigDict(extra="forbid")

    payload: MemoryJobPayload


class _ResultEnvelope(BaseModel):
    """仅供 TypeAdapter 校验任务结果判别联合的根对象。"""

    model_config = ConfigDict(extra="forbid")

    result: MemoryJobResult


_PAYLOAD_ADAPTER: TypeAdapter[_PayloadEnvelope] = TypeAdapter(_PayloadEnvelope)
_RESULT_ADAPTER: TypeAdapter[_ResultEnvelope] = TypeAdapter(_ResultEnvelope)


class MemoryBatchDiscarded(ValueError):
    """批次因来源或 scope 已失效而整体丢弃。"""


def _json_value(value: object) -> dict[str, object]:
    """把严格 Pydantic 模型投影为 JSONB 对象。"""
    if hasattr(value, "model_dump"):
        dumped = cast(Any, value).model_dump(mode="json")
        if isinstance(dumped, dict):
            return cast(dict[str, object], dumped)
    raise ValueError("memory_json_projection_invalid")


def _job_payload(value: dict[str, object]) -> MemoryJobPayload:
    """在持久化边界重新校验任务内容的 kind 判别联合。"""
    return _PAYLOAD_ADAPTER.validate_python({"payload": value}).payload


def _job_result(value: dict[str, object]) -> MemoryJobResult:
    """重新校验任务结果，拒绝混装不适用字段。"""
    return _RESULT_ADAPTER.validate_python({"result": value}).result


def _job_to_domain(row: MemoryJobModel) -> MemoryJob:
    """把任务 ORM 行转换为领域快照。"""
    return MemoryJob(
        id=row.id,
        dedupe_key=row.dedupe_key,
        kind=JobKind(row.kind),
        status=JobStatus(row.status),
        payload=_job_payload(row.payload),
        result=row.result,
        namespace_id=row.namespace_id,
        scope_key=row.scope_key,
        attempt_count=row.attempt_count,
        schema_retry_count=row.schema_retry_count,
        available_at=as_utc(row.available_at),
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
    )


def _normalized_content_hash(content: str) -> str:
    """使用稳定空白归一计算正文辅助摘要，不替代事实身份。"""
    canonical = " ".join(content.split())
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _message_text(parts: list[dict[str, object]]) -> str:
    """只提取可信用户消息中的文本块。"""
    from adapter.message.converter.message import deserialize_parts

    try:
        decoded = deserialize_parts(parts)
    except ValueError:
        return ""
    return "".join(part.text for part in decoded if isinstance(part, TextPart))


def _source_message(row: ConversationMessageModel) -> MemorySourceMessage:
    """把会话消息转换为无模型的来源证据快照。"""
    source_at = row.completed_at or row.created_at
    return MemorySourceMessage(
        message_id=row.id,
        text=_message_text(row.parts),
        source_at=as_utc(source_at),
    )


def _scope_key(project_id: str | None) -> str:
    """把固定项目身份映射为唯一 scope。"""
    return "personal" if project_id is None else f"project:{project_id}"



async def read_user_memory_generation(
    session: AsyncSession,
    namespace_id: str,
) -> int:
    """读取用户当前 generation；尚无设置行时使用默认值一。"""
    generation = await session.scalar(
        select(MemorySettingModel.generation).where(MemorySettingModel.namespace_id == namespace_id)
    )
    return 1 if generation is None else generation







async def _mark_user_runs_skipped_locked(
    session: AsyncSession,
    *,
    owner_id: str,
    safe_error_code: str,
    now: datetime,
) -> None:
    """在关闭自动提取等全局开关时跳过该用户待提取或尚未终态的 Run。"""
    rows = (
        await session.scalars(
            select(RunModel)
            .where(
                RunModel.owner_id == owner_id,
                or_(
                    RunModel.memory_extraction_status.is_(None),
                    RunModel.memory_extraction_status == ExtractionStatus.PENDING.value,
                    RunModel.finished_at.is_(None),
                ),
            )
            .order_by(RunModel.id)
            .with_for_update()
        )
    ).all()
    for row in rows:
        if row.memory_extraction_status in {
            None,
            ExtractionStatus.PENDING.value,
        }:
            row.memory_extraction_status = ExtractionStatus.SKIPPED.value
            row.memory_extraction_safe_error_code = safe_error_code
        if row.finished_at is not None:
            row.memory_extracted_at = now
        row.updated_at = now


async def _mark_batch_runs_locked(
    session: AsyncSession,
    batch: ExtractBatchPayload,
    *,
    outcome: ExtractionStatus,
    safe_error_code: str | None,
    now: datetime,
) -> None:
    """在一个事务内收敛整个固定批次对应的 Run。"""
    run_ids = tuple(item.run_id for item in batch.members)
    rows = (
        await session.scalars(
            select(RunModel).where(RunModel.id.in_(run_ids)).order_by(RunModel.id).with_for_update()
        )
    ).all()
    for row in rows:
        if row.owner_id != batch.owner_id:
            continue
        row.memory_extraction_status = outcome.value
        row.memory_extracted_at = now
        row.memory_extraction_batch_id = batch.batch_id
        row.memory_extraction_safe_error_code = safe_error_code
        row.updated_at = now




async def insert_prepared_memory(
    session: AsyncSession,
    create: PreparedMemoryCreate,
    now: datetime,
) -> bool:
    """插入一条正文记忆；同请求重放返回 False 而不重复来源。"""
    content_hash = _normalized_content_hash(create.content)
    row_id = await session.scalar(
        insert(MemoryModel)
        .values(
            id=create.memory_id,
            namespace_id=create.namespace_id,
            scope_key=create.scope_key,
            content=create.content,
            search_document=func.to_tsvector(
                "athena_memory_chinese",
                create.content,
            ),
            embedding=list(create.embedding),
            embedding_profile_id=create.embedding_profile_id,
            request_id=create.request_id,
            request_hash=content_hash,
            status="active",
            origin=create.origin,
            version=1,
            expires_at=create.expires_at,
            kind=create.kind.value,
            load_policy=create.load_policy.value,
            content_hash=content_hash,
            explicit_long_term=create.explicit_long_term,
            last_evidenced_at=create.last_evidenced_at,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing()
        .returning(MemoryModel.id)
    )
    if row_id is None:
        return False
    await append_evidence_sources(
        session,
        create.memory_id,
        create.source_evidence,
    )
    return True

async def append_evidence_sources(
    session: AsyncSession,
    memory_id: str,
    evidence: tuple[MemorySourceMessage, ...],
) -> None:
    """按消息 ID 去重追加来源，不重复延长证据时间。"""
    if not evidence:
        return
    existing = set(
        (
            await session.scalars(
                select(MemorySourceModel.source_ref).where(
                    MemorySourceModel.memory_id == memory_id,
                    MemorySourceModel.source_kind == "message",
                )
            )
        ).all()
    )
    for item in evidence:
        if item.message_id in existing:
            continue
        session.add(
            MemorySourceModel(
                id=f"msrc_{hashlib.sha256(f'{memory_id}:message:{item.message_id}'.encode()).hexdigest()[:24]}",
                memory_id=memory_id,
                source_kind="message",
                source_ref=item.message_id,
                source_at=item.source_at,
            )
        )
        existing.add(item.message_id)

async def copy_evidence_sources(
    session: AsyncSession,
    *,
    source_memory_id: str,
    target_memory_id: str,
) -> None:
    """把旧行来源复制到替代行，保证新正文仍可追溯全部历史条件。"""
    existing = {
        (source_kind, source_ref)
        for source_kind, source_ref in (
            await session.execute(
                select(
                    MemorySourceModel.source_kind,
                    MemorySourceModel.source_ref,
                ).where(MemorySourceModel.memory_id == target_memory_id)
            )
        ).all()
    }
    sources = (
        await session.scalars(
            select(MemorySourceModel)
            .where(MemorySourceModel.memory_id == source_memory_id)
            .order_by(
                MemorySourceModel.source_kind,
                MemorySourceModel.source_ref,
            )
        )
    ).all()
    for source in sources:
        source_key = (source.source_kind, source.source_ref)
        if source_key in existing:
            continue
        session.add(
            MemorySourceModel(
                id=(
                    "msrc_"
                    + hashlib.sha256(
                        (
                            f"{target_memory_id}:{source.source_kind}:{source.source_ref}"
                        ).encode()
                    ).hexdigest()[:24]
                ),
                memory_id=target_memory_id,
                source_kind=source.source_kind,
                source_ref=source.source_ref,
                source_at=source.source_at,
            )
        )
        existing.add(source_key)

async def preserve_replacement_evidence_time(
    session: AsyncSession,
    *,
    memory_id: str,
    create: PreparedMemoryCreate,
    target: MemoryModel,
) -> None:
    """替代行的证据时间不得早于新证据、旧目标或已复制来源。"""
    copied_source_at = await session.scalar(
        select(func.max(MemorySourceModel.source_at)).where(
            MemorySourceModel.memory_id == memory_id
        )
    )
    evidence_times = [create.last_evidenced_at]
    if target.last_evidenced_at is not None:
        evidence_times.append(target.last_evidenced_at)
    if copied_source_at is not None:
        evidence_times.append(copied_source_at)
    await session.execute(
        update(MemoryModel)
        .where(MemoryModel.id == memory_id)
        .values(last_evidenced_at=max(evidence_times))
    )

async def tombstone_memory(
    session: AsyncSession,
    row: MemoryModel,
    now: datetime,
) -> None:
    """按白名单清空正文、来源和全部内容派生字段。"""
    row.content = ""
    row.search_document = func.to_tsvector("athena_memory_chinese", "")
    row.embedding = None
    row.embedding_profile_id = None
    row.origin = None
    row.kind = None
    row.load_policy = None
    row.content_hash = None
    row.explicit_long_term = None
    row.last_evidenced_at = None
    row.expires_at = None
    row.superseded_by_id = None
    row.version += 1
    row.deleted_time = now
    row.updated_at = now
    await session.execute(
        delete(MemorySourceModel).where(MemorySourceModel.memory_id == row.id)
    )



def enabled_scope_exists(namespace_id: str, scope_key: str) -> Any:
    """构造 scope 启用校验子查询。"""
    return (
        select(MemoryScopeStateModel.scope_key)
        .where(
            MemoryScopeStateModel.namespace_id == namespace_id,
            MemoryScopeStateModel.scope_key == scope_key,
            MemoryScopeStateModel.enabled.is_(True),
        )
        .exists()
    )

def enabled_memory_scope() -> Any:
    """为检索行关联同 namespace/scope 的启用状态。"""
    return (
        select(MemoryScopeStateModel.scope_key)
        .where(
            MemoryScopeStateModel.namespace_id == MemoryModel.namespace_id,
            MemoryScopeStateModel.scope_key == MemoryModel.scope_key,
            MemoryScopeStateModel.enabled.is_(True),
        )
        .exists()
    )
