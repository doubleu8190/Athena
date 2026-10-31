"""以 PostgreSQL 所有权事实和持久文件保存 Worker 资源。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import tempfile
from collections.abc import AsyncIterable
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from application.orchestration.worker_execution_grants import (
    WorkerExecutionGrantClaims,
)
from application.orchestration.worker_resources import (
    WorkerResourceDownload,
    WorkerResourceWriteResult,
)
from domain.orchestration.worker_resource_entities import (
    WorkerExecutionResourceContext,
    WorkerExecutionResourceScope,
    WorkerResourceReference,
)
from ..models import (
    AgentRunModel,
    OrchestratorExecutionModel,
    OrchestratorPlanModel,
    OrchestratorTaskModel,
    WorkerInstanceModel,
    WorkerResourceModel,
)


class PostgresWorkerResourceRepository:
    """授权执行与不可变资源元数据的 PostgreSQL 适配器。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        storage_path: str | Path,
    ) -> None:
        """初始化持久资源根目录。

        参数：
        - `session_factory`（async_sessionmaker[AsyncSession]）：执行和资源元数据的事务会话工厂。
        - `storage_path`（str | Path）：专用于资源字节的持久目录；应用启动时创建。

        返回：无。

        异常：OSError 表示资源目录不可创建或不能写入。
        """
        self._session_factory = session_factory
        self._storage_path = Path(storage_path).resolve()
        self._staging_path = self._storage_path / ".staging"
        self._staging_path.mkdir(parents=True, exist_ok=True)

    async def load_execution_resource_context(
        self, execution_id: str
    ) -> WorkerExecutionResourceContext:
        """加载仍在运行的执行事实和任务冻结输入。

        参数：
        - `execution_id`（str）：要签发资源授权的 Execution。

        返回：带服务身份、完整执行代次、截止时间和 Task 输入的上下文。

        异常：ValueError 表示执行未分配、已停止、过期或身份关系不完整。
        """
        async with self._session_factory() as session:
            context = await self._load_current_context(session, execution_id)
            if context is None:
                raise ValueError("worker_execution_resource_scope_inactive")
            return context

    async def load_execution_resource_context_for_dispatch(
        self, execution_id: str
    ) -> WorkerExecutionResourceContext:
        """在首次发送 deadline 尚未启动时收集签发授权所需的执行快照。"""
        async with self._session_factory() as session:
            context = await self._load_current_context(
                session, execution_id, require_live_deadline=False
            )
            if context is None:
                raise ValueError("worker_execution_resource_scope_inactive")
            return context

    async def resolve_read_resource_ids(
        self,
        *,
        run_id: str,
        candidate_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        """只授权冻结输入明确引用且已在同一 Run 完成的资源。

        参数：
        - `run_id`（str）：当前 Task 所属 Run。
        - `candidate_ids`（tuple[str, ...]）：从 Task.input 的 resource_id 字段提取的 UUID。

        返回：候选中真实存在、同 Run 且来源任务和执行均成功的资源 ID。

        异常：此查询不主动抛出业务异常。
        """
        if not candidate_ids:
            return ()
        async with self._session_factory() as session:
            ids = (
                await session.scalars(
                    select(WorkerResourceModel.resource_id)
                    .join(
                        OrchestratorExecutionModel,
                        OrchestratorExecutionModel.execution_id
                        == WorkerResourceModel.execution_id,
                    )
                    .join(
                        OrchestratorTaskModel,
                        OrchestratorTaskModel.task_id == WorkerResourceModel.task_id,
                    )
                    .join(
                        OrchestratorPlanModel,
                        OrchestratorPlanModel.plan_id == WorkerResourceModel.plan_id,
                    )
                    .where(
                        WorkerResourceModel.resource_id.in_(candidate_ids),
                        WorkerResourceModel.run_id == run_id,
                        WorkerResourceModel.status == "ready",
                        OrchestratorExecutionModel.status == "completed",
                        OrchestratorTaskModel.status == "completed",
                        OrchestratorPlanModel.run_id == run_id,
                    )
                )
            ).all()
        return tuple(sorted(ids))

    async def validate_current_execution(
        self, claims: WorkerExecutionGrantClaims
    ) -> WorkerExecutionResourceScope | None:
        """比对签名令牌与数据库当前代次、实例和 Task 取消状态。

        参数：
        - `claims`（WorkerExecutionGrantClaims）：已验签且尚未过期的执行授权。

        返回：所有持久身份仍匹配时返回当前范围；否则返回 `None`。

        异常：此核对不主动抛出业务异常。
        """
        async with self._session_factory() as session:
            context = await self._load_current_context(session, claims.execution_id)
            if context is None:
                return None
            scope = context.scope
            if (
                scope.expires_at is None
                or scope.service_principal != claims.service_principal
                or scope.worker_instance_id != claims.worker_instance_id
                or scope.worker_instance_key != claims.worker_instance_key
                or scope.run_id != claims.run_id
                or scope.plan_id != claims.plan_id
                or scope.task_id != claims.task_id
                or scope.execution_id != claims.execution_id
                or scope.generation != claims.generation
                or scope.cancellation_epoch != claims.cancellation_epoch
                or claims.expires_at > scope.expires_at
            ):
                return None
            allowed_ids = await self.resolve_read_resource_ids(
                run_id=scope.run_id,
                candidate_ids=tuple(
                    resource_id
                    for resource_id in claims.read_resource_ids
                    if resource_id in _resource_ids(context.task_input)
                ),
            )
            if allowed_ids != tuple(sorted(claims.read_resource_ids)):
                return None
            return WorkerExecutionResourceScope(
                service_principal=scope.service_principal,
                worker_instance_id=scope.worker_instance_id,
                worker_instance_key=scope.worker_instance_key,
                run_id=scope.run_id,
                plan_id=scope.plan_id,
                task_id=scope.task_id,
                execution_id=scope.execution_id,
                generation=scope.generation,
                cancellation_epoch=scope.cancellation_epoch,
                read_resource_ids=allowed_ids,
                expires_at=claims.expires_at,
            )

    async def store_resource(
        self,
        *,
        scope: WorkerExecutionResourceScope,
        resource_id: str,
        media_type: str,
        expected_sha256: str,
        file_name: str | None,
        chunks: AsyncIterable[bytes],
        max_bytes: int,
    ) -> WorkerResourceWriteResult:
        """流式落盘后原子保留不可变资源元数据。

        参数：
        - `scope`（WorkerExecutionResourceScope）：再次从数据库核实的活动写入执行范围。
        - `resource_id`（str）：规范化后的 UUID 主键。
        - `media_type`（str）：协议媒体类型。
        - `expected_sha256`（str）：HTTP 请求声明的字节摘要。
        - `file_name`（str | None）：仅作为元数据的可选名称。
        - `chunks`（AsyncIterable[bytes]）：原始字节流。
        - `max_bytes`（int）：允许保存的最大字节数。

        返回：新创建或同摘要重放的资源引用。

        异常：ValueError 表示大小超限、摘要不符或执行已失效；RuntimeError 表示资源目录 I/O 失败。
        """
        staged_path, size_bytes, actual_sha256 = await self._stage_bytes(
            chunks, max_bytes=max_bytes
        )
        final_path = self._resource_path(resource_id)
        moved = False
        try:
            if not hmac.compare_digest(actual_sha256, expected_sha256):
                raise ValueError("worker_resource_digest_mismatch")
            async with self._session_factory() as session:
                async with session.begin():
                    # 资源 ID 锁串行化内容摘要检查、文件换名与 metadata 状态转换。
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                        {"lock_key": f"worker-resource:{resource_id}"},
                    )
                    if (
                        await self._load_current_scope_in_session(
                            session, scope, lock_execution_chain=True
                        )
                        is None
                    ):
                        raise ValueError("worker_execution_resource_scope_inactive")
                    prior = await session.get(WorkerResourceModel, resource_id)
                    if prior is not None:
                        if (
                            prior.worker_instance_id != scope.worker_instance_id
                            or prior.run_id != scope.run_id
                            or prior.plan_id != scope.plan_id
                            or prior.task_id != scope.task_id
                            or prior.execution_id != scope.execution_id
                            or prior.generation != scope.generation
                        ):
                            raise ResourceAccessDenied(
                                "worker_resource_owner_scope_mismatch"
                            )
                        if prior.sha256 != actual_sha256 or prior.size_bytes != size_bytes:
                            raise ResourceIdConflict("worker_resource_id_conflict")
                    if prior is None:
                        now = await self._database_now(session)
                        session.add(
                            WorkerResourceModel(
                                resource_id=resource_id,
                                worker_instance_id=scope.worker_instance_id,
                                run_id=scope.run_id,
                                plan_id=scope.plan_id,
                                task_id=scope.task_id,
                                execution_id=scope.execution_id,
                                generation=scope.generation,
                                media_type=media_type,
                                size_bytes=size_bytes,
                                sha256=actual_sha256,
                                file_name=file_name,
                                status="pending",
                                created_at=now,
                            )
                        )
                        await session.flush()
                    elif prior.status == "ready":
                        reference = _reference(prior)
                        return WorkerResourceWriteResult(reference, "replayed")

            await self._pending_record_committed(resource_id)
            # pending 元数据用于隐藏中断写入。短发布事务会串行处理文件换名、ready
            # 状态转换和撤销清理。
            final_path.parent.mkdir(parents=True, exist_ok=True)
            async with self._session_factory() as session, session.begin():
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": f"worker-resource:{resource_id}"},
                )
                # 文件换名期间可能发生用户取消。只有再次持有执行链锁并
                # 确认代次仍有效，才将 pending 元数据公开为 ready。
                current_scope = await self._load_current_scope_in_session(
                    session, scope, lock_execution_chain=True
                )
                row = await session.get(WorkerResourceModel, resource_id)
                if row is None or row.sha256 != actual_sha256 or row.size_bytes != size_bytes:
                    raise RuntimeError("worker_resource_pending_record_disappeared")
                if not _resource_owned_by_scope(row, scope):
                    raise ResourceAccessDenied("worker_resource_owner_scope_mismatch")
                # ready 资源不可变；重复上传不能撤销或删除其他请求已发布的资源。
                if row.status == "ready":
                    return WorkerResourceWriteResult(_reference(row), "replayed")
                if current_scope is None:
                    await session.delete(row)
                    # 资源 ID 锁需持续持有到文件删除完成，避免其他执行抢先接管 ID。
                    await self._unlink_resource_blob(final_path)
                else:
                    await asyncio.to_thread(os.replace, staged_path, final_path)
                    moved = True
                    await asyncio.to_thread(_fsync_directory, final_path.parent)
                    now = await self._database_now(session)
                    if (
                        scope.expires_at is None
                        or current_scope.expires_at is None
                        or scope.expires_at <= now
                        or current_scope.expires_at <= now
                    ):
                        await session.delete(row)
                        await self._unlink_resource_blob(final_path)
                        current_scope = None
                    if current_scope is not None:
                        row.status = "ready"
                        row_reference = _reference(row)
            if current_scope is None:
                moved = False
                raise ValueError("worker_execution_resource_scope_inactive")
            return WorkerResourceWriteResult(row_reference, "created")
        except ValueError:
            raise
        except Exception as exc:
            raise RuntimeError("worker_resource_storage_failed") from exc
        finally:
            if staged_path.exists():
                await asyncio.to_thread(staged_path.unlink, missing_ok=True)
            if moved and not final_path.exists():
                raise RuntimeError("worker_resource_atomic_store_lost_file")

    async def load_resource(
        self,
        *,
        scope: WorkerExecutionResourceScope,
        resource_id: str,
    ) -> WorkerResourceDownload | None:
        """按同 Execution 或显式输入引用读取资源。

        参数：
        - `scope`（WorkerExecutionResourceScope）：已验签且再次核实的当前执行授权范围。
        - `resource_id`（str）：规范化后的 UUID 资源身份。

        返回：范围允许、来源任务完成且文件存在时返回资源和存储路径。

        异常：此查询不主动抛出业务异常。
        """
        async with self._session_factory() as session:
            if await self._load_current_scope_in_session(session, scope) is None:
                return None
            resource = await session.scalar(
                select(WorkerResourceModel).where(
                    WorkerResourceModel.resource_id == resource_id,
                    WorkerResourceModel.status == "ready",
                )
            )
            if resource is None:
                return None
            if resource.run_id != scope.run_id:
                raise ResourceAccessDenied("worker_resource_run_scope_mismatch")
            is_same_execution = (
                resource.worker_instance_id == scope.worker_instance_id
                and resource.execution_id == scope.execution_id
                and resource.generation == scope.generation
            )
            if not is_same_execution:
                if resource_id not in scope.read_resource_ids:
                    raise ResourceAccessDenied(
                        "worker_resource_missing_from_execution_grant"
                    )
                source_task = await session.get(
                    OrchestratorTaskModel, resource.task_id
                )
                source_execution = await session.get(
                    OrchestratorExecutionModel, resource.execution_id
                )
                if (
                    source_task is None
                    or source_execution is None
                    or source_task.status != "completed"
                    or source_execution.status != "completed"
                ):
                    raise ResourceAccessDenied(
                        "worker_resource_source_execution_not_completed"
                    )
            path = self._resource_path(resource_id)
            if not path.is_file():
                raise RuntimeError("worker_resource_blob_missing")
            size_bytes, sha256 = await asyncio.to_thread(_hash_file, path)
            if size_bytes != resource.size_bytes or not hmac.compare_digest(
                sha256, resource.sha256
            ):
                raise RuntimeError("worker_resource_blob_integrity_failure")
            return WorkerResourceDownload(_reference(resource), path)

    async def _load_current_context(
        self,
        session: AsyncSession,
        execution_id: str,
        *,
        require_live_deadline: bool = True,
    ) -> WorkerExecutionResourceContext | None:
        """通过单条关联查询生成经过一致性检查的活动执行范围。"""
        row = await session.execute(
            select(
                AgentRunModel,
                OrchestratorPlanModel,
                OrchestratorTaskModel,
                OrchestratorExecutionModel,
                WorkerInstanceModel,
            )
            .join(
                OrchestratorPlanModel,
                OrchestratorPlanModel.run_id == AgentRunModel.run_id,
            )
            .join(
                OrchestratorTaskModel,
                OrchestratorTaskModel.plan_id == OrchestratorPlanModel.plan_id,
            )
            .join(
                OrchestratorExecutionModel,
                OrchestratorExecutionModel.task_id == OrchestratorTaskModel.task_id,
            )
            .join(
                WorkerInstanceModel,
                WorkerInstanceModel.instance_id == OrchestratorExecutionModel.instance_id,
            )
            .where(OrchestratorExecutionModel.execution_id == execution_id)
        )
        result = row.one_or_none()
        if result is None:
            return None
        run, plan, task, execution, instance = result
        now = await self._database_now(session)
        if (
            run.status != "running"
            or plan.status != "running"
            or task.status != "running"
            or task.active_execution_id != execution.execution_id
            or execution.status != "running"
            or execution.instance_id != instance.instance_id
            or execution.generation != task.execution_generation
            or execution.cancellation_epoch != task.cancellation_epoch
            or (
                require_live_deadline
                and (
                    execution.deadline_at is None
                    or execution.deadline_at <= now
                )
            )
            or not isinstance(task.input_json, dict)
        ):
            return None
        scope = WorkerExecutionResourceScope(
            service_principal=instance.service_principal,
            worker_instance_id=instance.instance_id,
            worker_instance_key=instance.instance_key,
            run_id=run.run_id,
            plan_id=plan.plan_id,
            task_id=task.task_id,
            execution_id=execution.execution_id,
            generation=execution.generation,
            cancellation_epoch=execution.cancellation_epoch,
            read_resource_ids=(),
            expires_at=execution.deadline_at,
        )
        return WorkerExecutionResourceContext(scope, task.input_json)

    async def _load_current_scope_in_session(
        self,
        session: AsyncSession,
        scope: WorkerExecutionResourceScope,
        *,
        lock_execution_chain: bool = False,
    ) -> WorkerExecutionResourceScope | None:
        """在资源元数据事务内重复验证签名令牌绑定的执行代次。

        store_resource 在文件落盘后使用固定 Run→Plan→Task→Execution 顺序加锁，
        让取消与资源发布串行提交；流式读取期间不持有业务行锁。
        """
        if lock_execution_chain:
            hint = await session.get(
                OrchestratorExecutionModel, scope.execution_id
            )
            if hint is None:
                return None
            task_hint = await session.get(OrchestratorTaskModel, hint.task_id)
            if task_hint is None:
                return None
            plan_hint = await session.get(
                OrchestratorPlanModel, task_hint.plan_id
            )
            if plan_hint is None:
                return None
            run = await session.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.run_id == plan_hint.run_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            plan = await session.scalar(
                select(OrchestratorPlanModel)
                .where(OrchestratorPlanModel.plan_id == plan_hint.plan_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            task = await session.scalar(
                select(OrchestratorTaskModel)
                .where(OrchestratorTaskModel.task_id == task_hint.task_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            execution = await session.scalar(
                select(OrchestratorExecutionModel)
                .where(OrchestratorExecutionModel.execution_id == scope.execution_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if any(item is None for item in (run, plan, task, execution)):
                return None
        context = await self._load_current_context(session, scope.execution_id)
        if context is None:
            return None
        current = context.scope
        if (
            current.service_principal != scope.service_principal
            or current.worker_instance_id != scope.worker_instance_id
            or current.worker_instance_key != scope.worker_instance_key
            or current.run_id != scope.run_id
            or current.plan_id != scope.plan_id
            or current.task_id != scope.task_id
            or current.generation != scope.generation
            or current.cancellation_epoch != scope.cancellation_epoch
        ):
            return None
        return current

    async def _stage_bytes(
        self, chunks: AsyncIterable[bytes], *, max_bytes: int
    ) -> tuple[Path, int, str]:
        """将请求体写入私有暂存目录并在流式过程中计数和计算摘要。"""
        descriptor, path_string = tempfile.mkstemp(
            prefix="resource-", suffix=".tmp", dir=self._staging_path
        )
        path = Path(path_string)
        size = 0
        digest = hashlib.sha256()
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as staged:
                async for chunk in chunks:
                    if size + len(chunk) > max_bytes:
                        raise ValueError("worker_resource_too_large")
                    size += len(chunk)
                    digest.update(chunk)
                    await asyncio.to_thread(staged.write, chunk)
                await asyncio.to_thread(staged.flush)
                await asyncio.to_thread(os.fsync, staged.fileno())
            return path, size, digest.hexdigest()
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    async def _pending_record_committed(self, resource_id: str) -> None:
        """提供 pending 提交后的无操作同步点，供仓储并发测试控制交错。"""
        return None

    async def _unlink_resource_blob(self, path: Path) -> None:
        """删除指定资源文件；调用方需持有对应资源 ID 数据库锁。"""
        await asyncio.to_thread(path.unlink, missing_ok=True)

    async def _database_now(self, session: AsyncSession) -> datetime:
        """从 PostgreSQL 获取用于执行 deadline 对比和创建事实的 UTC 时钟。"""
        value = await session.scalar(select(func.clock_timestamp()))
        if not isinstance(value, datetime):
            raise RuntimeError("worker_resource_database_clock_missing")
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _resource_path(self, resource_id: str) -> Path:
        """仅使用已规范化 UUID 生成目录，避免客户端控制存储路径。"""
        return self._storage_path / resource_id[:2] / resource_id


class ResourceIdConflict(ValueError):
    """资源 ID 已绑定到不同的字节内容。"""


class ResourceAccessDenied(ValueError):
    """资源 ID 属于另一个执行范围，不能借同内容重放读取或接管。"""


def _reference(row: WorkerResourceModel) -> WorkerResourceReference:
    """将 ORM 元数据映射成不泄露路径的纯领域引用。"""
    return WorkerResourceReference(
        resource_id=row.resource_id,
        media_type=row.media_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        file_name=row.file_name,
    )


def _resource_owned_by_scope(
    row: WorkerResourceModel, scope: WorkerExecutionResourceScope
) -> bool:
    """确认资源仍绑定到授权中的实例、Run、Plan、Task 和执行代次。"""
    return (
        row.worker_instance_id == scope.worker_instance_id
        and row.run_id == scope.run_id
        and row.plan_id == scope.plan_id
        and row.task_id == scope.task_id
        and row.execution_id == scope.execution_id
        and row.generation == scope.generation
    )


def _resource_ids(value: object) -> set[str]:
    """只从冻结输入的 resource_id 字段提取合法 UUID。"""
    from uuid import UUID

    result: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == "resource_id" and isinstance(nested, str):
                try:
                    result.add(str(UUID(nested)))
                except ValueError:
                    continue
            else:
                result.update(_resource_ids(nested))
    elif isinstance(value, list):
        for item in value:
            result.update(_resource_ids(item))
    return result


def _fsync_directory(path: Path) -> None:
    """确保原子换名的目录项写入持久存储。"""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _hash_file(path: Path) -> tuple[int, str]:
    """流式核对持久文件长度和 SHA-256，不把大资源读入内存。"""
    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as content:
        while chunk := content.read(1_048_576):
            size_bytes += len(chunk)
            digest.update(chunk)
    return size_bytes, digest.hexdigest()


__all__ = [
    "PostgresWorkerResourceRepository",
    "ResourceAccessDenied",
    "ResourceIdConflict",
]
