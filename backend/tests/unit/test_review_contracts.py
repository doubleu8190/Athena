from __future__ import annotations

from collections.abc import Mapping

import pytest

from domain.orchestration import PlanRevision, ReviewAction, ReviewDecision


def test_review_decision_accepts_each_valid_tagged_shape() -> None:
    revision = PlanRevision(
        base_version=2,
        keep_task_ids=("task-a",),
        cancel_task_ids=("task-b",),
        new_tasks=({"task_id": "task-c"},),
    )

    assert (
        ReviewDecision(ReviewAction.CONTINUE, "已有任务足以继续").action
        is ReviewAction.CONTINUE
    )
    assert ReviewDecision(
        ReviewAction.REPLAN, "补充一个排查任务", revision=revision
    ).revision == revision
    assert ReviewDecision(
        ReviewAction.FINISH, "目标已达成", final_status="completed"
    ).final_status == "completed"
    assert ReviewDecision(
        ReviewAction.FINISH, "证据不足以完成目标", final_status="failed"
    ).final_status == "failed"


@pytest.mark.parametrize(
    ("action", "reason", "revision", "final_status"),
    [
        (ReviewAction.CONTINUE, " ", None, None),
        ("CONTINUE", "继续", None, None),
        (ReviewAction.CONTINUE, "继续", PlanRevision(1, (), ()), None),
        (ReviewAction.CONTINUE, "继续", None, "completed"),
        (ReviewAction.REPLAN, "修订", None, None),
        (ReviewAction.REPLAN, "修订", PlanRevision(1, (), ()), "failed"),
        (ReviewAction.FINISH, "结束", PlanRevision(1, (), ()), "completed"),
        (ReviewAction.FINISH, "结束", None, None),
        (ReviewAction.FINISH, "结束", None, "cancelled"),
    ],
)
def test_review_decision_rejects_invalid_tagged_shapes(
    action: ReviewAction | str,
    reason: str,
    revision: PlanRevision | None,
    final_status: str | None,
) -> None:
    with pytest.raises(ValueError):
        ReviewDecision(action, reason, revision, final_status)


@pytest.mark.parametrize(
    "revision",
    [
        (0, (), (), ()),
        (1, ("task-a", "task-a"), (), ()),
        (1, ("task-a",), ("task-a",), ()),
        (1, (), (), ({"task_id": "task-a"}, {"task_id": "task-a"})),
        (1, ("task-a",), (), ({"task_id": "task-a"},)),
        (1, (), (), ({"title": "missing ID"},)),
    ],
)
def test_plan_revision_rejects_conflicting_task_identity_sets(
    revision: tuple[
        int,
        tuple[str, ...],
        tuple[str, ...],
        tuple[Mapping[str, object], ...],
    ],
) -> None:
    with pytest.raises(ValueError):
        PlanRevision(
            base_version=revision[0],
            keep_task_ids=revision[1],
            cancel_task_ids=revision[2],
            new_tasks=revision[3],
        )
