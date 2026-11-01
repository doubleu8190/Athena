from __future__ import annotations

from types import SimpleNamespace

import pytest

from application.orchestration.root_workflow import RootWorkflow
from domain.orchestration import WorkerPlanReceipt
from tests.fakes.leases import lease_context, lease_grant


class RootOrchestration:
    def __init__(self) -> None:
        self.submissions = []

    async def submit_plan(self, submission, *, lease):
        assert lease.key.resource_type == "root_execution"
        assert lease.key.resource_id == submission.run_id
        self.submissions.append(submission)
        return WorkerPlanReceipt(
            plan_id=submission.plan_id,
            run_id=submission.run_id,
            sequence=submission.sequence,
            accepted=True,
            replayed=len(self.submissions) > 1,
        )


def _workflow(orchestration: RootOrchestration) -> RootWorkflow:
    return RootWorkflow(SimpleNamespace(root_orchestration=orchestration))


def _state(*, completed_plans=(), tasks=None, extra=None):
    return {
        "request": {
            "run_id": "run-42",
            "session_id": "session-9",
            "user_message": "Prepare a report with evidence.",
        },
        "execution_lease": lease_context(lease_grant("root_execution", "run-42")),
        "completed_plans": list(completed_plans),
        "plan_draft": {
            "action": "plan",
            "goal": "Prepare a report",
            "tasks": tasks or [
                {
                    "task_ref": "gather",
                    "contract_id": "research",
                    "contract_revision": 3,
                    "objective": "Collect source data",
                    "input": {"topic": "workspace"},
                    "acceptance_criteria": "Cite source IDs",
                },
                {
                    "task_ref": "summarize",
                    "contract_id": "writer",
                    "contract_revision": 2,
                    "objective": "Summarize collected source data",
                    "input": {"source": "prior run facts"},
                    "acceptance_criteria": "Return a concise report",
                },
            ],
            **(extra or {}),
        },
    }


@pytest.mark.asyncio
async def test_root_submits_one_independent_plan_through_orchestration_port():
    orchestration = RootOrchestration()
    workflow = _workflow(orchestration)
    state = _state()

    result = await workflow.materialize_execution_plan(state)

    assert len(orchestration.submissions) == 1
    submission = orchestration.submissions[0]
    assert submission.run_id == "run-42"
    assert submission.sequence == 1
    assert submission.idempotency_key == "root-plan:run-42:1"
    assert submission.plan_id == result["plan"]["plan_id"]
    assert [task.task_ref for task in submission.tasks] == ["gather", "summarize"]
    assert submission.tasks[0].contract_id == "research"
    assert submission.tasks[0].contract_revision == 3
    assert submission.tasks[1].input == {"source": "prior run facts"}
    assert result["plan"]["root_wait_generation"] == 1


@pytest.mark.asyncio
async def test_plan_submission_identity_replays_stably_after_checkpoint_retry():
    orchestration = RootOrchestration()
    workflow = _workflow(orchestration)
    state = _state()

    first = await workflow.materialize_execution_plan(state)
    second = await workflow.materialize_execution_plan(state)

    assert first["plan"] == second["plan"]
    assert orchestration.submissions[0].idempotency_key == orchestration.submissions[1].idempotency_key
    assert orchestration.submissions[0].plan_id == orchestration.submissions[1].plan_id


@pytest.mark.asyncio
async def test_next_plan_sequence_is_derived_from_completed_plan_facts():
    orchestration = RootOrchestration()
    workflow = _workflow(orchestration)
    previous = {"plan_id": "old-plan", "sequence": 1, "status": "failed"}

    result = await workflow.materialize_execution_plan(_state(completed_plans=[previous]))

    submission = orchestration.submissions[0]
    assert submission.sequence == 2
    assert submission.idempotency_key == "root-plan:run-42:2"
    assert result["plan"]["root_wait_generation"] == 2


@pytest.mark.asyncio
async def test_root_rejects_dag_edges_and_future_plan_version_fields():
    orchestration = RootOrchestration()
    workflow = _workflow(orchestration)

    with pytest.raises(ValueError):
        await workflow.materialize_execution_plan(
            _state(extra={"edges": [{"from_task_id": "gather", "to_task_id": "summarize"}]})
        )

    assert orchestration.submissions == []
