"""Tests for target-owned external integration boundaries."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.src.infrastructure.integrations.llm.provider import (
    RetryPolicy,
    TargetEmbeddingProvider,
    TargetLLMProvider,
)
from backend.src.infrastructure.integrations.neo4j.adapter import Neo4jGraphAdapter
from backend.src.infrastructure.integrations.sandbox.subprocess_runner import SubprocessSandbox


@pytest.mark.asyncio
async def test_target_llm_retries_and_counts_tokens():
    class Model:
        def __init__(self): self.calls = 0
        async def invoke(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1: raise RuntimeError("temporary")
            return {"messages": messages}
    model = Model()
    provider = TargetLLMProvider(model, policy=RetryPolicy(attempts=2, timeout_seconds=1))
    assert (await provider.invoke(["hello"]))["messages"] == ["hello"]
    assert model.calls == 2
    assert provider.count_text_tokens("one two") == 2

    async def embed(texts): return [[float(len(texts[0]))]]
    embeddings = TargetEmbeddingProvider(embed, dimension=1)
    assert await embeddings.embed_query("abcd") == [4.0]
    assert await embeddings.embed_documents([]) == []
    with pytest.raises(ValueError):
        await TargetEmbeddingProvider(lambda values: [], dimension=1).embed_query("x")


@pytest.mark.asyncio
async def test_target_llm_timeout_and_failure_are_bounded():
    class Model:
        async def invoke(self, *_args, **_kwargs):
            await asyncio.sleep(0.02)
    with pytest.raises(RuntimeError):
        await TargetLLMProvider(Model(), policy=RetryPolicy(attempts=1, timeout_seconds=0.001)).invoke([])


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
