"""PostgreSQL acceptance for Worker approval checkpoint recovery."""

from __future__ import annotations

import os
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from psycopg.conninfo import make_conninfo
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from application.orchestration.langgraph.runtime.worker_executor import (
    LangGraphWorkerExecutor,
)
from application.orchestration.langgraph.graphs.worker import (
    build_worker_graph,
    invoke_worker_graph,
)
from application.orchestration.worker_tools import WorkerToolExecutor
from application.orchestration.worker_workflow import WorkerWorkflow
from domain.orchestration import Task, TaskExecution, TaskStatus
from domain.tools import ToolExecutionRequest, ToolExecutionResult
from infrastructure.persistence.postgres.checkpointer import PostgresCheckpointer


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


class _ApprovalToolService:
    """为 checkpoint 验收提供一个需要人工审批的工具。"""

    def __init__(self) -> None:
        """初始化工具执行记录；不接收参数，也不主动抛出业务异常。"""
        self.executed: list[ToolExecutionRequest] = []

    async def get(self, name: str) -> Any:
        """返回高风险工具配置。

        参数：name 是 Worker 请求查询的工具名。返回启用且需审批的配置。
        不接收未知工具名时抛出异常；验收只会查询 write_file。
        """
        return SimpleNamespace(
            name=name,
            enabled=True,
            risk_level="high",
            require_approval=True,
            execution_mode="native",
        )

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        """记录获批的工具调用并返回确定性成功结果。

        参数：request 是带有原始参数和审批决定的工具请求。返回固定成功结果。
        此测试替身不会主动抛出业务异常。
        """
        self.executed.append(request)
        return ToolExecutionResult(
            status="success",
            output="written",
            duration_ms=1,
        )


class _WorkerAgent:
    """先申请工具审批，恢复后再完成任务的确定性 Worker。"""

    def __init__(self) -> None:
        """将 LLM 调用次数置零；不接收参数，也不主动抛出业务异常。"""
        self.calls = 0
        self.observed_tool_results: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        execution: Mapping[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """首次请求获批工具，第二次调用结束任务。

        参数：execution 是当前 Worker 执行快照；其他关键字参数由工作流提供。
        返回 Worker 图可持久化的 JSON 数据。本替身不主动抛出业务异常。
        """
        self.calls += 1
        tool_results = execution.get("tool_results", [])
        if not tool_results:
            return {
                "status": "running",
                "tool_calls": [
                    {
                        "name": "write_file",
                        "arguments": {
                            "path": "report.txt",
                            "content": "ready",
                        },
                    }
                ],
            }
        self.observed_tool_results = [
            dict(result)
            for result in tool_results
            if isinstance(result, Mapping)
        ]
        return {"status": "done", "output": {"confirmed": True}}


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected_tool_status", "expected_error"),
    [
        ("approved", "success", None),
        ("denied", "denied", "approval_denied"),
        ("expired", "denied", "approval_expired"),
    ],
)
async def test_worker_approval_resumes_after_postgres_checkpointer_restart(
    decision: str,
    expected_tool_status: str,
    expected_error: str | None,
) -> None:
    """重建 checkpointer 和 executor 后找回 interrupt 并继续同一执行。

    该测试需要可连接且允许创建临时 schema 的 PostgreSQL 测试库；
    测试结束会删除 schema。
    """
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"worker_checkpoint_test_{uuid4().hex}"
    base_url = make_url(DATABASE_URL).set(drivername="postgresql")
    checkpoint_options: dict[str, str | int] = {
        "host": base_url.host or "",
        "port": base_url.port or 5432,
        "dbname": base_url.database or "",
        "user": base_url.username or "",
        "password": base_url.password or "",
        "options": f"-c search_path={schema}",
    }
    checkpoint_options.update(
        {
            name: value
            for name, value in base_url.query.items()
            if isinstance(value, (str, int)) and name != "options"
        }
    )
    checkpoint_connection_string = make_conninfo(**checkpoint_options)
    base_engine = create_async_engine(
        base_url.set(drivername="postgresql+psycopg")
    )
    schema_created = False
    checkpointers: list[PostgresCheckpointer] = []

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True

        tools = _ApprovalToolService()
        tool_executor = WorkerToolExecutor(tools)
        agent = _WorkerAgent()

        def workflow_factory() -> WorkerWorkflow:
            """创建连接共享测试替身的新工作流。"""
            return WorkerWorkflow(execute=agent, execute_tool=tool_executor)

        execution = TaskExecution(
            plan_id="plan-1",
            run_id="run-1",
            session_id="session-1",
            task=Task(
                "task-1",
                "Write report",
                "Write the requested report",
                allowed_tools=("write_file",),
            ),
            execution_generation=1,
            execution_id="execution-1",
            worker_thread_id="task:task-1:exec:1",
        )

        first_checkpointer = PostgresCheckpointer(
            checkpoint_connection_string
        )
        checkpointers.append(first_checkpointer)
        await first_checkpointer.start()
        first_graph = build_worker_graph(
            workflow=workflow_factory(),
            checkpointer=first_checkpointer.get_saver(),
        )
        paused = await invoke_worker_graph(
            first_graph,
            task_id=execution.task.task_id,
            execution_generation=execution.execution_generation,
            execution=execution,
        )

        interrupts = paused["__interrupt__"]
        assert len(interrupts) == 1
        interrupt_id = str(interrupts[0].id)
        approval = dict(interrupts[0].value)
        assert approval["tool_name"] == "write_file"
        assert tools.executed == []
        assert agent.calls == 1

        await first_checkpointer.stop()
        second_checkpointer = PostgresCheckpointer(
            checkpoint_connection_string
        )
        checkpointers.append(second_checkpointer)
        await second_checkpointer.start()
        recovered_executor = LangGraphWorkerExecutor(
            workflow_factory=workflow_factory,
            checkpointer=second_checkpointer,
        )
        # 模拟新 app 进程：checkpointer 和 Worker agent 都重新创建。
        agent = _WorkerAgent()

        recovered_approval = await recovered_executor.pending_interrupt(execution)

        assert recovered_approval is not None
        assert recovered_approval["interrupt_id"] == interrupt_id
        assert recovered_approval["approval_id"] == approval["approval_id"]
        resumed = await recovered_executor.resume(
            execution,
            {
                **recovered_approval,
                "decision": decision,
            },
        )

        assert resumed.status is TaskStatus.DONE
        assert resumed.output == {"confirmed": True}
        assert len(agent.observed_tool_results) == 1
        tool_result = agent.observed_tool_results[0]
        assert tool_result["status"] == expected_tool_status
        if decision == "approved":
            assert len(tools.executed) == 1
            assert tools.executed[0].arguments == {
                "path": "report.txt",
                "content": "ready",
            }
            assert tools.executed[0].approval_decision == "approved"
        else:
            assert tools.executed == []
            assert tool_result["error"]["code"] == expected_error
        assert agent.calls == 1
    finally:
        for checkpointer in reversed(checkpointers):
            await checkpointer.stop()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()
