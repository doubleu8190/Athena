"""阶段 6 Worker 调度、重试、审批等待和结果汇总测试。"""

from __future__ import annotations

import asyncio

import pytest

from application.orchestration import RootExecutionService, WorkerSchedulerService
from domain.orchestration import (
    Plan,
    Task,
    TaskExecution,
    TaskResult,
    TaskStatus,
)


def execution(generation: int = 1) -> TaskExecution:
    return TaskExecution(
        plan_id="plan-1",
        run_id="run-1",
        session_id="session-1",
        task=Task("task-1", "Task", "execute"),
        execution_generation=generation,
    )


class Repository:
    def __init__(self, claimed=None, *, retry=None):
        self.plan = Plan("plan-1", "goal", ("task-1",), session_id="session-1", run_id="run-1")
        self.claimed = list(claimed or [execution()])
        self.retry = retry
        self.finished = []
        self.waiting = []
        self.running = []
        self.results = {}

    async def get(self, plan_id):
        return self.plan, [Task("task-1", "Task", "execute")]

    async def claim_ready_tasks(self, plan_id, max_parallelism):
        if not self.claimed:
            return []
        return [self.claimed.pop(0)]

    async def finish_task(self, task_id, generation, *, status, output=None, error=None):
        self.finished.append((task_id, generation, status, output, error))
        self.results[task_id] = TaskResult(task_id, TaskStatus(status), output or {}, error=error)
        return {"accepted": True, "plan_completed": status in {"done", "failed"}}

    async def set_task_waiting_approval(self, task_id, generation, approval_batch=None):
        self.waiting.append((task_id, generation, approval_batch))
        return True

    async def set_task_running(self, task_id, generation):
        self.running.append((task_id, generation))
        return True

    async def retry_task(self, task_id):
        if self.retry is None:
            return None
        self.claimed.append(execution(2))
        return {"task_id": task_id, "execution_generation": 2}

    async def get_results(self, plan_id):
        return self.results

    async def get_task_execution(self, task_id):
        return execution()


class Worker:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.cancelled = []

    async def execute(self, value):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def resume(self, value, decisions):
        return self.outcomes.pop(0)

    async def cancel(self, run_id):
        self.cancelled.append(run_id)
        return 1


class Synthesizer:
    async def synthesize(self, plan, results):
        return f"{plan.plan_id}:{sorted(results)}"


@pytest.mark.asyncio
async def test_scheduler_runs_worker_and_synthesizes_terminal_results() -> None:
    repository = Repository()
    worker = Worker([TaskResult("task-1", TaskStatus.DONE, output={"ok": True})])
    service = WorkerSchedulerService(repository, worker, synthesizer=Synthesizer())

    accepted = await service.start_ready_tasks("plan-1")
    assert accepted["started_task_ids"] == ["task-1"]
    for _ in range(20):
        if service.synthesized_result("plan-1"):
            break
        await asyncio.sleep(0.01)

    assert repository.finished[0][2] == "done"
    assert service.synthesized_result("plan-1") == "plan-1:['task-1']"


@pytest.mark.asyncio
async def test_scheduler_retries_retryable_worker_failure() -> None:
    repository = Repository(retry=True)
    worker = Worker([
        TaskResult("task-1", TaskStatus.FAILED, error={"message": "temporary"}, retryable=True),
        TaskResult("task-1", TaskStatus.DONE, output={"ok": True}),
    ])
    service = WorkerSchedulerService(repository, worker, max_retries=1)

    await service.start_ready_tasks("plan-1")
    for _ in range(30):
        if len(repository.finished) >= 2:
            break
        await asyncio.sleep(0.01)

    assert [item[2] for item in repository.finished] == ["failed", "done"]


@pytest.mark.asyncio
async def test_scheduler_persists_approval_wait_and_resumes_same_generation() -> None:
    repository = Repository()
    worker = Worker([
        TaskResult("task-1", TaskStatus.WAITING_APPROVAL, approval_batch={"approval_id": "a"}),
        TaskResult("task-1", TaskStatus.DONE, output={"approved": True}),
    ])
    service = WorkerSchedulerService(repository, worker)

    await service.start_ready_tasks("plan-1")
    for _ in range(20):
        if repository.waiting:
            break
        await asyncio.sleep(0.01)
    result = await service.resume_worker(execution(), {"a": "approved"})

    assert repository.waiting[0][2] == {"approval_id": "a"}
    assert repository.running == [("task-1", 1)]
    assert result["accepted"] is True


@pytest.mark.asyncio
async def test_scheduler_can_resume_by_task_id() -> None:
    repository = Repository()
    worker = Worker([TaskResult("task-1", TaskStatus.DONE, output={"approved": True})])
    service = WorkerSchedulerService(repository, worker)

    result = await service.resume_task("task-1", {"a": "approved"})

    assert result["accepted"] is True
    assert repository.running == [("task-1", 1)]


@pytest.mark.asyncio
async def test_root_execution_service_delegates_to_graph_port() -> None:
    class Graph:
        async def invoke(self, **kwargs):
            return kwargs

    service = RootExecutionService(Graph())
    result = await service.execute(session_id="s", run_id="r", user_message="hello")
    assert result["session_id"] == "s"
    assert result["run_id"] == "r"
