"""编排持久化和调度边界。"""

from __future__ import annotations

from typing import Mapping, Protocol

from .entities import Plan, Task, TaskExecution, TaskResult


class PlanRepository(Protocol):
    async def create(self, plan: Plan, tasks: list[Task]) -> Plan: ...

    async def get(self, plan_id: str) -> tuple[Plan, list[Task]] | None: ...

    async def claim_ready_tasks(
        self, plan_id: str, max_parallelism: int
    ) -> list[TaskExecution]: ...

    async def finish_task(
        self,
        task_id: str,
        execution_generation: int,
        *,
        status: str,
        output: dict[str, object] | None = None,
        error: dict[str, object] | None = None,
    ) -> dict[str, object]: ...

    async def set_task_waiting_approval(
        self,
        task_id: str,
        execution_generation: int,
        approval_batch: dict[str, object] | None = None,
    ) -> bool: ...

    async def set_task_running(self, task_id: str, execution_generation: int) -> bool: ...

    async def retry_task(self, task_id: str) -> dict[str, object] | None: ...

    async def get_results(self, plan_id: str) -> Mapping[str, TaskResult]: ...

    async def get_task_execution(self, task_id: str) -> TaskExecution | None: ...


class PlanScheduler(Protocol):
    async def start_ready_tasks(self, plan_id: str) -> dict[str, object]: ...


class WorkerExecutor(Protocol):
    async def execute(self, execution: TaskExecution) -> TaskResult: ...

    async def resume(
        self,
        execution: TaskExecution,
        decisions: dict[str, str],
    ) -> TaskResult: ...

    async def cancel(self, run_id: str) -> int: ...


class ResultSynthesizer(Protocol):
    async def synthesize(
        self,
        plan: Plan,
        results: Mapping[str, TaskResult],
    ) -> str: ...


class RootGraphPort(Protocol):
    async def invoke(
        self,
        *,
        session_id: str,
        run_id: str,
        user_message: str,
        message_id: str = "",
        attachment_ids: list[str] | None = None,
        resume_value: dict[str, object] | None = None,
    ) -> object: ...


__all__ = [
    "PlanRepository",
    "PlanScheduler",
    "ResultSynthesizer",
    "RootGraphPort",
    "WorkerExecutor",
]
