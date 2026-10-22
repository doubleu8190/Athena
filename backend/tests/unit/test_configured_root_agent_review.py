from __future__ import annotations

import json

import pytest

from application.orchestration.langgraph.runtime.root_agent import (
    ConfiguredRootAgent,
)
from domain.orchestration import ReviewAction, ReviewContext


class ReviewLLM:
    def __init__(self, response: object):
        self.response = response
        self.messages = None

    async def invoke(self, messages):
        self.messages = messages
        return {"content": self.response}


class RepairingReviewLLM:
    def __init__(self, responses: list[str]):
        self.responses = iter(responses)
        self.calls = []

    async def invoke(self, messages):
        self.calls.append(messages)
        return {"content": next(self.responses)}


def review_context() -> ReviewContext:
    return ReviewContext(
        original_goal="Prepare an evidence-backed report",
        plan_id="plan-1",
        plan_version=3,
        round_id="round-7",
        full_dag_summary={"nodes": [{"task_id": "task-a"}], "edges": []},
        round_results=({"task_id": "task-a", "status": "completed"},),
    )


@pytest.mark.asyncio
async def test_configured_root_agent_decodes_review_decision_and_sends_round_context():
    llm = ReviewLLM(
        json.dumps(
            {
                "action": "REPLAN",
                "reason": "The evidence shows another check is needed.",
                "revision": {
                    "base_version": 3,
                    "keep_task_ids": ["task-a"],
                    "cancel_task_ids": [],
                    "new_tasks": [
                        {
                            "task_id": "task-b",
                            "title": "Verify metric",
                            "instruction": "Check the metric source",
                            "depends_on": ["task-a"],
                        }
                    ],
                },
            }
        )
    )
    agent = ConfiguredRootAgent(llm)

    decision = await agent.review(
        review_context(), request={"run_id": "run-1", "session_id": "session-1"}
    )

    assert decision.action is ReviewAction.REPLAN
    assert decision.revision is not None
    assert decision.revision.base_version == 3
    user_payload = json.loads(llm.messages[1]["content"])
    assert user_payload["round_id"] == "round-7"
    assert user_payload["round_results"][0]["task_id"] == "task-a"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        '{"action":"CONTINUE","reason":"Keep going."}',
        '{"action":"FINISH","reason":"The goal is met.","final_status":"completed"}',
    ],
)
async def test_configured_root_agent_accepts_continue_and_finish(response: str):
    decision = await ConfiguredRootAgent(ReviewLLM(response)).review(review_context())

    assert decision.action in {ReviewAction.CONTINUE, ReviewAction.FINISH}


@pytest.mark.asyncio
async def test_configured_root_agent_repairs_review_missing_required_reason():
    llm = RepairingReviewLLM(
        [
            '{"action":"FINISH","final_status":"completed"}',
            '{"action":"FINISH","reason":"The requested goal is complete.","final_status":"completed"}',
        ]
    )

    decision = await ConfiguredRootAgent(llm).review(review_context())

    assert decision.action is ReviewAction.FINISH
    assert decision.reason == "The requested goal is complete."
    assert len(llm.calls) == 2
    assert "non-empty reason" in llm.calls[1][0]["content"]


@pytest.mark.asyncio
async def test_configured_root_agent_rejects_revision_for_another_plan_version():
    llm = ReviewLLM(
        {
            "action": "REPLAN",
            "reason": "Need more work.",
            "revision": {
                "base_version": 2,
                "keep_task_ids": ["task-a"],
                "cancel_task_ids": [],
                "new_tasks": [],
            },
        }
    )

    with pytest.raises(ValueError, match="review_revision_base_version_mismatch"):
        await ConfiguredRootAgent(llm).review(review_context())


@pytest.mark.asyncio
async def test_configured_root_agent_rejects_malformed_review_json():
    with pytest.raises(ValueError, match="root_review_response_invalid_json"):
        await ConfiguredRootAgent(ReviewLLM("not JSON")).review(review_context())
