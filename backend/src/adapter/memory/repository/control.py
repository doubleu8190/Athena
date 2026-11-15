"""记忆生命周期仓储：同步显式控制与幂等回执子模块。"""

from __future__ import annotations

import hashlib

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from adapter.auth.repository.transaction_guard import SqlAlchemyAuthTransactionGuard
from adapter.memory.model import (
    MemoryJobModel,
    MemoryModel,
    MemoryScopeStateModel,
)
from adapter.memory.repository._shared import (
    _job_payload,
    _job_result,
    _json_value,
    _MemoryRepositoryHelpersMixin,
    _normalized_content_hash,
    _scope_key,
    lock_scope_state,
    lock_user_memory_setting,
    read_user_memory_generation,
)
from adapter.message.model import ConversationMessageModel
from adapter.project.model import ProjectModel
from adapter.run.model import RunModel
from adapter.session.model import SessionModel
from common.date import as_utc, utc_now
from domain.memory.lifecycle import (
    ControlCommand,
    ControlJobResult,
    ControlPayload,
    ControlReceiptResult,
    JobKind,
    JobStatus,
    MemoryScopeState,
    PreparedMemoryCreate,
)
from domain.message import ConversationRole


class MemoryControlRepositoryMixin(_MemoryRepositoryHelpersMixin):
    """负责同步控制写入、无正文幂等回执以及 scope/generation 查询。"""

    async def apply_control(self, command: ControlCommand) -> ControlReceiptResult:
        """原子执行业务控制并写入无正文幂等回执。"""
        receipt = command.receipt
        dedupe_key = f"control:{receipt.run_id}:{receipt.tool_call_id}"
        now = utc_now()
        try:
            async with self._factory() as session, session.begin():
                setting = await lock_user_memory_setting(
                    session,
                    receipt.namespace_id,
                )
                existing = await session.scalar(
                    select(MemoryJobModel)
                    .where(MemoryJobModel.dedupe_key == dedupe_key)
                    .with_for_update()
                )
                if existing is not None:
                    return self._control_replay_result(existing, receipt)
                if command.decision_generation != setting.generation:
                    # 同步控制决策生成后已有其他控制或写入推进代次，旧控制计划拒绝。
                    raise ValueError("memory_control_generation_changed")
                # 同一事务锁定用户与登录会话；撤销提交后到达的写入不能越过鉴权复核。
                current_user = await SqlAlchemyAuthTransactionGuard(
                    session
                ).lock_current_session_user(
                    receipt.owner_id,
                    receipt.auth_session_id,
                )
                if current_user is None:
                    raise ValueError("memory_auth_invalid")
                state, _run = await self._lock_control_context(session, command)
                if not state.enabled:
                    raise ValueError("memory_scope_disabled")
                result = ControlReceiptResult(
                    operation=receipt.operation,
                    generation=setting.generation + 1,
                )
                affected_scope = command.access_scope_key
                if command.operation == "remember":
                    if command.target_memory_id is not None or command.expected_version is not None:
                        raise ValueError("memory_control_arguments_invalid")
                    create = self._control_create(command)
                    if create is None:
                        raise ValueError("memory_control_create_invalid")
                    if not await self._insert_prepared(session, create, now):
                        raise ValueError("memory_control_create_conflict")
                    result = result.model_copy(update={"memory_id": create.memory_id})
                elif command.operation == "correct":
                    if command.target_memory_id is None or command.expected_version is None:
                        raise ValueError("memory_control_target_invalid")
                    target = await self._lock_control_target(
                        session,
                        receipt.namespace_id,
                        command.access_scope_key,
                        command.target_memory_id,
                        command.expected_version,
                    )
                    create = self._control_create(command)
                    if create is None:
                        raise ValueError("memory_control_create_invalid")
                    if not await self._insert_prepared(session, create, now):
                        raise ValueError("memory_control_create_conflict")
                    await self._copy_evidence_sources(
                        session,
                        source_memory_id=target.id,
                        target_memory_id=create.memory_id,
                    )
                    await self._preserve_replacement_evidence_time(
                        session,
                        memory_id=create.memory_id,
                        create=create,
                        target=target,
                    )
                    target.status = "superseded"
                    target.superseded_by_id = create.memory_id
                    target.version += 1
                    target.updated_at = now
                    result = result.model_copy(
                        update={
                            "memory_id": create.memory_id,
                            "superseded_memory_id": target.id,
                        }
                    )
                elif command.operation == "forget":
                    if command.clear_scope:
                        rows = (
                            await session.scalars(
                                select(MemoryModel)
                                .where(
                                    MemoryModel.namespace_id == receipt.namespace_id,
                                    MemoryModel.scope_key == command.access_scope_key,
                                    MemoryModel.deleted_time.is_(None),
                                )
                                .order_by(MemoryModel.id)
                                .with_for_update()
                            )
                        ).all()
                        for row in rows:
                            await self._tombstone(session, row, now)
                    else:
                        if command.target_memory_id is None:
                            raise ValueError("memory_control_target_invalid")
                        target = await self._lock_control_target(
                            session,
                            receipt.namespace_id,
                            command.access_scope_key,
                            command.target_memory_id,
                            command.expected_version,
                        )
                        await self._tombstone(session, target, now)
                        result = result.model_copy(update={"memory_id": target.id})
                else:
                    raise ValueError("memory_control_operation_invalid")
                # 同步控制只推进用户 generation（供外部控制回执与控制并发检测），
                # 不再级联跳过其他无关的 pending/运行中自动提取 Run。
                setting.generation += 1
                result = result.model_copy(update={"generation": setting.generation})
                job_result = ControlJobResult(result=result)
                session.add(
                    MemoryJobModel(
                        id=f"mjob_{hashlib.sha256(dedupe_key.encode()).hexdigest()[:24]}",
                        dedupe_key=dedupe_key,
                        kind=JobKind.CONTROL.value,
                        status=JobStatus.DONE.value,
                        payload=_json_value(receipt),
                        result=_json_value(job_result),
                        namespace_id=receipt.namespace_id,
                        scope_key=affected_scope,
                        attempt_count=0,
                        schema_retry_count=0,
                        available_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
            return result
        except IntegrityError:
            winner = await self.control_receipt(
                run_id=receipt.run_id,
                tool_call_id=receipt.tool_call_id,
                arguments_digest=receipt.arguments_digest,
            )
            if winner is None:
                raise
            return winner

    async def control_receipt(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        arguments_digest: str,
    ) -> ControlReceiptResult | None:
        """按稳定工具键读取已完成控制回执，供响应丢失时重放。"""
        dedupe_key = f"control:{run_id}:{tool_call_id}"
        async with self._factory() as session:
            row = await session.scalar(
                select(MemoryJobModel).where(
                    MemoryJobModel.dedupe_key == dedupe_key,
                    MemoryJobModel.kind == JobKind.CONTROL.value,
                )
            )
            if row is None:
                return None
            try:
                payload = _job_payload(row.payload)
            except (ValidationError, ValueError) as error:
                raise ValueError("memory_control_receipt_unavailable") from error
            if (
                not isinstance(payload, ControlPayload)
                or payload.arguments_digest != arguments_digest
            ):
                raise ValueError("memory_control_idempotency_conflict")
            if row.status != JobStatus.DONE.value or row.result is None:
                raise ValueError("memory_control_receipt_unavailable")
            result = _job_result(row.result)
            if not isinstance(result, ControlJobResult):
                raise ValueError("memory_control_receipt_invalid")
            return result.result

    @staticmethod
    def _control_replay_result(
        row: MemoryJobModel,
        receipt: ControlPayload,
    ) -> ControlReceiptResult:
        """校验已完成控制回执的规范参数摘要并返回无正文结果。"""
        try:
            existing_payload = _job_payload(row.payload)
        except (ValidationError, ValueError) as error:
            raise ValueError("memory_control_receipt_unavailable") from error
        if (
            not isinstance(existing_payload, type(receipt))
            or existing_payload.arguments_digest != receipt.arguments_digest
        ):
            raise ValueError("memory_control_idempotency_conflict")
        if row.status != JobStatus.DONE.value or row.result is None:
            raise ValueError("memory_control_receipt_unavailable")
        parsed = _job_result(row.result)
        if not isinstance(parsed, ControlJobResult):
            raise ValueError("memory_control_receipt_invalid")
        return parsed.result

    async def _lock_control_context(
        self,
        session: AsyncSession,
        command: ControlCommand,
    ) -> tuple[MemoryScopeStateModel, RunModel]:
        """校验只读归属与来源存在性，并锁定需修改状态的 scope 与 Run。"""
        receipt = command.receipt
        hint = (
            await session.execute(
                select(
                    RunModel.session_id,
                    RunModel.memory_project_id,
                ).where(
                    RunModel.id == receipt.run_id,
                    RunModel.owner_id == receipt.owner_id,
                )
            )
        ).one_or_none()
        if hint is None:
            raise ValueError("memory_control_run_invalid")
        session_id, project_id = hint
        expected_scope = _scope_key(project_id)
        if command.access_scope_key != expected_scope:
            raise ValueError("memory_control_scope_invalid")
        if project_id is not None:
            # ProjectModel 为只读归属校验表，不加 FOR UPDATE 行锁。
            project = await session.scalar(
                select(ProjectModel).where(
                    ProjectModel.id == project_id,
                    ProjectModel.owner_id == receipt.owner_id,
                    ProjectModel.deleted_time.is_(None),
                )
            )
            if project is None:
                raise ValueError("memory_control_project_invalid")
        # SessionModel 为只读归属校验表，不加 FOR UPDATE 行锁。
        chat_session = await session.scalar(
            select(SessionModel).where(
                SessionModel.id == session_id,
                SessionModel.owner_id == receipt.owner_id,
                SessionModel.deleted_time.is_(None),
            )
        )
        if chat_session is None or chat_session.project_id != project_id:
            raise ValueError("memory_control_session_invalid")
        state = await lock_scope_state(
            session,
            receipt.namespace_id,
            command.access_scope_key,
        )
        run = await session.scalar(
            select(RunModel)
            .where(
                RunModel.id == receipt.run_id,
                RunModel.owner_id == receipt.owner_id,
            )
            .with_for_update()
        )
        if run is None or run.session_id != session_id or run.memory_project_id != project_id:
            raise ValueError("memory_control_run_invalid")
        await self._validate_control_sources(session, command, run)
        return state, run

    async def _validate_control_sources(
        self,
        session: AsyncSession,
        command: ControlCommand,
        run: RunModel,
    ) -> None:
        """只读复核当前 Run 固定的直接用户证据存在性，不加 FOR UPDATE 行锁。"""
        evidence_ids = tuple(item.message_id for item in command.evidence_sources)
        rows = (
            await session.scalars(
                select(ConversationMessageModel)
                .where(
                    ConversationMessageModel.id.in_(evidence_ids),
                    ConversationMessageModel.session_id == run.session_id,
                    ConversationMessageModel.role == ConversationRole.USER.value,
                )
                .order_by(ConversationMessageModel.id)
            )
        ).all()
        rows_by_id = {item.id: item for item in rows}
        if set(rows_by_id) != set(evidence_ids):
            raise ValueError("memory_control_source_invalid")
        allowed_ids = set(run.memory_input_message_ids or (run.trigger_message_id,))
        if not set(evidence_ids) <= allowed_ids:
            raise ValueError("memory_control_source_invalid")
        for evidence in command.evidence_sources:
            row = rows_by_id[evidence.message_id]
            if as_utc(row.completed_at or row.created_at) != evidence.source_at:
                raise ValueError("memory_control_source_changed")

    async def _lock_control_target(
        self,
        session: AsyncSession,
        namespace_id: str,
        scope_key: str,
        memory_id: str,
        expected_version: int | None,
    ) -> MemoryModel:
        """锁定当前可写 scope 内未过期的明确控制目标。"""
        if expected_version is None:
            raise ValueError("memory_control_version_required")
        row = await session.scalar(
            select(MemoryModel)
            .where(
                MemoryModel.id == memory_id,
                MemoryModel.namespace_id == namespace_id,
                MemoryModel.scope_key == scope_key,
                MemoryModel.status.in_(("active", "archived")),
                MemoryModel.version == expected_version,
                MemoryModel.deleted_time.is_(None),
                MemoryModel.expires_at.is_(None) | (MemoryModel.expires_at > func.now()),
            )
            .with_for_update()
        )
        if row is None:
            raise ValueError("memory_control_version_conflict")
        return row

    @staticmethod
    def _control_create(
        command: ControlCommand,
    ) -> PreparedMemoryCreate | None:
        """把已校验控制参数投影为不带事实身份的创建结构。"""
        if (
            command.create_memory_id is None
            or command.content is None
            or command.kind is None
            or command.load_policy is None
            or command.embedding is None
            or command.embedding_profile_id is None
            or not command.evidence_sources
        ):
            return None
        last_evidence = max(item.source_at for item in command.evidence_sources)
        return PreparedMemoryCreate(
            memory_id=command.create_memory_id,
            namespace_id=command.receipt.namespace_id,
            request_id=(
                f"control:{command.receipt.run_id}:{command.receipt.tool_call_id}:owner:"
                f"{command.receipt.namespace_id}:{command.create_memory_id}"
            ),
            scope_key=command.access_scope_key,
            content=command.content,
            content_hash=_normalized_content_hash(command.content),
            embedding=command.embedding,
            embedding_profile_id=command.embedding_profile_id,
            kind=command.kind,
            load_policy=command.load_policy,
            explicit_long_term=True,
            last_evidenced_at=last_evidence,
            expires_at=command.expires_at,
            source_evidence=command.evidence_sources,
        )

    async def user_generation(self, owner_id: str) -> int:
        """读取用户当前 generation。"""
        async with self._factory() as session:
            return await read_user_memory_generation(session, owner_id)

    async def scope_state(
        self,
        namespace_id: str,
        scope_key: str,
    ) -> MemoryScopeState:
        """读取或初始化单个 scope 的启用状态。"""
        async with self._factory() as session, session.begin():
            row = await self._lock_scope_state(session, namespace_id, scope_key)
            return MemoryScopeState(
                namespace_id=row.namespace_id,
                scope_key=row.scope_key,
                enabled=row.enabled,
            )
