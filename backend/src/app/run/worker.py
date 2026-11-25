"""有界领取 Run 并管理租约、取消和执行 deadline。"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import cast

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError

from app.run.config import RunWorkerConfig
from app.run.events import publish_pending_events
from app.run.executor import RunExecutor
from common.ids import new_id, now_utc
from domain.run.model import (
    FailRunCommand,
    Run,
    RunContextSafetyCommitError,
    RunLease,
    RunLeaseState,
    RunStatus,
)
from domain.run.ports import RunEventTransport, RunRepository

logger = logging.getLogger(__name__)


def _is_recoverable_worker_error(error: Exception) -> bool:
    """判定是否为可恢复的瞬态数据库/网络连接错误，避免单次闪断打崩整个 TaskGroup。"""
    if isinstance(error, (OperationalError, InterfaceError, SqlAlchemyTimeoutError, OSError)):
        return True
    if not isinstance(error, DBAPIError):
        return False
    if error.connection_invalidated:
        return True
    sqlstate = getattr(error.orig, "sqlstate", None)
    return isinstance(sqlstate, str) and (
        sqlstate.startswith("08") or sqlstate in {"57P01", "57P02", "57P03", "53300"}
    )


class RunWorker:
    """以固定并发领取任务并清理每次执行的外部资源。"""

    def __init__(
        self,
        repository: RunRepository,
        executor: RunExecutor,
        config: RunWorkerConfig,
        *,
        worker_id: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = now_utc,
        publisher: RunEventTransport | None = None,
    ) -> None:
        """保存持久化命令、执行器及有界轮询设置。"""
        self._repository = repository
        self._executor = executor
        self._config = config
        self._worker_id = worker_id or new_id("worker")
        self._sleep = sleep
        self._clock = clock
        self._publisher = publisher
        self._stop = asyncio.Event()
        self._started = False

    async def run(self) -> None:
        """启动固定数量 consumer 与一个过期租约回收循环。"""
        if self._started:
            raise RuntimeError("run_worker_already_started")
        self._started = True
        await self._repository.reclaim_expired(limit=32)
        try:
            async with asyncio.TaskGroup() as tasks:
                for index in range(self._config.concurrency):
                    tasks.create_task(self._consume(index))
                tasks.create_task(self._reclaim())
        finally:
            # 优雅停机时等待执行器登记的轻量后台任务（如首轮自动标题）限时收敛，避免未回收 Task。
            wait_background = getattr(self._executor, "wait_background_tasks", None)
            if callable(wait_background):
                with suppress(Exception):
                    async with asyncio.timeout(10.0):
                        await wait_background()

    async def stop(self) -> None:
        """停止新领取并等待活跃 Run 完成或按自身控制流程收尾。"""
        self._stop.set()

    async def _consume(self, index: int) -> None:
        """每次只领取一个 Run，完成当前执行后再申请下一项；瞬态 DB 故障仅局部退避重试。"""
        worker_id = f"{self._worker_id}-{index}"
        consecutive_failures = 0
        while not self._stop.is_set():
            try:
                leases = await self._repository.claim(
                    worker_id,
                    lease_seconds=self._config.lease_seconds,
                    execution_seconds=self._config.execution_seconds,
                    limit=1,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if not _is_recoverable_worker_error(error):
                    raise
                # 瞬态数据库连接/网络异常仅当前 consumer 有界抖动退避（上限 30s），
                # 避免抛出到 asyncio.TaskGroup 误杀并发在途的其他 Run。
                consecutive_failures += 1
                ceiling = min(30.0, 0.5 * (2 ** min(consecutive_failures - 1, 6)))
                retry_after = random.uniform(0.0, ceiling)
                logger.warning(
                    "run_worker_database_retry worker_id=%s error_type=%s retry_after=%.2f",
                    worker_id,
                    type(error).__name__,
                    retry_after,
                )
                await self._wait_or_stop(retry_after)
                continue
            consecutive_failures = 0
            if not leases:
                await self._wait_or_stop(self._config.queue_poll_seconds)
                continue
            try:
                await self._drive_lease(leases[0])
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if not _is_recoverable_worker_error(error):
                    raise
                consecutive_failures += 1
                ceiling = min(30.0, 0.5 * (2 ** min(consecutive_failures - 1, 6)))
                retry_after = random.uniform(0.0, ceiling)
                logger.warning(
                    "run_worker_drive_database_retry worker_id=%s run_id=%s "
                    "error_type=%s retry_after=%.2f",
                    worker_id,
                    leases[0].run.id,
                    type(error).__name__,
                    retry_after,
                )
                await self._wait_or_stop(retry_after)

    async def _drive_lease(self, lease: RunLease) -> None:
        """独立轮询状态并续约；取消、超时或失租后先关闭模型流。"""
        # 没有 worker 归属就无法续租，直接放弃本次监督。
        run = lease.run
        worker_id = run.worker_id
        if worker_id is None:
            return
        # 执行放到独立 Task，让监督循环能在执行期间继续续租和轮询状态。
        execution = asyncio.create_task(self._executor.execute(lease))
        loop = asyncio.get_running_loop()
        # 防御性分支：没有执行截止时间的租约不允许执行（正常领取一定带 deadline）。
        if run.execution_deadline_at is None:
            await self._cancel_and_join(execution, lease)
            return
        # 将数据库的墙上时钟 deadline 转成单调时钟，避免系统时间调整影响超时判断。
        remaining = (lease.execution_deadline_at - self._now()).total_seconds()
        monotonic_deadline = loop.time() + max(0.0, remaining)
        next_heartbeat = loop.time() + self._config.heartbeat_seconds
        poll_interval = self._config.cancel_poll_seconds
        try:
            while not execution.done():
                remaining = monotonic_deadline - loop.time()
                if remaining <= 0:
                    # 超时：先取消执行并等待模型流等上游资源清理完毕。
                    await self._cancel_and_join(execution, lease)
                    # 清理可能耗时，期间租约可能易主，因此重读轻量租约状态后再决定终态。
                    current = await self._read_lease_state(run.session_id, run.owner_id, run.id)
                    # 记录不存在或所有权/执行轮次已变，说明已被回收，交给新 owner 处理。
                    if (
                        current is None
                        or current.worker_id != worker_id
                        or current.execution_epoch != run.execution_epoch
                    ):
                        return
                    # 用户取消优先于超时：已进入 CANCELLING 时按取消结算。
                    if current.status is RunStatus.CANCELLING:
                        await self._settle_cancellation(lease)
                    # 仍由本 worker 持有且处于 RUNNING 时才写入超时失败态。
                    elif current.status is RunStatus.RUNNING:
                        try:
                            await self._repository.fail(
                                FailRunCommand(
                                    run_id=run.id,
                                    worker_id=worker_id,
                                    execution_epoch=run.execution_epoch,
                                    safe_error_code="execution_timeout",
                                    failed_at=self._now(),
                                )
                            )
                        except Exception as error:
                            logger.error(
                                "run_timeout_settle_failed run_id=%s exception_type=%s",
                                run.id,
                                type(error).__name__,
                            )
                        # 状态事务提交后尽力推送终态事件，丢失的通知由 SSE 对账补齐。
                        await self._publish_latest_events(lease)
                    return

                # 在取消轮询、执行截止时间和下一次心跳三者中取最早的唤醒时间。
                wait_seconds = min(poll_interval, remaining, max(0.0, next_heartbeat - loop.time()))
                if wait_seconds <= 0:
                    # 心跳到期：续租以阻止其他 worker 回收；返回 None 说明租约已失。
                    heartbeat = await self._repository.heartbeat(
                        run.id,
                        worker_id,
                        run.execution_epoch,
                        lease_seconds=self._config.lease_seconds,
                    )
                    next_heartbeat = loop.time() + self._config.heartbeat_seconds
                    if heartbeat is None:
                        # 所有权已丢失，只关闭本地执行，不能覆盖新 owner 的状态。
                        await self._cancel_and_join(execution, lease)
                        return
                    continue

                try:
                    # shield 保证 wait_for 超时只打断本次等待，不会取消执行任务；
                    # TimeoutError 是正常轮询节拍，继续下一轮。
                    await asyncio.wait_for(asyncio.shield(execution), timeout=wait_seconds)
                except TimeoutError:
                    pass
                except asyncio.CancelledError:
                    # 区分两种取消：执行任务自身被取消（结算终态）与本协程被取消（向上传播）。
                    current_task = asyncio.current_task()
                    if execution.cancelled() and (
                        current_task is None or not current_task.cancelling()
                    ):
                        await self._finalize_execution(lease, execution)
                        return
                    raise
                except Exception:
                    # 执行任务异常结束：交由 finalize 收敛状态；否则属于意外错误，向上抛出。
                    if execution.done():
                        await self._finalize_execution(lease, execution)
                        return
                    raise
                # 执行正常结束：收敛终态（执行器可能未写入终态，finalize 会兜底）。
                if execution.done():
                    await self._finalize_execution(lease, execution)
                    return

                # 轮询数据库确认状态：优先只读轻量租约标量列，取消指令与所有权变更以数据库为准。
                current = await self._read_lease_state(run.session_id, run.owner_id, run.id)
                if current is None:
                    # Run 已被删除，直接关闭本地执行。
                    await self._cancel_and_join(execution, lease)
                    return
                if (
                    current.worker_id != worker_id
                    or current.execution_epoch != run.execution_epoch
                    or current.status is not RunStatus.RUNNING
                    and current.status is not RunStatus.CANCELLING
                ):
                    # 所有权/执行轮次不匹配或已进入其他终态：可能被回收，不能写状态。
                    await self._cancel_and_join(execution, lease)
                    return
                if current.status is RunStatus.CANCELLING:
                    # 外部取消：先停止执行并完成上游清理，再结算为已取消。
                    await self._cancel_and_join(execution, lease)
                    await self._settle_cancellation(lease)
                    return
            # 执行可能在心跳等数据库调用期间完成，退出循环后补一次终态收敛。
            if execution.done():
                await self._finalize_execution(lease, execution)
        except asyncio.CancelledError:
            # 监督协程被取消（如 Worker 关闭）：先关闭执行与上游资源，再继续传播取消。
            await self._cancel_and_join(execution, lease)
            raise
        except BaseException:
            # 监督循环内出现瞬态数据库等异常时同样须先清理执行任务，避免孤儿协程泄漏。
            await self._cancel_and_join(execution, lease)
            raise

    async def _finalize_execution(self, lease: RunLease, execution: asyncio.Task[None]) -> None:
        """收敛已清理协程与取消并发，避免迟到状态提交挂起 Run。"""
        safety_commit_failed = False
        try:
            await execution
        except asyncio.CancelledError:
            pass
        except RunContextSafetyCommitError as error:
            # 专用类型表示已有稳定安全收尾命令但结果不确定；禁用普通失败覆盖。
            safety_commit_failed = True
            logger.error(
                "run_context_safety_commit_failed run_id=%s report_message_id=%s reason=%s",
                lease.run.id,
                error.report_message_id,
                error.reason,
            )
        except Exception as error:
            logger.error(
                "run_execution_task_failed run_id=%s exception_type=%s",
                lease.run.id,
                type(error).__name__,
            )
        try:
            current = await self._repository.get(
                lease.run.session_id, lease.run.owner_id, lease.run.id
            )
            target_event_sequence = None if current is None else current.event_sequence
            if (
                current is not None
                and current.worker_id == lease.run.worker_id
                and current.execution_epoch == lease.run.execution_epoch
                and current.status is RunStatus.CANCELLING
            ):
                await self._settle_cancellation(lease)
            elif (
                current is not None
                and current.worker_id == lease.run.worker_id
                and current.execution_epoch == lease.run.execution_epoch
                and current.status is RunStatus.RUNNING
                and not safety_commit_failed
            ):
                worker_id = lease.run.worker_id
                if worker_id is not None:
                    failed_run = await self._repository.fail(
                        FailRunCommand(
                            run_id=lease.run.id,
                            worker_id=worker_id,
                            execution_epoch=lease.run.execution_epoch,
                            safe_error_code="execution_failed",
                            failed_at=self._now(),
                        )
                    )
                    if failed_run is not None:
                        # 兜底失败会新增 failed 事件并推进 event_sequence，
                        # 必须使用新序号推送避免漏推终态。
                        target_event_sequence = failed_run.event_sequence
            if target_event_sequence is not None:
                await self._publish_new_events(lease, target_event_sequence)
        except Exception as error:
            logger.error(
                "run_execution_finalize_failed run_id=%s exception_type=%s",
                lease.run.id,
                type(error).__name__,
            )

    async def _publish_new_events(self, lease: RunLease, event_sequence: int) -> None:
        """在状态事务提交后尽力发布正式事件，丢通知由 SSE 对账补齐。"""
        latest_cursor, lookup_failed = await publish_pending_events(
            self._repository,
            self._publisher,
            session_id=lease.run.session_id,
            owner_id=lease.run.owner_id,
            run_id=lease.run.id,
            execution_epoch=lease.run.execution_epoch,
            after_sequence=lease.run.event_sequence,
            target_sequence=event_sequence,
        )
        if (
            not lookup_failed
            and self._publisher is not None
            and lease.run.parent_run_id is not None
            and latest_cursor > lease.run.event_sequence
        ):
            # 子 Run 状态变更同步唤醒父 Run 实时频道，避免父 Run SSE 等待周期对账。
            events = await self._repository.list_events(
                lease.run.session_id,
                lease.run.owner_id,
                lease.run.id,
                after_sequence=lease.run.event_sequence,
                limit=100,
                through_sequence=latest_cursor,
            )
            for event in events:
                try:
                    await self._publisher.publish_persisted(
                        lease.run.parent_run_id, lease.run.execution_epoch, event
                    )
                except Exception:
                    logger.warning(
                        "subagent_parent_event_forward_failed parent_run_id=%s sub_run_id=%s",
                        lease.run.parent_run_id,
                        lease.run.id,
                    )

    async def _settle_cancellation(self, lease: RunLease) -> None:
        """尽力完成已清理执行的取消状态，竞争终态不结束 Worker。"""
        worker_id = lease.run.worker_id
        if worker_id is None:
            return
        try:
            settled = await self._repository.settle_cancelled(
                lease.run.id,
                worker_id,
                lease.run.execution_epoch,
                self._now(),
            )
            if settled is not None:
                await self._publish_new_events(lease, settled.event_sequence)
        except Exception as error:
            logger.error(
                "run_cancel_settle_failed run_id=%s exception_type=%s",
                lease.run.id,
                type(error).__name__,
            )

    async def _publish_latest_events(self, lease: RunLease) -> None:
        try:
            current = await self._repository.get(
                lease.run.session_id, lease.run.owner_id, lease.run.id
            )
            if current is not None:
                await self._publish_new_events(lease, current.event_sequence)
        except Exception as error:
            logger.warning(
                "run_event_lookup_failed run_id=%s exception_type=%s",
                lease.run.id,
                type(error).__name__,
            )

    async def _reclaim(self) -> None:
        """定期回收进程停止或租约过期的执行。"""
        while not self._stop.is_set():
            await self._wait_or_stop(self._config.reclaim_seconds)
            if self._stop.is_set():
                break
            try:
                await self._repository.reclaim_expired(limit=32)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error("run_reclaim_failed exception_type=%s", type(error).__name__)

    async def _wait_or_stop(self, seconds: float) -> None:
        """等待指定时间或在停止信号到达时提前返回。"""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _cancel_and_join(self, task: asyncio.Task[None], lease: RunLease) -> None:
        """取消流时继续续租，直到执行协程完成上游资源清理。"""
        if not task.done():
            task.cancel()
        loop = asyncio.get_running_loop()
        next_heartbeat = loop.time()
        worker_id = lease.run.worker_id
        if worker_id is not None:
            while not task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=self._config.cancel_poll_seconds,
                    )
                except TimeoutError:
                    pass
                except asyncio.CancelledError:
                    if task.done():
                        break
                except Exception:
                    # 被取消的执行任务在清理阶段抛出的异常交由下方统一记录收敛，
                    # 避免穿透 _cancel_and_join 覆盖外层瞬态数据库异常并击穿 TaskGroup。
                    if task.done():
                        break
                    raise
                if task.done() or loop.time() < next_heartbeat:
                    continue
                try:
                    heartbeat = await self._repository.heartbeat(
                        lease.run.id,
                        worker_id,
                        lease.run.execution_epoch,
                        lease_seconds=self._config.lease_seconds,
                    )
                except Exception as error:
                    # 清理阶段数据库暂不可用时停止续租循环，仍继续等待被取消的执行任务完成清理。
                    logger.warning(
                        "run_cancel_heartbeat_failed run_id=%s exception_type=%s",
                        lease.run.id,
                        type(error).__name__,
                    )
                    break
                if heartbeat is None:
                    break
                next_heartbeat = loop.time() + self._config.heartbeat_seconds
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as error:
            logger.error(
                "run_execution_cleanup_failed run_id=%s exception_type=%s",
                lease.run.id,
                type(error).__name__,
            )

    async def _read_lease_state(
        self, session_id: str, owner_id: str, run_id: str
    ) -> RunLeaseState | Run | None:
        """优先调用轻量标量租约查询，兼容测试中仅实现 get() 的最小仓储替身。"""
        get_lease_state = getattr(self._repository, "get_lease_state", None)
        if callable(get_lease_state):
            return cast(
                RunLeaseState | None,
                await get_lease_state(session_id, owner_id, run_id),
            )
        return await self._repository.get(session_id, owner_id, run_id)

    def _now(self) -> datetime:
        """读取并规范化应用时钟，用于比较数据库返回的租约快照。"""
        value = self._clock()
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


__all__ = ["RunWorker"]
