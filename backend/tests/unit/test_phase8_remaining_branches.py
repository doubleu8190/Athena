"""Exercise remaining lifecycle and orchestration branches."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from application.orchestration import WorkerSchedulerService
from application.runs import RunExecutionService, RunQueryService
from workers import CommandConsumer
from application.sessions import SessionNotFoundError
from bootstrap import create_app
from bootstrap.lifespan import lifespan
from domain.events import ApplicationEvent
from domain.orchestration import Plan, Task, TaskExecution, TaskResult, TaskStatus
from domain.runs import CommandType, RunCommand, RunSummary
from domain.sessions import Session
from fastapi.testclient import TestClient

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_lifespan_context_manager_resources_and_failed_cleanup() -> None:
    events = []
    class Context:
        async def __aenter__(self): events.append("enter")
        async def __aexit__(self, *args): events.append("exit")
    class App:
        state = SimpleNamespace()
    async with lifespan(App(), [Context()]):
        events.append("body")
    assert events == ["enter", "body", "exit"]
    class Broken:
        async def start(self): raise RuntimeError("broken")
    with pytest.raises(RuntimeError):
        async with lifespan(App(), [Broken()]):
            pass


@pytest.mark.asyncio
async def test_run_query_and_command_consumer_error_paths() -> None:
    class Sessions:
        async def get(self, key): return Session.create(session_id="s", title="s", now=NOW) if key == "s" else None
    class Runs:
        async def list_for_session(self, key): return []
        async def get(self, key): return RunSummary("r", "s", "running", "t") if key == "r" else None
    service = RunQueryService(Sessions(), Runs())
    assert await service.list_for_session("s") == []
    with pytest.raises(SessionNotFoundError): await service.list_for_session("missing")
    with pytest.raises(LookupError): await service.get("missing")
    class Queue:
        def __init__(self): self.claimed = False
        async def claim_next_command(self):
            if self.claimed: return None
            self.claimed = True
            return RunCommand("c", CommandType.RUN_START, "s", "r", {}, NOW)
    class Execution:
        async def execute(self, command): raise RuntimeError("fail")
        async def fail(self, command, error): self.error = error
    consumer = CommandConsumer(Queue(), Execution(), poll_interval=0.001)
    task = asyncio.create_task(consumer.run()); await asyncio.sleep(0.005); consumer._stop.set(); await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_scheduler_duplicate_wait_reject_resume_and_synthesize_branches() -> None:
    execution = TaskExecution("p", "r", "s", Task("t", "t", "do"), 1)
    class Repo:
        def __init__(self): self.mode = "reject"; self.tasks = [execution]
        async def get(self, key): return (Plan("p", "g", ("t",), session_id="s"), [])
        async def claim_ready_tasks(self, *args): return self.tasks
        async def finish_task(self, *args, **kwargs): return {"accepted": self.mode != "reject", "plan_completed": False}
        async def set_task_waiting_approval(self, *args): self.waiting = True
        async def set_task_running(self, *args): return self.mode != "not_waiting"
        async def retry_task(self, *args): return None
        async def get_task_execution(self, key): return None if self.mode == "missing" else execution
        async def get_results(self, key): return {}
    class Worker:
        async def execute(self, value): return TaskResult("t", TaskStatus.WAITING_APPROVAL, approval_batch={"a": 1})
        async def resume(self, value, decisions): return TaskResult("t", TaskStatus.DONE, output={})
        async def cancel(self, value): return 1
    repo = Repo(); scheduler = WorkerSchedulerService(repo, Worker())
    await scheduler.start_ready_tasks("p")
    await scheduler.start_ready_tasks("p")
    await asyncio.sleep(0.01)
    repo = Repo(); scheduler = WorkerSchedulerService(repo, Worker())
    repo.mode = "missing"
    assert await scheduler.resume_task("missing", {}) == {"accepted": False, "reason": "worker_not_found"}
    repo.mode = "not_waiting"
    assert await scheduler.resume_worker(execution, {}) == {"accepted": False, "reason": "task_not_waiting_approval"}


@pytest.mark.asyncio
async def test_scheduler_worker_exception_and_rejected_finish_paths() -> None:
    execution = TaskExecution("p", "r", "s", Task("t", "t", "do"), 1)

    class Repo:
        async def get(self, key):
            return Plan("p", "g", ("t",), session_id="s"), []

        async def claim_ready_tasks(self, *args):
            return [execution]

        async def finish_task(self, *args, **kwargs):
            return {"accepted": False, "plan_completed": False}

        async def set_task_waiting_approval(self, *args):
            raise AssertionError("approval path should not run")

        async def set_task_running(self, *args):
            return True

        async def retry_task(self, *args):
            return None

        async def get_task_execution(self, key):
            return execution

        async def get_results(self, key):
            return {}

    class Worker:
        async def execute(self, value):
            raise RuntimeError("boom")

        async def resume(self, value, decisions):
            return TaskResult("t", TaskStatus.FAILED, error={"reason": "rejected"})

        async def cancel(self, value):
            return 0

    repo = Repo()
    scheduler = WorkerSchedulerService(repo, Worker())
    await scheduler.start_ready_tasks("p")
    await asyncio.sleep(0.01)
    assert await scheduler.resume_worker(execution, {}) == {
        "accepted": False,
        "plan_completed": False,
    }
