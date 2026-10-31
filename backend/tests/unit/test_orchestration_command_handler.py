from __future__ import annotations

from datetime import datetime, timezone

import pytest

from application.orchestration.command_handler import OrchestrationCommandHandler
from domain.orchestration import (
    OrchestrationCommand,
    OrchestrationCommandType,
    PlanRevision,
    ReviewAction,
    ReviewDecision,
)
from tests.fakes.leases import lease_grant


class ReviewCommandRepository:
    def __init__(self):
        self.applied = None

    async def apply_review_decision(self, command, decision):
        self.applied = (command, decision)
        return "receipt"


class SubmitCommandRepository:
    def __init__(self):
        self.submitted = None

    async def submit_plan(self, command, plan, tasks):
        self.submitted = (command, plan, tasks)
        return "receipt"


def command(command_type: OrchestrationCommandType) -> OrchestrationCommand:
    return OrchestrationCommand(
        message_id="message-1",
        command_type=command_type,
        idempotency_key="review:round-1",
        correlation_id="run-1",
        occurred_at=datetime.now(timezone.utc),
        plan_id="plan-1",
        scope_key="run-1",
        plan_version=4,
        round_id="round-1",
        lease=lease_grant("root_execution", "run-1"),
    )


@pytest.mark.asyncio
async def test_legacy_submit_plan_is_explicitly_disabled_during_s2():
    repository = SubmitCommandRepository()
    handler = OrchestrationCommandHandler(repository)

    with pytest.raises(RuntimeError, match="execution_not_available_during_worker_s2"):
        await handler.submit_plan(None, None, [])

    assert repository.submitted is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "command_type"),
    [
        (ReviewAction.CONTINUE, OrchestrationCommandType.CONTINUE_PLAN),
        (ReviewAction.REPLAN, OrchestrationCommandType.REVISE_PLAN),
        (ReviewAction.FINISH, OrchestrationCommandType.FINISH_PLAN),
    ],
)
async def test_command_handler_dispatches_matching_review_action(action, command_type):
    repository = ReviewCommandRepository()
    handler = OrchestrationCommandHandler(repository)
    decision = ReviewDecision(
        action=action,
        reason="reviewed",
        revision=(
            PlanRevision(4, (), (), ()) if action == ReviewAction.REPLAN else None
        ),
        final_status=("completed" if action == ReviewAction.FINISH else None),
    )
    review_command = command(command_type)

    receipt = await handler.apply_review_decision(review_command, decision)

    assert receipt == "receipt"
    assert repository.applied == (review_command, decision)


@pytest.mark.asyncio
async def test_command_handler_rejects_review_decision_with_wrong_command_type():
    repository = ReviewCommandRepository()
    handler = OrchestrationCommandHandler(repository)

    with pytest.raises(ValueError, match="review_action_command_type_mismatch"):
        await handler.apply_review_decision(
            command(OrchestrationCommandType.FINISH_PLAN),
            ReviewDecision(ReviewAction.CONTINUE, "keep going"),
        )


@pytest.mark.asyncio
async def test_command_handler_rejects_revision_for_a_different_base_version():
    repository = ReviewCommandRepository()
    handler = OrchestrationCommandHandler(repository)
    decision = ReviewDecision(
        ReviewAction.REPLAN,
        "replace a blocked task",
        revision=PlanRevision(3, (), (), ()),
    )

    with pytest.raises(ValueError, match="review_command_revision_version_mismatch"):
        await handler.apply_review_decision(
            command(OrchestrationCommandType.REVISE_PLAN), decision
        )

    assert repository.applied is None
