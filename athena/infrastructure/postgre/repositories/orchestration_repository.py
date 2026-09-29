"""中心编排计划、任务和结果的 PostgreSQL 仓库。"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from athena.infrastructure.postgre.engine import get_session
from athena.infrastructure.postgre.models import (
    AgentPlanModel,
    AgentTaskModel,
    AgentTaskResultModel,
)
from .repository_utils import _json_dumps, _now_iso
from athena.contracts.orchestration import (
    ExecutionPlan,
    TaskStatus,
    WorkerResult,
    ensure_task_transition,
)
from datetime import datetime, timedelta


class OrchestrationRepository:
    """保存可恢复的中心编排任务账本。"""

    async def create_plan(self, session_id: str, plan: ExecutionPlan) -> None:
        """原子创建计划及其全部无依赖任务。

        参数：
            session_id: 计划所属用户会话。
            plan: 已通过 V1 契约校验的执行计划。

        返回值：
            None: 计划和任务已在同一事务提交。

        异常：
            ValueError: plan_id 或 task_id 已存在。
        """

        now = _now_iso()
        async with get_session() as db:
            db.add(
                AgentPlanModel(
                    plan_id=plan.plan_id,
                    session_id=session_id,
                    root_run_id=plan.root_run_id,
                    goal=plan.goal,
                    status="running",
                    schema_version=plan.schema_version,
                    plan_json=_json_dumps(plan),
                    created_at=now,
                    updated_at=now,
                )
            )
            for index, task in enumerate(plan.tasks):
                db.add(
                    AgentTaskModel(
                        task_id=task.task_id,
                        plan_id=plan.plan_id,
                        task_index=index,
                        status=TaskStatus.QUEUED.value,
                        task_json=_json_dumps(task),
                        available_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
            try:
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                raise ValueError("plan or task identity already exists") from exc

    async def claim_next_task(
        self, plan_id: str, lease_owner: str
    ) -> AgentTaskModel | None:
        """领取计划中最早的可运行任务。

        参数：
            plan_id: 要调度的计划标识。
            lease_owner: 当前 PlanDispatcher 实例标识。

        返回值：
            AgentTaskModel | None: 领取后的任务；没有可用任务时为空。

        异常：
            数据库读写失败时传播 SQLAlchemy 异常。
        """

        now = _now_iso()
        async with get_session() as db:
            candidate = await db.scalar(
                select(AgentTaskModel)
                .where(
                    AgentTaskModel.plan_id == plan_id,
                    AgentTaskModel.status == TaskStatus.QUEUED.value,
                    AgentTaskModel.available_at <= now,
                )
                .order_by(AgentTaskModel.task_index)
                .limit(1)
            )
            if candidate is None:
                return None
            # 并发领取依赖数据库 CAS；候选索引只提供乐观提示。
            claimed = await db.execute(
                update(AgentTaskModel)
                .where(
                    AgentTaskModel.task_id == candidate.task_id,
                    AgentTaskModel.status == TaskStatus.QUEUED.value,
                )
                .values(
                    status=TaskStatus.CLAIMED.value,
                    claimed_at=now,
                    lease_owner=lease_owner,
                    attempt=AgentTaskModel.attempt + 1,
                    updated_at=now,
                )
            )
            if claimed.rowcount != 1:
                await db.rollback()
                return None
            await db.commit()
            return await db.get(AgentTaskModel, candidate.task_id)

    async def transition_task(
        self,
        task_id: str,
        current: TaskStatus,
        target: TaskStatus,
        *,
        worker_run_id: str | None = None,
    ) -> bool:
        """以 compare-and-set 方式推进任务状态。

        参数：
            task_id: 稳定任务标识。
            current: 调用方已观察到的当前状态。
            target: 状态机允许的目标状态。
            worker_run_id: CLAIMED 启动时绑定的 Worker 尝试标识。

        返回值：
            bool: 状态与预期一致并成功更新时返回 True。

        异常：
            ValueError: 状态机不允许该转换。
        """

        ensure_task_transition(current, target)
        now = _now_iso()
        values: dict[str, object] = {"status": target.value, "updated_at": now}
        if worker_run_id is not None:
            values["worker_run_id"] = worker_run_id
        if target == TaskStatus.RUNNING:
            values["started_at"] = now
        if target in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.TIMED_OUT,
            TaskStatus.INVALID_OUTPUT,
            TaskStatus.CANCELLED,
        }:
            values["finished_at"] = now
        async with get_session() as db:
            changed = await db.execute(
                update(AgentTaskModel)
                .where(
                    AgentTaskModel.task_id == task_id,
                    AgentTaskModel.status == current.value,
                )
                .values(**values)
            )
            await db.commit()
            return changed.rowcount == 1

    async def save_task_result(self, result: WorkerResult) -> bool:
        """幂等保存通过 Structured Output 校验的 Worker 结果。

        参数：
            result: Worker 最终结果契约。

        返回值：
            bool: 首次保存返回 True；完全相同的重复写入返回 False。

        异常：
            ValueError: 同一 task_id 已保存不同结果。
        """

        result_json = _json_dumps(result)
        output_hash = hashlib.sha256(result_json.encode("utf-8")).hexdigest()
        now = _now_iso()
        async with get_session() as db:
            existing = await db.get(AgentTaskResultModel, result.task_id)
            if existing is not None:
                if existing.output_hash != output_hash:
                    raise ValueError("task result idempotency conflict")
                return False
            db.add(
                AgentTaskResultModel(
                    task_id=result.task_id,
                    worker_run_id=result.run_id,
                    status=result.status.value,
                    result_json=result_json,
                    output_hash=output_hash,
                    created_at=now,
                    completed_at=now,
                )
            )
            await db.commit()
            return True

    async def update_plan_status(
        self,
        plan_id: str,
        status: str,
        *,
        error: dict[str, Any] | None = None,
    ) -> bool:
        """更新计划的持久化状态。

        参数：
            plan_id: 执行计划标识。
            status: 计划状态字符串。
            error: 计划失败时的结构化错误。

        返回值：
            bool: 计划存在并更新成功时返回 True。

        异常：
            数据库写入失败时传播 SQLAlchemy 异常。
        """

        values: dict[str, Any] = {"status": status, "updated_at": _now_iso()}
        if error is not None:
            values["error_json"] = _json_dumps(error)
        async with get_session() as db:
            changed = await db.execute(
                update(AgentPlanModel)
                .where(AgentPlanModel.plan_id == plan_id)
                .values(**values)
            )
            await db.commit()
            return changed.rowcount == 1

    async def get_task_record(self, task_id: str) -> AgentTaskModel | None:
        """按 ID 查询任务。

        参数：
            task_id: 稳定任务标识。

        返回值：
            AgentTaskModel | None: 任务记录；不存在时为空。

        异常：
            数据库查询失败时传播 SQLAlchemy 异常。
        """
        async with get_session() as db:
            return await db.get(AgentTaskModel, task_id)

    async def requeue_stale_claimed_tasks(
        self,
        *,
        lease_seconds: int = 300,
        retry_limit: int = 1,
    ) -> int:
        """把租约过期的已领取任务重新排队或标记重试等待。

        参数：
            lease_seconds: Worker 领取后的最长无更新时间。
            retry_limit: 任务最大重试次数；达到上限的任务进入 RETRY_WAIT。

        返回值：
            int: 本次被重置的任务数量。

        异常：
            数据库读写失败时传播 SQLAlchemy 异常。
        """

        threshold = (
            datetime.now() - timedelta(seconds=lease_seconds)
        ).isoformat()
        now = _now_iso()
        async with get_session() as db:
            rows = (
                await db.execute(
                    select(AgentTaskModel).where(
                        AgentTaskModel.status == TaskStatus.CLAIMED.value,
                        AgentTaskModel.claimed_at < threshold,
                    )
                )
            ).scalars()
            count = 0
            for row in rows:
                if row.attempt > retry_limit:
                    next_status = TaskStatus.RETRY_WAIT.value
                else:
                    next_status = TaskStatus.QUEUED.value
                await db.execute(
                    update(AgentTaskModel)
                    .where(
                        AgentTaskModel.task_id == row.task_id,
                        AgentTaskModel.status == TaskStatus.CLAIMED.value,
                        AgentTaskModel.claimed_at < threshold,
                    )
                    .values(
                        status=next_status,
                        claimed_at=None,
                        lease_owner=None,
                        updated_at=now,
                    )
                )
                count += 1
            await db.commit()
            return count
