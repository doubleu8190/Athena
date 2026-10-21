from __future__ import annotations

import asyncio

import pytest

from application.orchestration import OrchestrationService, WorkerSchedulerService
from application.orchestration.root_workflow import RootWorkflow
from application.runs.context.planner import ContextPlanner
from domain.orchestration import Plan, PlanEdge, PlanStatus, Task, TaskExecution, TaskResult, TaskStatus
from application.orchestration.langgraph import RootGraphDependencies, build_root_graph, invoke_root_graph


class Repository:
    def __init__(self) -> None:
        self.plan: Plan | None = None
        self.tasks: dict[str, tuple[Task, TaskStatus, int, dict]] = {}

    async def create(self, plan: Plan, tasks: list[Task]) -> Plan:
        self.plan = plan
        self.tasks = {task.task_id: (task, TaskStatus.PENDING, 0, {}) for task in tasks}
        return plan

    async def get(self, plan_id: str):
        if self.plan is None or self.plan.plan_id != plan_id:
            return None
        return self.plan, [item[0] for item in self.tasks.values()]

    async def claim_ready_tasks(self, plan_id: str, max_parallelism: int):
        assert self.plan is not None
        ready = []
        for task_id, (task, status, generation, output) in sorted(self.tasks.items()):
            if status is not TaskStatus.PENDING:
                continue
            parents = {edge.from_task_id for edge in self.plan.edges if edge.to_task_id == task_id}
            if any(self.tasks[parent][1] is not TaskStatus.DONE for parent in parents):
                continue
            if len(ready) >= max_parallelism:
                break
            generation += 1
            self.tasks[task_id] = (task, TaskStatus.RUNNING, generation, output)
            ready.append(TaskExecution(self.plan.plan_id, self.plan.run_id or "", self.plan.session_id or "", task, generation))
        if ready:
            self.plan = Plan(self.plan.plan_id, self.plan.goal, self.plan.task_ids, self.plan.edges, self.plan.max_parallelism, PlanStatus.RUNNING, self.plan.session_id, self.plan.run_id)
        return ready

    async def finish_task(self, task_id: str, execution_generation: int, *, status: str, output=None, error=None, finalize_plan=True):
        task, current, generation, _ = self.tasks[task_id]
        if current is not TaskStatus.RUNNING or generation != execution_generation:
            return {"accepted": False}
        self.tasks[task_id] = (task, TaskStatus(status), generation, output or {})
        terminal = all(value[1] in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.TIMED_OUT} for value in self.tasks.values())
        if terminal and finalize_plan and self.plan is not None:
            status_value = "completed" if all(value[1] is TaskStatus.DONE for value in self.tasks.values()) else "failed"
            self.plan = Plan(self.plan.plan_id, self.plan.goal, self.plan.task_ids, self.plan.edges, self.plan.max_parallelism, PlanStatus(status_value), self.plan.session_id, self.plan.run_id)
        return {"accepted": True, "plan_completed": terminal}

    async def set_task_waiting_approval(self, *args, **kwargs):
        return True

    async def set_task_running(self, *args, **kwargs):
        return True

    async def retry_task(self, task_id):
        return None

    async def get_results(self, plan_id):
        return {task_id: TaskResult(task_id, value[1], output=value[3]) for task_id, value in self.tasks.items()}

    async def get_task_execution(self, task_id):
        task, status, generation, output = self.tasks[task_id]
        assert self.plan is not None
        return TaskExecution(self.plan.plan_id, self.plan.run_id or "", self.plan.session_id or "", task, generation)


class Worker:
    async def execute(self, execution: TaskExecution) -> TaskResult:
        await asyncio.sleep(0)
        return TaskResult(execution.task.task_id, TaskStatus.DONE, {"worker": execution.task.task_id})

    async def resume(self, execution, decisions):
        return await self.execute(execution)

    async def cancel(self, run_id):
        return 0


class RequestPreparation:
    async def prepare(self, *, request, **kwargs):
        return {"request": dict(request)}

    async def load_history(self, **kwargs):
        return []

    async def load_attachments(self, **kwargs):
        return []

    async def load_knowledge_bases(self, request):
        return []


class ContextAcquisition:
    async def retrieve_context(self, **kwargs):
        return {}


class ToolService:
    async def list_tools(self):
        return []


class EventPublisher:
    async def publish(self, event):
        return event


@pytest.mark.asyncio
async def test_root_runs_and_aggregates_two_workers() -> None:
    repository = Repository()
    scheduler = WorkerSchedulerService(repository, Worker())

    async def plan_builder(**kwargs):
        return {
            "plan_id": "plan:e2e",
            "goal": "parallel",
            "max_parallelism": 2,
            "tasks": [
                {"task_id": "worker-a", "title": "A", "objective": "analyze"},
                {"task_id": "worker-b", "title": "B", "objective": "review"},
            ],
        }

    async def load_plan(*, plan_id, **kwargs):
        results = await repository.get_results(plan_id)
        return {"completed": repository.plan is not None and repository.plan.status.value == "completed", "results": results}

    class Understanding:
        async def understand(self, **kwargs):
            return {
                "task": {
                    "goal": "parallel",
                    "mode": "plan",
                    "context_requirements": ["conversation"],
                }
            }

    class Agent:
        async def __call__(self, **kwargs):
            return {"next_route": "materialize_execution_plan"}

    dependencies = RootGraphDependencies(
        task_understanding=Understanding(),
        context_acquisition=ContextAcquisition(),
        context_planner=ContextPlanner(),
        agent=Agent(),
        orchestration=OrchestrationService(repository),
        plan_repository=repository,
        worker_scheduler=scheduler,
        request_preparation=RequestPreparation(),
        event_publisher=EventPublisher(),
        tool_service=ToolService(),
        approval_service=None,
        plan_builder=plan_builder,
        plan_loader=load_plan,
    )
    graph = build_root_graph(
        workflow=RootWorkflow(dependencies), dependencies=dependencies
    )
    result = await invoke_root_graph(graph, session_id="s", run_id="r", user_message="parallel")
    assert result["status"] == "completed", result
    assert set(result["plan_results"]) == {"worker-a:r", "worker-b:r"}
    assert all(value.status is TaskStatus.DONE for value in (await repository.get_results("plan:e2e")).values())
