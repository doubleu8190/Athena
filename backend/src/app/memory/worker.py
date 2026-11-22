"""记忆生命周期后台 Worker：联合提取、扫描、归档与删除清理。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.memory.extraction import (
    ExtractionContextBudgetExceeded,
    ExtractionInfrastructureError,
    ExtractionSchemaError,
    ExtractionSourceChanged,
    MemoryExtractionService,
)
from common.date import utc_now
from common.ids import new_id
from domain.llm.profile_ports import ModelProfileRepository
from domain.memory.lifecycle import (
    ApplyBatchCommand,
    CleanupJobResult,
    CleanupScopePayload,
    CompleteJobCommand,
    ExtractBatchMember,
    ExtractBatchPayload,
    ExtractPendingPayload,
    JobKind,
    MemoryJob,
    MemoryJobResult,
    MemoryPendingRun,
)
from domain.memory.ports import MemoryLifecycleRepositoryPort

logger = logging.getLogger(__name__)

BACKGROUND_JOB_KINDS = (
    JobKind.EXTRACT_PENDING,
    JobKind.CLEANUP_SCOPE,
)
_SCAN_PAGE_LIMIT = 100
_MAX_EXTRACT_ATTEMPTS = 5
_BUSINESS_FAILURE_CODES = frozenset(
    {
        "memory_auto_extract_disabled",
        "memory_batch_create_replayed",
        "memory_batch_decisions_incomplete",
        "memory_batch_job_changed",
        "memory_batch_member_changed",
        "memory_batch_project_mixed",
        "memory_batch_project_unavailable",
        "memory_batch_session_changed",
        "memory_batch_source_changed",
        "memory_batch_source_invalid",
        "memory_batch_target_changed",
        "memory_decision_content_invalid",
        "memory_decision_context_too_large",
        "memory_decision_coverage_invalid",
        "memory_decision_target_duplicate",
        "memory_decision_target_forbidden",
        "memory_scope_disabled",
    }
)


@dataclass(frozen=True, slots=True)
class MemoryWorkerConfig:
    """单 Worker 的轮询、联合提取批次与归档边界。"""

    batch_run_limit: int = 20
    batch_token_limit: int = 12_000
    fact_max_age_days: int = 180
    other_max_age_days: int = 365
    idle_sleep_seconds: float = 1.0
    scan_interval_seconds: float = 30.0

    def __post_init__(self) -> None:
        """拒绝无界批次、非法归档期限和过于紧密的轮询。"""
        if not 1 <= self.batch_run_limit <= 20:
            raise ValueError("memory_worker_batch_invalid")
        if not 1_000 <= self.batch_token_limit <= 1_000_000:
            raise ValueError("memory_worker_batch_invalid")
        if self.fact_max_age_days < 1 or self.other_max_age_days < 1:
            raise ValueError("memory_worker_maintenance_invalid")
        if not 0.05 <= self.idle_sleep_seconds <= 60:
            raise ValueError("memory_worker_poll_invalid")
        if not 0.0 <= self.scan_interval_seconds <= 3_600:
            raise ValueError("memory_worker_scan_interval_invalid")


class MemoryWorkerConfigurationError(RuntimeError):
    """默认文本模型或运行配置不可用。"""


class MemoryWorker:
    """在一个顺序循环中执行四类记忆后台任务。"""

    def __init__(
        self,
        repository: MemoryLifecycleRepositoryPort,
        profiles: ModelProfileRepository,
        extraction: MemoryExtractionService,
        *,
        config: MemoryWorkerConfig | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """保存固定依赖；生产入口只创建一个共享实例。"""
        self._repository = repository
        self._profiles = profiles
        self._extraction = extraction
        self._config = config or MemoryWorkerConfig()
        self._monotonic = monotonic
        self._maintenance_day: str | None = None
        self._scan_boundary_at: datetime = utc_now()
        self._scan_cursor_finished_at: datetime | None = None
        self._scan_cursor_run_id: str | None = None
        self._last_scan_at: float | None = None

    def set_scan_boundary(self, enabled_boundary_at: datetime) -> None:
        """设置缺漏 Run 扫描的起始时间边界，避免全表历史回扫。"""
        self._scan_boundary_at = enabled_boundary_at
        self._scan_cursor_finished_at = None
        self._scan_cursor_run_id = None
        self._last_scan_at = None

    async def validate_runtime(self) -> None:
        """启动前确认管理员默认文本模型可解析。"""
        if await self._profiles.get_default_runtime() is None:
            raise MemoryWorkerConfigurationError(
                "memory_worker_default_model_unavailable"
            )

    async def recover_running_jobs(self) -> int:
        """启动时恢复普通后台任务，control 回执不进入恢复范围。"""
        return await self._repository.recover_running_jobs()

    async def run_once(self) -> bool:
        """执行定时维护与缺漏扫描，并处理至多一个异步后台任务。"""
        await self._ensure_daily_maintenance()
        await self._scan_missing_runs()
        job = await self._repository.claim_job(
            kinds=BACKGROUND_JOB_KINDS,
            limit=1,
        )
        if job is None:
            return False
        try:
            await self._process_job(job)
        except Exception as error:
            logger.warning(
                "memory_job_failed kind=%s exception_type=%s",
                job.kind.value,
                type(error).__name__,
            )
            await self._handle_unexpected_job_failure(job)
        return True

    async def _process_job(self, job: MemoryJob) -> None:
        """按类型化 payload 分派普通后台任务，control 不会进入此方法。"""
        payload = job.payload
        if isinstance(payload, ExtractPendingPayload):
            await self._process_extract(job, payload)
            return
        if isinstance(payload, CleanupScopePayload):
            cleanup_result = await self._repository.cleanup_scope(
                payload.namespace_id,
                payload.scope_key,
            )
            await self._complete(
                job,
                CleanupJobResult(result=cleanup_result),
            )
            return
        raise ValueError("memory_worker_job_kind_invalid")

    async def _process_extract(
        self,
        job: MemoryJob,
        payload: ExtractPendingPayload,
    ) -> None:
        """完成一个内存中的有界联合批次，并交由仓储原子提交。"""
        batch: ExtractBatchPayload | None = None
        try:
            runtime = await self._profiles.get_default_runtime()
            if runtime is None:
                raise MemoryWorkerConfigurationError(
                    "memory_worker_default_model_unavailable"
                )
            pending = await self._load_batch_candidates(payload.owner_id)
            if not pending:
                remaining = await self._repository.list_pending_runs(
                    payload.owner_id,
                    limit=1,
                    token_budget=self._config.batch_token_limit,
                )
                completed = await self._complete(job, None)
                if completed and remaining:
                    await self._repository.wake_user(payload.owner_id)
                return
            project_id = pending[0].project_id
            batch_runs = tuple(
                item for item in pending if item.project_id == project_id
            )
            if not batch_runs:
                raise ExtractionSourceChanged("memory_batch_member_changed")
            batch = self._new_batch(
                payload.owner_id,
                batch_runs,
                runtime.version.id,
            )
            sources = await self._repository.load_batch_sources(batch)
            batch = await self._extraction.complete_extraction(
                batch,
                runtime,
                sources,
            )
            batch = await self._extraction.complete_decisions(
                batch,
                runtime,
                sources,
            )
            changes = await self._extraction.prepare_changes(batch, sources)
            await self._repository.apply_batch(
                ApplyBatchCommand(
                    job_id=job.id,
                    batch=batch,
                    changes=changes,
                    summarized_skips=batch.skipped,
                )
            )
        except ExtractionSchemaError:
            await self._handle_schema_failure(job, batch)
        except (
            ExtractionContextBudgetExceeded,
            ExtractionSourceChanged,
            ValueError,
        ) as error:
            code = str(error)
            if batch is None or code not in _BUSINESS_FAILURE_CODES:
                await self._handle_infrastructure_failure(
                    job,
                    "memory_extraction_failed",
                    batch,
                )
                return
            await self._discard_batch(job, batch, code)
        except (ExtractionInfrastructureError, TimeoutError, OSError):
            await self._handle_infrastructure_failure(
                job,
                "memory_extraction_model_failed",
                batch,
            )
        except MemoryWorkerConfigurationError:
            await self._handle_infrastructure_failure(
                job,
                "memory_worker_model_unavailable",
                batch,
            )
        except Exception as error:
            logger.warning(
                "memory_extract_failed exception_type=%s",
                type(error).__name__,
            )
            await self._handle_infrastructure_failure(
                job,
                "memory_extraction_failed",
                batch,
            )

    async def _load_batch_candidates(
        self,
        owner_id: str,
    ) -> tuple[MemoryPendingRun, ...]:
        """在建模前跳过不合规 Run，只返回可完整进入联合批次的来源。"""
        char_budget = self._config.batch_token_limit * 4
        for _ in range(self._config.batch_run_limit):
            pending = await self._repository.list_pending_runs(
                owner_id,
                limit=self._config.batch_run_limit,
                token_budget=self._config.batch_token_limit,
            )
            if not pending:
                return ()
            oversized = tuple(
                item
                for item in pending
                if self._run_exceeds_budget(item, char_budget)
            )
            if not oversized:
                return pending
            await self._repository.skip_runs(
                owner_id,
                tuple(item.run_id for item in oversized),
                "memory_run_too_large",
            )
        return ()

    @staticmethod
    def _run_exceeds_budget(
        run: MemoryPendingRun,
        char_budget: int,
    ) -> bool:
        """按与 pending 查询一致的字符近似预算判断完整 Run 是否超长。"""
        return (
            len(run.messages) > 100
            or sum(len(item.text) for item in run.messages) > char_budget
        )

    @classmethod
    def _new_batch(
        cls,
        owner_id: str,
        pending: tuple[MemoryPendingRun, ...],
        model_profile_version_id: str,
    ) -> ExtractBatchPayload:
        """把同一项目范围的完整 Run 固定成内存联合批次。"""
        generation = pending[0].generation
        members: list[ExtractBatchMember] = []
        for run in pending:
            if not run.messages:
                raise ExtractionSourceChanged("memory_batch_source_changed")
            members.append(
                ExtractBatchMember(
                    run_id=run.run_id,
                    session_id=run.session_id,
                    project_id=run.project_id,
                    source_message_ids=tuple(
                        item.message_id for item in run.messages
                    ),
                    source_cutoff_at=max(item.source_at for item in run.messages),
                )
            )
        project_id = pending[0].project_id
        scope_keys = (
            ("personal",)
            if project_id is None
            else ("personal", f"project:{project_id}")
        )
        return ExtractBatchPayload(
            batch_id=new_id("mbatch"),
            owner_id=owner_id,
            generation=generation,
            members=tuple(members),
            model_profile_version_id=model_profile_version_id,
            prompt_version=2,
            scope_keys=scope_keys,
            created_at=utc_now(),
        )

    async def _discard_batch(
        self,
        job: MemoryJob,
        batch: ExtractBatchPayload,
        discard_reason: str,
    ) -> None:
        """通过唯一原子提交端口丢弃失效批次，不在任务内重试模型。"""
        await self._repository.apply_batch(
            ApplyBatchCommand(
                job_id=job.id,
                batch=batch,
                changes=(),
                summarized_skips=0,
                discard_reason=discard_reason,
            )
        )

    async def _handle_schema_failure(
        self,
        job: MemoryJob,
        batch: ExtractBatchPayload | None = None,
    ) -> None:
        """Schema 首次失败退避重生成一次，第二次直接判死当前批次涉及的 Run。"""
        if job.schema_retry_count < 1:
            await self._complete(
                job,
                None,
                safe_error_code="memory_extraction_schema_invalid",
                retryable=True,
                schema_invalid=True,
            )
            return
        await self._complete(
            job,
            None,
            safe_error_code="memory_extraction_schema_invalid",
            failed_run_ids=() if batch is None else tuple(item.run_id for item in batch.members),
        )

    async def _handle_infrastructure_failure(
        self,
        job: MemoryJob,
        safe_error_code: str,
        batch: ExtractBatchPayload | None = None,
    ) -> None:
        """基础设施失败按任务状态有界退避，耗尽后仅收敛当前失败批次涉及的 Run。"""
        retryable = job.attempt_count < _MAX_EXTRACT_ATTEMPTS
        await self._complete(
            job,
            None,
            safe_error_code=safe_error_code,
            retryable=retryable,
            failed_run_ids=(
                ()
                if retryable or batch is None
                else tuple(item.run_id for item in batch.members)
            ),
        )

    async def _handle_unexpected_job_failure(self, job: MemoryJob) -> None:
        """按任务尝试次数收敛未预期异常，不让消费者退出。"""
        if isinstance(job.payload, ExtractPendingPayload):
            await self._handle_infrastructure_failure(
                job,
                "memory_extraction_failed",
            )
            return
        await self._complete(
            job,
            None,
            safe_error_code="memory_worker_job_failed",
            retryable=job.attempt_count < _MAX_EXTRACT_ATTEMPTS,
        )

    async def _complete(
        self,
        job: MemoryJob,
        result: MemoryJobResult | None,
        *,
        safe_error_code: str | None = None,
        retryable: bool = False,
        schema_invalid: bool = False,
        failed_run_ids: tuple[str, ...] = (),
    ) -> bool:
        """完成、退避或失败一个后台任务。"""
        return await self._repository.complete_job(
            CompleteJobCommand(
                job_id=job.id,
                result=result,
                safe_error_code=safe_error_code,
                retryable=retryable,
                schema_invalid=schema_invalid,
                failed_run_ids=failed_run_ids,
            )
        )

    async def _ensure_daily_maintenance(self) -> None:
        """每个 UTC 日直接执行启用 scope 的过期与超龄归档，不再生成大量维护任务行。"""
        day = utc_now().astimezone(UTC).date().isoformat()
        if self._maintenance_day == day:
            return
        await self._repository.maintain_due_scopes(
            day,
            fact_max_age_days=self._config.fact_max_age_days,
            other_max_age_days=self._config.other_max_age_days,
        )
        self._maintenance_day = day

    async def _scan_missing_runs(self) -> None:
        """在 Worker 循环内按周期节流有界扫描遗漏的 pending Run，无需持久化游标任务。"""
        # 仅在整轮扫描已收敛且未到下一周期时跳过，避免每次 run_once() 重复扫库；
        # 首次启动及多页续扫期间立即执行，保证积压尽快排空。
        if (
            self._scan_cursor_finished_at is None
            and self._last_scan_at is not None
            and (self._monotonic() - self._last_scan_at) < self._config.scan_interval_seconds
        ):
            return
        result = await self._repository.scan_missing_runs(
            enabled_boundary_at=self._scan_boundary_at,
            cursor_finished_at=self._scan_cursor_finished_at,
            cursor_run_id=self._scan_cursor_run_id,
            page_limit=_SCAN_PAGE_LIMIT,
        )
        if result.round_complete:
            self._scan_cursor_finished_at = None
            self._scan_cursor_run_id = None
            self._last_scan_at = self._monotonic()
        else:
            self._scan_cursor_finished_at = result.next_cursor_finished_at
            self._scan_cursor_run_id = result.next_cursor_run_id


__all__ = [
    "BACKGROUND_JOB_KINDS",
    "MemoryWorker",
    "MemoryWorkerConfig",
    "MemoryWorkerConfigurationError",
]
