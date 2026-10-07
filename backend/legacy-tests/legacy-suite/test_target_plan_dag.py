import asyncio
from types import SimpleNamespace

import pytest

from athena.planning import PlanDAG, PlanDocument, PlanValidator, TaskDefinition
from athena.planning.models import TaskResult
from athena.runtime.task_executor import TaskExecutor
from athena.agents.worker import GeneralWorkerAgent, WorkerAgentRegistry


def _plan():
    return PlanDocument(nodes=[{"task_id": "a"}, {"task_id": "b"}, {"task_id": "c"}],
        edges=[{"from": "a", "to": "c"}, {"from": "b", "to": "c"}])


def test_ready_and_fan_in_are_deterministic():
    plan = _plan()
    tasks = [TaskDefinition(task_id=key, title=key, objective=key) for key in ("a", "b", "c")]
    PlanValidator().validate(plan, tasks, max_parallelism=2)
    dag = PlanDAG(plan, tasks)
    assert dag.ready_tasks({"a": "pending", "b": "pending", "c": "pending"}) == ["a", "b"]
    assert dag.ready_tasks({"a": "done", "b": "done", "c": "pending"}) == ["c"]


def test_failure_propagates_recursively():
    plan = PlanDocument(nodes=[{"task_id": "a"}, {"task_id": "b"}, {"task_id": "c"}],
        edges=[{"from": "a", "to": "b"}, {"from": "b", "to": "c"}])
    dag = PlanDAG(plan)
    states = {"a": "failed", "b": "pending", "c": "pending"}
    assert dag.propagate_dependency_failures(states) == ["b", "c"]
    assert dag.all_terminal(states)


@pytest.mark.parametrize(
    ("nodes", "edges", "tasks", "message"),
    [
        (["a", "a"], [], ["a"], "unique"),
        (["a"], [("missing", "a")], ["a"], "unknown task"),
        (["a"], [("a", "a")], ["a"], "itself"),
        (["a", "b"], [("a", "b"), ("a", "b")], ["a", "b"], "duplicate edge"),
        (["a", "b"], [("a", "b"), ("b", "a")], ["a", "b"], "cycle"),
    ],
)
def test_validator_rejects_invalid_dag(nodes, edges, tasks, message):
    plan = PlanDocument(
        nodes=[{"task_id": task_id} for task_id in nodes],
        edges=[{"from": source, "to": target} for source, target in edges],
    )
    definitions = [TaskDefinition(task_id=task_id, title=task_id, objective=task_id)
                   for task_id in tasks]
    with pytest.raises(ValueError, match=message):
        PlanValidator().validate(plan, definitions, max_parallelism=1)


class _MemoryTaskRepository:
    def __init__(self, plan, tasks):
        self.plan = SimpleNamespace(plan_id="p", run_id="r", session_id="s",
                                    max_parallelism=2,
                                    plan_json=plan.model_dump_json(by_alias=True))
        self.tasks = {task.task_id: SimpleNamespace(
            task_id=task.task_id, plan_id="p", run_id="r", title=task.title,
            objective=task.objective, input_json=__import__("json").dumps(task.input),
            expected_output_json=__import__("json").dumps(task.expected_output),
            allowed_tools_json=__import__("json").dumps(task.allowed_tools),
            worker_type=task.worker_type, execution_generation=0, status="pending",
            output_json=None, error_json=None, worker_thread_id=None,
        ) for task in tasks}

    async def get_plan_with_tasks(self, plan_id):
        return self.plan, [self.tasks[key] for key in sorted(self.tasks)]

    async def get_task(self, task_id):
        return self.tasks.get(task_id)

    async def claim_ready_tasks(self, plan_id, max_parallelism):
        states = {key: row.status for key, row in self.tasks.items()}
        plan = PlanDocument.model_validate_json(self.plan.plan_json)
        dag = PlanDAG(plan)
        running = sum(value == "running" for value in states.values())
        claimed = []
        for task_id in dag.ready_tasks(states):
            if running + len(claimed) >= max_parallelism:
                break
            row = self.tasks[task_id]
            row.status = "running"
            row.execution_generation = max(1, row.execution_generation)
            row.worker_thread_id = f"task:{task_id}:exec:{row.execution_generation}"
            claimed.append(row)
        return claimed

    async def complete_task(self, task_id, generation, output):
        row = self.tasks[task_id]
        assert row.execution_generation == generation and row.status == "running"
        row.status = "done"
        row.output_json = __import__("json").dumps(output)
        return {"accepted": True, "plan_completed": all(
            item.status in {"done", "failed", "timed_out", "cancelled"}
            for item in self.tasks.values())}

    async def fail_task(self, task_id, generation, error):
        row = self.tasks[task_id]
        assert row.execution_generation == generation and row.status == "running"
        row.status = "failed"
        row.error_json = __import__("json").dumps(error)
        return {"accepted": True, "plan_completed": False}


@pytest.mark.asyncio
async def test_executor_runs_parallel_fan_in_with_persisted_direct_results():
    plan = PlanDocument(
        nodes=[{"task_id": key} for key in ("a", "b", "c")],
        edges=[{"from": "a", "to": "c"}, {"from": "b", "to": "c"}],
    )
    tasks = [TaskDefinition(task_id=key, title=key, objective=key) for key in ("a", "b", "c")]
    repository = _MemoryTaskRepository(plan, tasks)
    observed = {}

    async def worker(context):
        observed[context.task_id] = context
        return TaskResult(task_id=context.task_id, status="done",
                          output={"value": context.task_id})

    executor = TaskExecutor(
        repository=repository,
        workers=WorkerAgentRegistry([GeneralWorkerAgent(handler=worker)]),
    )
    outcome = await executor.start_ready_tasks("p")
    assert outcome["started_task_ids"] == ["a", "b"]
    for _ in range(20):
        if all(row.status == "done" for row in repository.tasks.values()):
            break
        await asyncio.sleep(0.01)

    assert all(row.status == "done" for row in repository.tasks.values())
    assert list(observed["c"].upstream_results) == ["a", "b"]
    assert observed["c"].upstream_results["a"].output == {"value": "a"}
    assert observed["c"].upstream_results["b"].output == {"value": "b"}


@pytest.mark.asyncio
async def test_worker_exception_is_reported_as_terminal_task_failure():
    plan = PlanDocument(nodes=[{"task_id": "a"}])
    task = TaskDefinition(task_id="a", title="a", objective="a")
    repository = _MemoryTaskRepository(plan, [task])

    async def worker(_context):
        raise RuntimeError("worker broke")

    executor = TaskExecutor(
        repository=repository,
        workers=WorkerAgentRegistry([GeneralWorkerAgent(handler=worker)]),
    )
    await executor.start_ready_tasks("p")
    for _ in range(10):
        if repository.tasks["a"].status == "failed":
            break
        await asyncio.sleep(0.01)
    assert repository.tasks["a"].status == "failed"
    assert "worker broke" in repository.tasks["a"].error_json


def test_worker_agent_owns_one_graph_per_worker_type():
    first = GeneralWorkerAgent()
    second = GeneralWorkerAgent()

    assert first.get_graph() is first.get_graph()
    assert second.get_graph() is second.get_graph()
    assert first.get_graph() is not second.get_graph()
