from __future__ import annotations

from types import SimpleNamespace

import pytest

from application.orchestration.root_workflow import RootWorkflow
from domain.orchestration import (
    CommandReceipt,
    OrchestrationCommandType,
    PlanCommandRejected,
    PlanRevision,
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


@pytest.mark.asyncio
async def test_blocked_continue_gets_one_root_review_to_replan():
    """Continue 被依赖失败阻断时，只追加一次带约束的 Root Review。"""
    blocked_context = context()
    blocked_context = ReviewContext(
        original_goal=blocked_context.original_goal,
        plan_id=blocked_context.plan_id,
        plan_version=blocked_context.plan_version,
        round_id=blocked_context.round_id,
        full_dag_summary=blocked_context.full_dag_summary,
        round_results=blocked_context.round_results,
        remaining_tasks=(
            {
                "task_id": "task-2",
                "status": "pending",
                "blocked_by": ["task-1"],
            },
        ),
    )
    revised = ReviewDecision(
        ReviewAction.REPLAN,
        "Replace the blocked dependency with an independent verification task.",
        revision=PlanRevision(
            base_version=3,
            keep_task_ids=("task-1",),
            cancel_task_ids=(),
            new_tasks=(
                {
                    "task_id": "task-3",
                    "title": "Independent verification",
                    "objective": "Verify the report independently",
                    "depends_on": [],
                },
            ),
        ),
    )

    class Agent:
        def __init__(self):
            """初始化 Review 调用记录。"""
            self.contexts = []

        async def review(self, value, *, request):
            """首次 Continue，收到阻断说明后改为 Replan。"""
            self.contexts.append((value, request))
            if len(self.contexts) == 1:
                return ReviewDecision(ReviewAction.CONTINUE, "Continue.")
            return revised

    class Handler(CommandHandler):
        def __init__(self):
            """创建可注入阻断结果的命令处理替身。"""
            super().__init__(None)
            self.decisions = []

        async def apply_review_decision(self, command, decision):
            """首次拒绝被阻断的 Continue，其后返回稳定回执。"""
            self.decisions.append(decision)
            if len(self.decisions) == 1:
                raise PlanCommandRejected("BLOCKED_DEPENDENCIES")
            self.applied = (command, decision)
            return CommandReceipt(
                message_id=command.message_id,
                plan_id=command.plan_id,
                plan_version=4,
                round_id="round-8",
                task_ids=("task-3",),
            )

    class BlockedContextRepository:
        async def load_review_context(self, _round_id):
            """返回包含失败依赖和受阻任务的 Round 快照。"""
            return blocked_context

    agent = Agent()
    handler = Handler()
    workflow = RootWorkflow(
        SimpleNamespace(
            review_context_repository=BlockedContextRepository(),
            orchestration_command_handler=handler,
            agent=agent,
        )
    )

    result = await workflow.review_completed_round(state())

    assert len(agent.contexts) == 2
    assert agent.contexts[0][0] == blocked_context
    assert any(
        "BLOCKED_DEPENDENCIES" in item
        for item in agent.contexts[1][0].user_constraints
    )
    assert handler.decisions == [
        ReviewDecision(ReviewAction.CONTINUE, "Continue."),
        revised,
    ]
    assert result["review_action"] == "REPLAN"
    assert handler.applied is not None
    assert handler.applied[0].command_type is OrchestrationCommandType.REVISE_PLAN


@pytest.mark.asyncio
async def test_repeated_blocked_continue_finishes_failed_within_review_budget():
    """Root 重复选择 Continue 时按有限预算结束 Plan。"""
    class Agent:
        def __init__(self):
            """初始化 Review 次数。"""
            self.calls = 0

        async def review(self, *_args, **_kwargs):
            """每次都返回 Continue，以验证安全终止分支。"""
            self.calls += 1
            return ReviewDecision(ReviewAction.CONTINUE, "Continue anyway.")

    class Handler(CommandHandler):
        def __init__(self):
            """创建记录决策并拒绝首次 Continue 的替身。"""
            super().__init__(None)
            self.decisions = []

        async def apply_review_decision(self, command, decision):
            """拒绝第一次提交，随后接受 Root 的 failed 终结决定。"""
            self.decisions.append(decision)
            if len(self.decisions) == 1:
                raise PlanCommandRejected("BLOCKED_DEPENDENCIES")
            self.applied = (command, decision)
            return CommandReceipt(
                message_id=command.message_id,
                plan_id=command.plan_id,
                plan_version=3,
                round_id="round-7",
                task_ids=(),
            )

    agent = Agent()
    handler = Handler()
    workflow = RootWorkflow(
        SimpleNamespace(
            review_context_repository=ContextRepository(),
            orchestration_command_handler=handler,
            agent=agent,
        )
    )

    result = await workflow.review_completed_round(state())

    assert agent.calls == 2
    assert len(handler.decisions) == 2
    assert handler.decisions[0].action is ReviewAction.CONTINUE
    assert handler.decisions[1].action is ReviewAction.FINISH
    assert handler.decisions[1].final_status == "failed"
    assert result["review_action"] == "FINISH"
    assert handler.applied is not None
    assert handler.applied[0].command_type is OrchestrationCommandType.FINISH_PLAN


@pytest.mark.asyncio
async def test_replan_still_blocked_finishes_failed_without_another_review_loop():
    """Replan 仍因依赖阻断时不再循环调用 Root。"""
    class Agent:
        def __init__(self):
            """初始化有限 Review 次数。"""
            self.calls = 0

        async def review(self, *_args, **_kwargs):
            """返回 Continue 后再给出仍然受阻的 Replan。"""
            self.calls += 1
            if self.calls == 1:
                return ReviewDecision(ReviewAction.CONTINUE, "Continue.")
            return ReviewDecision(
                ReviewAction.REPLAN,
                "Add a task after the blocked dependency.",
                revision=PlanRevision(
                    base_version=3,
                    keep_task_ids=("task-1",),
                    cancel_task_ids=("task-2",),
                    new_tasks=(
                        {
                            "task_id": "task-3",
                            "title": "Blocked replacement",
                            "objective": "Wait on the failed task",
                            "depends_on": ["task-2"],
                        },
                    ),
                ),
            )

    class Handler(CommandHandler):
        def __init__(self):
            """创建两次阻断后接受终结命令的替身。"""
            super().__init__(None)
            self.decisions = []

        async def apply_review_decision(self, command, decision):
            """阻断 Continue/Replan，最后接受确定性的 failed Finish。"""
            self.decisions.append(decision)
            if len(self.decisions) < 3:
                raise PlanCommandRejected("BLOCKED_DEPENDENCIES")
            self.applied = (command, decision)
            return CommandReceipt(
                message_id=command.message_id,
                plan_id=command.plan_id,
                plan_version=3,
                round_id="round-7",
                task_ids=(),
            )

    agent = Agent()
    handler = Handler()
    workflow = RootWorkflow(
        SimpleNamespace(
            review_context_repository=ContextRepository(),
            orchestration_command_handler=handler,
            agent=agent,
        )
    )

    result = await workflow.review_completed_round(state())

    assert agent.calls == 2
    assert [item.action for item in handler.decisions] == [
        ReviewAction.CONTINUE,
        ReviewAction.REPLAN,
        ReviewAction.FINISH,
    ]
    assert handler.decisions[-1].final_status == "failed"
    assert result["review_action"] == "FINISH"
