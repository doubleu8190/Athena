"""严格 Round 的纯领域构造规则。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from .contracts import PlanRound
from .dag import PlanDAG
from .entities import Plan, TaskStatus


class NoReadyTasksError(ValueError):
    """当前 DAG 中没有可进入新 Round 的 Task。"""


def freeze_ready_round(
    plan: Plan,
    *,
    plan_version: int,
    round_number: int,
    round_id: str,
    task_states: Mapping[str, TaskStatus | str],
    created_at: datetime,
) -> PlanRound:
    """冻结该版本当前全部 ready Task，不按并发额度裁剪成员。

    Args:
        plan: 已通过 DAG 校验的当前 Plan。
        plan_version: 本轮引用的不可变版本号，必须为正数。
        round_number: Plan 内从 1 开始的轮次序号。
        round_id: 调用方生成的稳定轮次 ID。
        task_states: 每个逻辑 Task 的当前状态。
        created_at: 本轮创建时间，统一使用 UTC。

    Returns:
        成员集合固定的 PlanRound。

    Raises:
        ValueError: 版本、轮次或 ID 无效，或当前不存在 ready Task。
    """
    if plan_version < 1:
        raise ValueError("plan_version must be positive")
    if round_number < 1:
        raise ValueError("round_number must be positive")
    if not round_id.strip():
        raise ValueError("round_id is required")

    task_ids = tuple(PlanDAG(plan).ready_tasks(task_states))
    if not task_ids:
        raise NoReadyTasksError("no tasks are ready for a new round")
    return PlanRound(
        round_id=round_id,
        plan_id=plan.plan_id,
        plan_version=plan_version,
        round_number=round_number,
        task_ids=task_ids,
        created_at=created_at,
    )


__all__ = ["NoReadyTasksError", "freeze_ready_round"]
