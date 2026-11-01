from __future__ import annotations

from types import SimpleNamespace

import pytest

from application.orchestration.root_decision import AnswerIntent, PlanIntent
from application.orchestration.root_workflow import RootWorkflow
from application.runs.context import ContextBundle, ContextPlanner
from application.orchestration.root_completed_plan import (
    RootCompletedPlanModel,
    RootPlanTaskFactModel,
)
from tests.fakes.leases import lease_context, lease_grant


class Agent:
    def __init__(self, decision):
        self.decision = decision
        self.inputs = []

    async def redecide(self, inputs):
        self.inputs.append(inputs)
        return self.decision


def _state():
    return {
        "request": {
            "run_id": "run-1",
            "session_id": "session-1",
            "message_id": "message-1",
            "user_message": "Prepare a report",
        },
        "execution_lease": lease_context(lease_grant("root_execution", "run-1")),
        "completed_plans": [
            RootCompletedPlanModel(
                run_id="run-1",
                plan_id="plan-1",
                sequence=1,
                root_wait_generation=1,
                goal="Prepare a report",
                status="failed",
                tasks=(
                    RootPlanTaskFactModel(
                        task_ref="task-1",
                        status="failed",
                        error={"code": "source_unavailable"},
                    ),
                ),
            ).model_dump(mode="json")
        ],
        "root_turn_count": 0,
        "context_bundle": {"items": [], "provider_results": []},
        "context_queries": [],
    }


def _workflow(agent):
    class ContextAcquisition:
        async def validate_memory_context(self, bundle):
            return bundle

        async def persist_revalidated_selection(self, _bundle):
            return None

    return RootWorkflow(
        SimpleNamespace(
            agent=agent,
            context_acquisition=ContextAcquisition(),
            context_planner=ContextPlanner(),
            root_max_turns=4,
            root_orchestration=None,
            available_context_providers=(),
            memory_settings=None,
        )
    )


@pytest.mark.asyncio
async def test_root_redecides_from_typed_completed_plan_facts() -> None:
    agent = Agent(
        PlanIntent(
            action="plan",
            goal="Use another source",
            instructions="Avoid the unavailable source.",
        )
    )

    result = await _workflow(agent).redecide_root_action(_state())

    assert result["root_decision"]["action"] == "plan"
    assert agent.inputs[0].completed_plans[0].tasks[0].error == {
        "code": "source_unavailable"
    }


@pytest.mark.asyncio
async def test_root_redecision_can_finish_after_partial_failure() -> None:
    agent = Agent(
        AnswerIntent(
            action="answer",
            kind="final",
            instructions="Summarize the successful and failed tasks.",
        )
    )

    result = await _workflow(agent).redecide_root_action(_state())

    assert result["root_decision"]["action"] == "answer"
    assert result["answer_input"]["source"] == "decision"
