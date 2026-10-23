"""Tests for target-owned external integration boundaries."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from application.common.responses import ToolResponse
from application.orchestration.agents.worker_agent import ConfiguredWorkerAgent
from application.orchestration.langgraph.runtime.worker_executor import LangGraphWorkerExecutor
from application.orchestration.worker_workflow import WorkerWorkflow
from domain.orchestration import Task, TaskExecution
from infrastructure.integrations.llm.embedding_provider import EmbeddingProvider
from infrastructure.integrations.llm.llm_provider import ConfiguredLLMProvider
from infrastructure.integrations.llm.options import (
    LLMOptions,
    LLMProviderOptions,
    RetryOptions,
)
from infrastructure.integrations.neo4j.adapter import Neo4jGraphAdapter
from infrastructure.integrations.sandbox.docker_runner import DockerSandbox
from infrastructure.integrations.sandbox.options import SandboxOptions
from infrastructure.integrations.sandbox.subprocess_runner import SubprocessSandbox
from infrastructure.tools.native import BuiltinToolExecutor
from domain.tools import ToolExecutionRequest


def _configured_llm_for_test(
    model: object,
    *,
    attempts: int = 3,
    timeout_seconds: float = 1.0,
) -> ConfiguredLLMProvider:
    provider = ConfiguredLLMProvider(
        LLMOptions(
            providers=(LLMProviderOptions(provider="ollama"),),
            retry=RetryOptions(
                attempts=attempts,
                timeout_seconds=timeout_seconds,
                delay_seconds=0,
            ),
        )
    )
    provider._model = model
    return provider


@pytest.mark.asyncio
async def test_target_llm_retries_and_counts_tokens():
    class Model:
        custom_get_token_ids = None

        def __init__(self): self.calls = 0

        def get_num_tokens(self, text):
            return len(text.split())

        async def ainvoke(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1: raise RuntimeError("temporary")
            return {"messages": messages}
    model = Model()
    provider = _configured_llm_for_test(model, attempts=2)
    assert (await provider.invoke(["hello"]))["messages"] == ["hello"]
    assert model.calls == 2
    assert provider.count_text_tokens("one two") == 2

    async def embed(texts): return [[float(len(texts[0]))]]
    embeddings = EmbeddingProvider(embed, dimension=1)
    assert await embeddings.embed_query("abcd") == [4.0]
    assert await embeddings.embed_documents([]) == []
    with pytest.raises(ValueError):
        await EmbeddingProvider(lambda values: [], dimension=1).embed_query("x")


@pytest.mark.asyncio
async def test_target_llm_timeout_and_failure_are_bounded():
    class Model:
        async def ainvoke(self, *_args, **_kwargs):
            await asyncio.sleep(0.02)
    with pytest.raises(RuntimeError):
        await _configured_llm_for_test(
            Model(), attempts=1, timeout_seconds=0.001
        ).invoke([])


@pytest.mark.asyncio
async def test_sync_llm_and_embedding_calls_do_not_block_the_event_loop():
    class SyncModel:
        def invoke(self, messages, **kwargs):
            time.sleep(0.08)
            return {"messages": messages}

        async def ainvoke(self, messages, **kwargs):
            return await asyncio.to_thread(self.invoke, messages, **kwargs)

    model = SyncModel()
    provider = _configured_llm_for_test(model, attempts=1)
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        deadline = asyncio.get_running_loop().time() + 0.06
        while asyncio.get_running_loop().time() < deadline:
            ticks += 1
            await asyncio.sleep(0.005)

    result, _ = await asyncio.gather(provider.invoke(["hello"]), heartbeat())
    assert result["messages"] == ["hello"]
    assert ticks >= 5

    thread_id = threading.get_ident()

    def sync_embed(texts):
        assert threading.get_ident() != thread_id
        time.sleep(0.04)
        return [[float(len(texts[0]))]]

    embeddings = EmbeddingProvider(sync_embed, dimension=1)
    ticks = 0
    vector, _ = await asyncio.gather(embeddings.embed_query("abcd"), heartbeat())
    assert vector == [4.0]
    assert ticks >= 5


@pytest.mark.asyncio
async def test_subprocess_sandbox_success_failure_timeout_and_lifecycle(tmp_path: Path):
    sandbox = SubprocessSandbox(max_output_bytes=10)
    request = SimpleNamespace(argv=("/bin/echo", "hello"), workspace=tmp_path, env={}, timeout_seconds=1)
    result = await sandbox.execute(request)
    assert result.status == "success" and result.stdout.strip() == "hello"
    missing = SimpleNamespace(argv=("/missing-athena-command",), workspace=tmp_path, env={}, timeout_seconds=1)
    assert (await sandbox.execute(missing)).status == "runner_unavailable"
    timeout = SimpleNamespace(argv=("/bin/sh", "-c", "sleep 1"), workspace=tmp_path, env={}, timeout_seconds=0.001)
    assert (await sandbox.execute(timeout)).status == "timeout"
    assert await sandbox.health()
    assert sandbox.build_mcp_process(command="python", args=["-V"], env={}).command == "python"
    await sandbox.close_all()


@pytest.mark.asyncio
async def test_subprocess_sandbox_cancellation_stops_child_processes(tmp_path: Path):
    sandbox = SubprocessSandbox()
    marker = tmp_path / "late-child-effect.txt"
    request = SimpleNamespace(
        argv=("/bin/sh", "-c", f"sleep 0.5; touch {marker}"),
        workspace=tmp_path,
        env={},
        timeout_seconds=2,
    )
    execution = asyncio.create_task(sandbox.execute(request))
    await asyncio.sleep(0.05)
    execution.cancel()

    with pytest.raises(asyncio.CancelledError):
        await execution
    await asyncio.sleep(0.6)

    assert marker.exists() is False
    assert sandbox._processes == set()


@pytest.mark.asyncio
async def test_docker_sandbox_can_run_read_only_without_workspace_mount(
    monkeypatch, tmp_path: Path
):
    calls = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"3.12.14\n", b""

    async def create(*args, **_kwargs):
        calls.append(args)
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    sandbox = DockerSandbox(
        SandboxOptions(
            mount_workspace=False,
            read_only_root=True,
            default_network="none",
            memory_mb=512,
            cpu_limit=1,
            pids_limit=64,
        )
    )
    result = await sandbox.execute(
        SimpleNamespace(
            workspace=tmp_path,
            image="python:3.12-slim",
            argv=(
                "/bin/sh",
                "-lc",
                "python -c 'import sys; print(sys.version.split()[0])'",
            ),
            timeout_seconds=10,
        )
    )

    docker_args = calls[0]
    assert result.status == "success"
    assert result.stdout == "3.12.14\n"
    assert docker_args[1:4] == ("run", "--rm", "--network")
    assert "none" in docker_args
    assert "--read-only" in docker_args
    assert "--memory" in docker_args and "512m" in docker_args
    assert "--cpus" in docker_args and "1" in docker_args
    assert "--pids-limit" in docker_args and "64" in docker_args
    assert "-v" not in docker_args and "-w" not in docker_args


@pytest.mark.asyncio
async def test_worker_graph_passes_allowed_tool_manifest_to_worker_model():
    """验证 Worker Graph 会把任务获准的工具和审批策略交给模型。"""

    class ToolService:
        async def list_tools(self) -> list[ToolResponse]:
            """返回测试用的唯一高风险工具配置。"""
            return [
                ToolResponse(
                    name="exec_shell",
                    description="Run a sandboxed command.",
                    risk_level="high",
                    execution_mode="native",
                    require_approval=True,
                    enabled=True,
                    parameters={
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                    },
                )
            ]

    class CaptureLLM:
        def __init__(self) -> None:
            """初始化用于记录 Worker 输入的模拟模型。"""
            self.messages: list[dict[str, Any]] = []

        async def invoke(
            self, messages: list[dict[str, Any]]
        ) -> dict[str, str]:
            """保存提示并返回不触发工具执行的测试结果。"""
            self.messages = messages
            return {"content": json.dumps({"output": "mock"})}

    llm = CaptureLLM()
    worker_agent = ConfiguredWorkerAgent(llm, tool_service=ToolService())
    executor = LangGraphWorkerExecutor(
        workflow_factory=lambda: WorkerWorkflow(execute=worker_agent)
    )
    task = Task(
        task_id="task-1",
        title="Run one approved command",
        objective="Request exec_shell once.",
        allowed_tools=("exec_shell",),
    )

    await executor.execute(
        TaskExecution(
            plan_id="plan-1",
            run_id="run-1",
            session_id="session-1",
            task=task,
            execution_generation=1,
        )
    )

    assert '"name": "exec_shell"' in llm.messages[0]["content"]
    assert '"allowed_tools": ["exec_shell"]' in llm.messages[1]["content"]


@pytest.mark.asyncio
async def test_worker_shell_cancellation_stops_child_processes(tmp_path: Path):
    marker = tmp_path / "late-worker-effect.txt"
    executor = BuiltinToolExecutor(tmp_path)
    request = ToolExecutionRequest(
        tool_name="exec_shell",
        arguments={"command": f"sleep 0.5; touch {marker}"},
        session_id="session-1",
        run_id="run-1",
        tool_call_id="tool-call-1",
        agent_role="worker",
    )
    execution = asyncio.create_task(executor.execute(request))
    await asyncio.sleep(0.05)
    execution.cancel()

    with pytest.raises(asyncio.CancelledError):
        await execution
    await asyncio.sleep(0.6)

    assert marker.exists() is False


@pytest.mark.asyncio
async def test_neo4j_adapter_health_projection_and_close():
    class Result:
        async def single(self): return {"health": 1}
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return False
        async def run(self, query, **kwargs):
            self.query = query
            return Result()
    class Driver:
        def __init__(self): self.closed = False
        def session(self, **kwargs): return Session()
        async def close(self): self.closed = True
    driver = Driver()
    adapter = Neo4jGraphAdapter(driver)
    assert await adapter.health()
    chunk = SimpleNamespace(id="c", content="text")
    await adapter.index_attachment(SimpleNamespace(id="a"), [chunk])
    await adapter.delete_attachment("a")
    await adapter.close()
    assert driver.closed
