from __future__ import annotations

from types import SimpleNamespace

import pytest

from application.orchestration.root_workflow import RootWorkflow
from domain.orchestration import (
    CommandReceipt,
    OrchestrationCommandType,
    ReviewAction,
    ReviewContext,
    ReviewDecision,
)


def context() -> ReviewContext:
    return ReviewContext(
        original_goal="Prepare a report",
        plan_id="plan-1",
        plan_version=3,
        round_id="round-7",
        full_dag_summary={"nodes": [{"task_id": "task-1"}], "edges": []},
        round_results=({"task_id": "task-1", "status": "completed"},),
    )


def state():
    return {
        "request": {"run_id": "run-1", "session_id": "session-1"},
        "root_wait": {
            "type": "round_wait",
            "run_id": "run-1",
            "plan_id": "plan-1",
            "round_id": "round-7",
            "plan_version": 3,
        },
        "resume_value": {
            "type": "round_completed",
            "run_id": "run-1",
            "plan_id": "plan-1",
            "round_id": "round-7",
            "plan_version": 3,
            "interrupt_id": "interrupt-7",
        },
    }


class ContextRepository:
    async def load_review_context(self, round_id):
        assert round_id == "round-7"
        return context()


class CommandHandler:
    def __init__(self, stored_decision):
        self.stored_decision = stored_decision
        self.loaded = None
        self.applied = None

    async def load_review_decision(self, **kwargs):
        self.loaded = kwargs
        return self.stored_decision

    async def apply_review_decision(self, command, decision):
        self.applied = (command, decision)
        return CommandReceipt(
            message_id=command.message_id,
            plan_id=command.plan_id,
            plan_version=4 if decision.action == ReviewAction.REPLAN else 3,
            round_id="round-8",
            task_ids=("task-2",),
            deduplicated=self.stored_decision is not None,
        )


@pytest.mark.asyncio
async def test_root_review_reuses_persisted_decision_without_another_llm_call():
    decision = ReviewDecision(ReviewAction.CONTINUE, "Continue to the next task.")

    class Agent:
        async def review(self, *_args, **_kwargs):
            raise AssertionError("a committed review must be reused")

    handler = CommandHandler(decision)
    workflow = RootWorkflow(
        SimpleNamespace(
            review_context_repository=None,
            orchestration_command_handler=handler,
            agent=Agent(),
        )
    )

    result = await workflow.review_completed_round(state())

    assert handler.loaded == {
        "scope_key": "run-1",
        "idempotency_key": "root-review:round-7",
        "plan_id": "plan-1",
        "plan_version": 3,
        "round_id": "round-7",
    }
    command, applied = handler.applied
    assert applied == decision
    assert command.command_type is OrchestrationCommandType.CONTINUE_PLAN
    assert command.round_id == "round-7"
    assert command.plan_version == 3
    assert command.causation_id == "interrupt-7"
    assert result["review_action"] == "CONTINUE"


@pytest.mark.asyncio
async def test_root_review_calls_agent_once_then_applies_new_decision():
    decision = ReviewDecision(
        ReviewAction.FINISH,
        "The report is ready.",
        final_status="completed",
    )

    class Agent:
        def __init__(self):
            self.calls = []

        async def review(self, value, *, request):
            self.calls.append((value, request))
            return decision

    agent = Agent()
    handler = CommandHandler(None)
    workflow = RootWorkflow(
        SimpleNamespace(
            review_context_repository=ContextRepository(),
            orchestration_command_handler=handler,
            agent=agent,
        )
    )

    await workflow.review_completed_round(state())

    assert agent.calls == [(context(), state()["request"])]
    command, applied = handler.applied
    assert applied == decision
    assert command.command_type is OrchestrationCommandType.FINISH_PLAN


@pytest.mark.asyncio
async def test_root_review_rejects_resume_for_a_different_round():
    workflow = RootWorkflow(SimpleNamespace())
    invalid_state = state()
    invalid_state["resume_value"]["round_id"] = "round-6"

    with pytest.raises(RuntimeError, match="root_review_resume_identity_mismatch"):
        await workflow.review_completed_round(invalid_state)
