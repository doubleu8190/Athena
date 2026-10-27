from __future__ import annotations

from contextlib import asynccontextmanager
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
    class ThreadLock:
        @asynccontextmanager
        async def hold(self, _thread_id):
            yield

    class Checkpointer:
        def get_saver(self):
            return InMemorySaver()

    adapter = LangGraphRootGraph(
        workflow=None,
        dependencies=None,
        checkpointer=Checkpointer(),
        thread_lock=ThreadLock(),
    )
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
async def test_root_wait_node_reviews_completed_round_before_waiting_again():
    class Workflow:
        def __init__(self):
            self.wait_calls = 0
            self.reviewed = []

        async def wait_for_plan_completion(self, _state):
            self.wait_calls += 1
            if len(self.reviewed) == 0:
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
            if len(self.reviewed) == 1:
                return {
                    "waiting": True,
                    "root_wait": {
                        "type": "round_wait",
                        "run_id": "run-1",
                        "plan_id": "plan-1",
                        "round_id": "round-2",
                        "plan_version": 1,
                    },
                }
            return {"waiting": False, "plan_results": {"task-1": {"ok": True}}}

        async def review_completed_round(self, state):
            self.reviewed.append(state)

    workflow = Workflow()
    builder = StateGraph(RootGraphState)
    builder.add_node("wait", _wait_for_plan_completion_node(workflow))
    builder.add_edge(START, "wait")
    builder.add_conditional_edges(
        "wait",
        lambda state: "wait" if state.get("waiting") else END,
        {"wait": "wait", END: END},
    )
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "run:run-1"}}
    paused = await invoke_root_graph(graph, session_id="session-1", run_id="run-1")

    first_resumed = await graph.ainvoke(
        Command(
            resume={
                "type": "round_completed",
                "run_id": "run-1",
                "plan_id": "plan-1",
                "round_id": "round-1",
                "plan_version": 1,
            }
        ),
        config=config,
    )

    assert len(paused["__interrupt__"]) == 1
    assert paused["__interrupt__"][0].value["round_id"] == "round-1"
    assert len(first_resumed["__interrupt__"]) == 1
    assert first_resumed["__interrupt__"][0].value["round_id"] == "round-2"

    resumed = await graph.ainvoke(
        Command(
            resume={
                "type": "round_completed",
                "run_id": "run-1",
                "plan_id": "plan-1",
                "round_id": "round-2",
                "plan_version": 1,
            }
        ),
        config=config,
    )

    assert resumed["waiting"] is False
    assert workflow.wait_calls == 6
    assert [item["root_wait"]["round_id"] for item in workflow.reviewed] == [
        "round-1",
        "round-2",
    ]
    assert workflow.reviewed[0]["root_wait"]["round_id"] == "round-1"
    assert workflow.reviewed[0]["resume_value"]["type"] == "round_completed"


@pytest.mark.asyncio
async def test_resume_exception_fails_run_instead_of_replaying_checkpoint():
    """恢复调用发生异常时启动失败收尾，避免重放已经产生副作用的调用。"""
    class Jobs:
        def __init__(self):
            self.retries = []

        async def retry(self, lease, *, delay_seconds):
            self.retries.append(lease)

    class Graph:
        async def has_pending_round_wait(self, **_identity):
            return True

    class Execution:
        def __init__(self):
            self.failures = []

        async def execute(self, _command):
            raise RuntimeError("checkpoint failure")

        async def fail(self, command, error):
            self.failures.append((command, error))

    class Runs:
        def __init__(self):
            self.releases = []

        async def acquire_execution_lease(self, _run_id, *, owner, lease_seconds):
            return 3

        async def release_execution_lease(self, run_id, *, owner, generation):
            self.releases.append((run_id, owner, generation))
            return True

    jobs, execution, runs = Jobs(), Execution(), Runs()
    dispatcher = RootResumeDispatcher(jobs, Graph(), execution, runs, worker_id="resumer")
    lease = SimpleNamespace(job_id="job", run_id="run", session_id="session", plan_id="plan",
        round_id="round", plan_version=1, root_wait_generation=1,
        interrupt_id="interrupt", checkpoint_id="checkpoint")

    await dispatcher._resume(lease)

    assert execution.failures[0][1]["code"] == "root_checkpoint_resume_failed"
    assert jobs.retries == []
    assert runs.releases[0][2] == 3


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
async def test_root_resume_recovers_checkpoint_identity_before_dispatch():
    """Run 行登记缺失时从 checkpoint 找回身份并继续恢复 Root。

    参数：无。
    返回：无。
    异常：断言失败时由 pytest 将测试标记为失败。
    """
    class Jobs:
        def __init__(self):
            """初始化完成与重试记录；本构造函数不接收参数。"""
            self.completed = []
            self.retries = []

        async def retry(self, lease, *, delay_seconds):
            """记录待重试的 Root Resume lease。"""
            self.retries.append((lease, delay_seconds))

        async def complete(self, lease):
            """记录已完成的 Root Resume lease。"""
            self.completed.append(lease)

        async def renew(self, lease, *, lease_seconds):
            """返回当前 lease，模拟成功续租。"""
            return lease

    class Graph:
        async def find_pending_round_wait(self, **identity):
            """返回匹配 checkpoint 中的中断身份。"""
            assert identity["round_id"] == "round-1"
            return "interrupt-recovered", "checkpoint-recovered"

        async def has_pending_round_wait(self, **identity):
            """确认恢复出的中断和 checkpoint 与图状态一致。"""
            return identity == {
                "run_id": "run-1",
                "plan_id": "plan-1",
                "round_id": "round-1",
                "plan_version": 1,
                "interrupt_id": "interrupt-recovered",
                "checkpoint_id": "checkpoint-recovered",
            }

    class Execution:
        def __init__(self, runs):
            """初始化命令记录；本构造函数不接收参数。"""
            self.command = None
            self.runs = runs

        async def execute(self, command):
            """记录 Root 恢复命令并模拟执行成功。"""
            self.command = command
            self.runs.status = "completed"

    class Runs:
        def __init__(self):
            """初始化 Root wait 登记记录；本构造函数不接收参数。"""
            self.armed = []
            self.status = "running"

        async def acquire_execution_lease(self, _run_id, *, owner, lease_seconds):
            return 4

        async def renew_execution_lease(self, _run_id, **_kwargs):
            return True

        async def release_execution_lease(self, _run_id, *, owner, generation):
            return True

        async def arm_root_wait(self, run_id, **identity):
            """记录从 checkpoint 修复的 Run wait 身份。"""
            self.armed.append((run_id, identity))
            return True

        async def get(self, _run_id):
            """返回当前 Run 状态。"""
            return SimpleNamespace(status=self.status)

    jobs, runs = Jobs(), Runs()
    execution = Execution(runs)
    dispatcher = RootResumeDispatcher(
        jobs,
        Graph(),
        execution,
        runs,
        worker_id="root-resumer-1",
    )
    lease = SimpleNamespace(
        job_id="resume-unarmed",
        run_id="run-1",
        session_id="session-1",
        plan_id="plan-1",
        round_id="round-1",
        plan_version=1,
        root_wait_generation=3,
        interrupt_id=None,
        checkpoint_id=None,
        worker_id="root-resumer-1",
        lease_until=datetime.now(timezone.utc),
    )

    await dispatcher._resume(lease)

    assert runs.armed == [
        (
            "run-1",
            {
                "plan_id": "plan-1",
                "round_id": "round-1",
                "plan_version": 1,
                "interrupt_id": "interrupt-recovered",
                "checkpoint_id": "checkpoint-recovered",
            },
        )
    ]
    assert execution.command.payload.resume_value.interrupt_id == (
        "interrupt-recovered"
    )
    assert jobs.completed == [lease]
    assert jobs.retries == []


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
        def __init__(self, runs):
            self.command = None
            self.runs = runs

        async def execute(self, command):
            self.command = command
            self.runs.status = "completed"
            return {"content": "done"}

    class Runs:
        status = "running"

        async def acquire_execution_lease(self, _run_id, *, owner, lease_seconds):
            return 4

        async def renew_execution_lease(self, _run_id, **_kwargs):
            return True

        async def release_execution_lease(self, _run_id, *, owner, generation):
            return True

        async def get(self, _run_id):
            return SimpleNamespace(status=self.status)

    jobs, runs = Jobs(), Runs()
    execution = Execution(runs)
    dispatcher = RootResumeDispatcher(
        jobs,
        Graph(),
        execution,
        runs,
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
    assert execution.command.payload.resume_value.round_id == "round-1"
    assert execution.command.payload.resume_value.interrupt_id == "interrupt-1"
    assert len(jobs.completed) == 1


@pytest.mark.asyncio
async def test_plan_loader_keeps_completed_round_waiting_for_root_review():
    class Repository:
        async def get_plan_status(self, _plan_id):
            return PlanStatus.WAITING_REVIEW

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

    assert value["completed"] is False
    assert value["root_wait"]["round_id"] == "round-1"
