from __future__ import annotations

import pytest

from application.orchestration.agents.root_agent import ConfiguredRootAgent
from application.orchestration.root_decision import (
    AnswerIntent,
    RootAgentInput,
    RootMessage,
    RootWorkerCapability,
)
from application.orchestration.root_completed_plan import RootCompletedPlanModel, RootPlanTaskFactModel


class StructuredLLM:
    def __init__(self, response: object):
        self.response = response
        self.messages = None
        self.schema = None

    async def invoke_structured(self, messages, schema):
        self.messages = messages
        self.schema = schema
        return self.response


def root_input() -> RootAgentInput:
    return RootAgentInput(
        session_id="session-1",
        run_id="run-1",
        message_id="message-1",
        messages=(RootMessage(role="user", content="Prepare a report"),),
        worker_capabilities=(
            RootWorkerCapability(
                contract_id="research",
                contract_revision=3,
                can_do=("Find source material",),
                cannot_do=("Invent citations",),
                input_schema={"type": "object"},
                output_schema={"type": "object"},
            ),
        ),
        completed_plans=(
            RootCompletedPlanModel(
                run_id="run-1",
                plan_id="plan-1",
                sequence=1,
                root_wait_generation=1,
                goal="Prepare a report",
                status="failed",
                tasks=(
                    RootPlanTaskFactModel(
                        task_ref="research-1",
                        status="failed",
                        error={"code": "source_unavailable"},
                        resource_ids=("artifact-1",),
                    ),
                ),
            ),
        ),
        remaining_turns=2,
    )


@pytest.mark.asyncio
async def test_root_redecides_from_completed_plan_facts_and_authorized_capabilities():
    llm = StructuredLLM(
        {
            "action": "plan",
            "goal": "Use another source and finish the report",
            "instructions": "Avoid the unavailable source",
        }
    )

    decision = await ConfiguredRootAgent(llm).redecide(root_input())

    assert decision.action == "plan"
    assert llm.schema.__name__ == "RootReDecisionStructuredOutput"
    prompt = llm.messages[0]["content"]
    assert "completed_plans" in prompt
    assert "source_unavailable" in prompt
    assert "artifact-1" in prompt
    assert '"contract_id": "research"' in prompt
    assert "worker_tools" not in prompt
    assert "Round Review" not in prompt


@pytest.mark.asyncio
async def test_root_redecision_can_finish_after_partial_failure():
    llm = StructuredLLM(
        {
            "action": "answer",
            "kind": "final",
            "instructions": "Summarize available evidence and identify the failed task.",
        }
    )

    decision = await ConfiguredRootAgent(llm).redecide(root_input())

    assert isinstance(decision, AnswerIntent)
    assert '"status": "failed"' in llm.messages[0]["content"]
