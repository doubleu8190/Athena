from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from application.orchestration.langgraph.graphs.root import (
    _wait_for_plan_completion_node,
    invoke_root_graph,
)
from application.orchestration.langgraph.graphs.state import RootGraphState
from application.orchestration.langgraph.runtime.root_adapter import LangGraphRootGraph
from domain.orchestration import PlanStatus
from infrastructure.persistence.postgres.repositories.orchestration_repository import (
    PostgresPlanLoader,
)
from workers.root_resume_dispatcher import RootResumeDispatcher


@pytest.mark.asyncio
async def test_root_wait_node_interrupts_and_rechecks_after_resume():
    class Workflow:
        def __init__(self):
            self.calls = 0

        async def wait_for_plan_completion(self, _state):
            self.calls += 1
            if self.calls == 1:
                return {
                    "waiting": True,
                    "root_wait": {
                        "type": "round_wait",
                        "run_id": "run-1",
                        "plan_id": "plan-1",
                        "round_id": "round-1",
                        "plan_version": 1,
                    },
                }
            return {"waiting": False, "plan_results": {"task-1": {"ok": True}}}

    workflow = Workflow()
    builder = StateGraph(RootGraphState)
    builder.add_node("wait", _wait_for_plan_completion_node(workflow))
    builder.add_edge(START, "wait")
    builder.add_edge("wait", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "run:run-1"}}

    paused = await invoke_root_graph(
        graph, session_id="session-1", run_id="run-1"
    )

    assert len(paused["__interrupt__"]) == 1
    assert paused["__interrupt__"][0].value["round_id"] == "round-1"
    assert paused["__root_checkpoint_id__"]
    adapter = LangGraphRootGraph(workflow=None, dependencies=None)
    adapter._graph = graph
    identity = {
        "run_id": "run-1",
        "plan_id": "plan-1",
        "round_id": "round-1",
        "plan_version": 1,
        "interrupt_id": paused["__interrupt__"][0].id,
        "checkpoint_id": paused["__root_checkpoint_id__"],
    }
    assert await adapter.has_pending_round_wait(**identity) is True
    assert await adapter.has_pending_round_wait(
        **{**identity, "interrupt_id": "stale-interrupt"}
    ) is False
    assert await adapter.has_pending_round_wait(
        **{**identity, "checkpoint_id": "stale-checkpoint"}
    ) is False
    resumed = await graph.ainvoke(
        Command(
            resume={
                "type": "round_completed",
                "run_id": "run-1",
                "plan_id": "plan-1",
                "round_id": "round-1",
            }
        ),
        config=config,
    )

    assert resumed["waiting"] is False
    assert resumed["plan_results"] == {"task-1": {"ok": True}}


@pytest.mark.asyncio
async def test_root_resume_dispatcher_retries_until_matching_interrupt_exists():
    class Jobs:
        def __init__(self):
            self.retries = []
            self.completed = []

        async def retry(self, lease, *, delay_seconds):
            self.retries.append((lease, delay_seconds))
            return True

        async def complete(self, lease):
            self.completed.append(lease)
            return True

        async def renew(self, lease, *, lease_seconds):
            return lease

    class Graph:
        async def has_pending_round_wait(self, **_identity):
            return False

    class Execution:
        async def execute(self, _command):
            raise AssertionError("must not invoke a Root without its interrupt")

    class Runs:
        async def get(self, _run_id):
            return SimpleNamespace(status="completed")

    jobs = Jobs()
    dispatcher = RootResumeDispatcher(
        jobs,
        Graph(),
        Execution(),
        Runs(),
        worker_id="root-resumer-1",
    )
    lease = SimpleNamespace(
        job_id="resume-1",
        run_id="run-1",
        session_id="session-1",
        plan_id="plan-1",
        round_id="round-1",
        plan_version=1,
        root_wait_generation=3,
        interrupt_id="interrupt-1",
        checkpoint_id="checkpoint-1",
        worker_id="root-resumer-1",
        lease_until=datetime.now(timezone.utc),
    )

    await dispatcher._resume(lease)

    assert len(jobs.retries) == 1
    assert jobs.completed == []


@pytest.mark.asyncio
async def test_root_resume_dispatcher_uses_same_run_thread_and_completes_job():
    class Jobs:
        def __init__(self):
            self.completed = []

        async def retry(self, *_args, **_kwargs):
            raise AssertionError("matching checkpoint should resume")

        async def complete(self, lease):
            self.completed.append(lease)
            return True

        async def renew(self, lease, *, lease_seconds):
            return lease

    class Graph:
        async def has_pending_round_wait(self, **identity):
            return identity == {
                "run_id": "run-1",
                "plan_id": "plan-1",
                "round_id": "round-1",
                "plan_version": 1,
                "interrupt_id": "interrupt-1",
                "checkpoint_id": "checkpoint-1",
            }

    class Execution:
        def __init__(self):
            self.command = None

        async def execute(self, command):
            self.command = command
            return {"content": "done"}

    class Runs:
        async def get(self, _run_id):
            return SimpleNamespace(status="completed")

    jobs, execution = Jobs(), Execution()
    dispatcher = RootResumeDispatcher(
        jobs,
        Graph(),
        execution,
        Runs(),
        worker_id="root-resumer-1",
    )
    lease = SimpleNamespace(
        job_id="resume-1",
        run_id="run-1",
        session_id="session-1",
        plan_id="plan-1",
        round_id="round-1",
        plan_version=1,
        root_wait_generation=3,
        interrupt_id="interrupt-1",
        checkpoint_id="checkpoint-1",
        worker_id="root-resumer-1",
        lease_until=datetime.now(timezone.utc),
    )

    await dispatcher._resume(lease)

    assert execution.command.run_id == "run-1"
    assert execution.command.payload["resume_value"]["round_id"] == "round-1"
    assert execution.command.payload["resume_value"]["interrupt_id"] == "interrupt-1"
    assert len(jobs.completed) == 1


@pytest.mark.asyncio
async def test_plan_loader_treats_completed_round_as_ready_for_root_review():
    class Repository:
        async def get(self, _plan_id):
            return SimpleNamespace(status=PlanStatus.WAITING_REVIEW), []

        async def get_results(self, _plan_id):
            return {"task-1": {"status": "completed"}}

        async def get_wait_context(self, _plan_id):
            return {
                "type": "round_wait",
                "run_id": "run-1",
                "plan_id": "plan-1",
                "round_id": "round-1",
                "plan_version": 1,
            }

    value = await PostgresPlanLoader(Repository())(plan_id="plan-1")

    assert value["completed"] is True
    assert value["root_wait"]["round_id"] == "round-1"
