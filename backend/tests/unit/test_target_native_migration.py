from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.src.application.retrieval import HybridRetrievalService
from backend.src.application.runs.context import ContextAcquisitionService, ContextItem, ContextPlan, ProviderResult
from backend.src.application.runs.task_understanding import TaskUnderstandingService
from backend.src.domain.retrieval import RetrievalCandidate
from backend.src.domain.retrieval.evaluation import RetrievalEvaluationCase, evaluate_rankings
from backend.src.infrastructure.integrations.file_parsers.native import NativeFileAdapterRegistry
from backend.src.infrastructure.integrations.sandbox.docker_runner import DockerSandbox
from backend.src.infrastructure.tools.native import NativeToolExecution
from backend.src.domain.tools import ToolExecutionRequest
from backend.src.bootstrap.config import Settings, LLMProviderConfig
from backend.src.infrastructure.integrations.llm.provider import ConfiguredEmbeddingProvider, ConfiguredLLMProvider
from backend.src.infrastructure.integrations.neo4j.resource import Neo4jResource
from backend.src.infrastructure.observability import trace_run
from backend.src.shared import dumps, loads, new_id, now_utc
from backend.src.application.memory.worker import MemoryWorker as DurableMemoryWorker
from backend.src.infrastructure.integrations.pgvector import PostgresFileVectorIndexer, PostgresMemoryVectorIndexer


@pytest.mark.asyncio
async def test_target_context_and_task_understanding_are_bounded():
    async def provider(**_kwargs):
        return ProviderResult("memory", "succeeded", (ContextItem("memory", "one two", "m1"), ContextItem("memory", "three", "m2")))
    result = await ContextAcquisitionService({"memory": provider}).acquire(session_id="s", task=object(), plan=ContextPlan(("memory",), max_items=1, max_tokens=2))
    assert len(result.items) == 1 and result.truncated
    understood = await TaskUnderstandingService().understand(session_id="s", user_message="查找这个", attachment_refs=[{"id": "a"}])
    assert understood.source == "fast_path" and understood.task.attachment_ids == ("a",)


def test_target_retrieval_evaluation_and_entrypoint(monkeypatch):
    case = RetrievalEvaluationCase.from_ids("q", ["a", "b"])
    metrics = evaluate_rankings([case], {"q": ["x", "a", "a", "b"]}, ks=(1, 3))
    assert metrics.query_count == 1 and metrics.recall_at_k[3] == 1.0
    assert evaluate_rankings([], {}).query_count == 0
    with pytest.raises(ValueError):
        evaluate_rankings([case], {}, ks=(0,))

    import backend.src.main as entrypoint
    observed = {}
    class Uvicorn:
        @staticmethod
        def run(*args, **kwargs): observed.update(args=args, kwargs=kwargs)
    monkeypatch.setitem(sys.modules, "uvicorn", Uvicorn)
    import backend.src.bootstrap.config as target_config
    monkeypatch.setattr(target_config, "get_settings", lambda: SimpleNamespace(host="127.0.0.1", port=8123))
    entrypoint.run()
    assert observed["args"] == (entrypoint.app,)
    assert observed["kwargs"]["port"] == 8123


def test_target_retrieval_fuses_rankings_and_registry_supports_formats():
    service = HybridRetrievalService()
    values = service.fuse([RetrievalCandidate("a", "a")], [RetrievalCandidate("b", "b"), RetrievalCandidate("a", "a")], limit=2)
    assert [item.source_id for item in values] == ["a", "b"]
    registry = NativeFileAdapterRegistry()
    assert {registry.select("x.pdf", "application/pdf").name, registry.select("x.docx", "").name} == {"pdf", "word"}


@pytest.mark.asyncio
async def test_native_tools_enforce_workspace_and_execute(tmp_path: Path):
    execution = NativeToolExecution(tmp_path)
    request = ToolExecutionRequest("write_file", {"path": "a.txt", "content": "hello"}, "s", "r", "c")
    assert (await execution.execute(request)).status == "success"
    result = await execution.execute(ToolExecutionRequest("read_local_file", {"path": "a.txt"}, "s", "r", "c"))
    assert result.output == "hello"
    blocked = await execution.execute(ToolExecutionRequest("read_local_file", {"path": "../secret"}, "s", "r", "c"))
    assert blocked.status == "failed"


@pytest.mark.asyncio
async def test_docker_sandbox_has_target_lifecycle(monkeypatch, tmp_path: Path):
    settings = SimpleNamespace(sandbox_docker_binary="docker", sandbox_default_network="none", sandbox_memory_mb=64, sandbox_cpu_limit=1, sandbox_max_output_bytes=10)
    sandbox = DockerSandbox(settings)
    await sandbox.start()
    await sandbox.stop()
    assert sandbox.build_mcp_process(command="python").command == "python"

    class Process:
        def __init__(self, code=0, timeout=False): self.returncode = code; self.timeout = timeout; self.killed = False
        async def communicate(self):
            if self.timeout: raise asyncio.TimeoutError
            return b"out", b"err"
        def kill(self): self.killed = True
        async def wait(self): return self.returncode
    calls = []
    async def create(*args, **kwargs):
        calls.append(args)
        if args and args[1] == "version": return Process(0)
        return Process(0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    request = SimpleNamespace(workspace=tmp_path, image="python", argv=("-V",), timeout_seconds=1)
    assert (await sandbox.execute(request)).status == "success"
    assert await sandbox.health()
    hanging = Process(timeout=True)
    async def hanging_create(*args, **kwargs): return hanging
    monkeypatch.setattr(asyncio, "create_subprocess_exec", hanging_create)
    assert (await sandbox.execute(request)).status == "timeout" and hanging.killed
    sandbox._processes.add(Process())
    await sandbox.close_all()


def test_target_shared_helpers_and_registry_reject_unknown():
    value = new_id("x")
    assert value.startswith("x_") and now_utc().tzinfo is not None
    assert loads(dumps({"x": 1})) == {"x": 1}
    assert loads("bad", {}) == {}
    with pytest.raises(ValueError):
        NativeFileAdapterRegistry().select("x.unknown", "application/octet-stream")


@pytest.mark.asyncio
async def test_configured_local_llm_and_embedding_are_lazy():
    settings = Settings(llm_providers=[LLMProviderConfig(provider="echo", api_key="")], embedding_provider="echo", embedding_dimension=2)
    llm = ConfiguredLLMProvider(settings)
    assert (await llm.invoke(["hello"]))["content"] == "hello"
    embedding = ConfiguredEmbeddingProvider(settings)
    assert len(await embedding.embed_query("x")) == 2


@pytest.mark.asyncio
async def test_configured_llm_retries_after_lazy_model_failure():
    settings = Settings(
        llm_providers=[LLMProviderConfig(provider="echo", api_key="")],
        llm_retry={"max_attempts": 2, "timeout_ms": 1000},
    )
    provider = ConfiguredLLMProvider(settings)

    class Flaky:
        def __init__(self):
            self.calls = 0

        async def invoke(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary")
            return {"content": "ok"}

    flaky = Flaky()
    provider._model = flaky
    provider._loaded = True
    assert (await provider.invoke(["hello"]))["content"] == "ok"
    assert flaky.calls == 2


@pytest.mark.asyncio
async def test_context_unknown_and_task_understanding_fallback_paths():
    result = await ContextAcquisitionService({}).acquire(session_id="s", task=object(), plan=ContextPlan(("missing",), max_items=2, max_tokens=2))
    assert result.providers_failed == () and result.provider_results[0].status == "skipped"
    class BadLLM:
        async def generate(self, *_args, **_kwargs): raise RuntimeError("bad")
    understood = await TaskUnderstandingService(BadLLM()).understand(session_id="s", user_message="hello")
    assert understood.source == "fallback"


@pytest.mark.asyncio
async def test_observability_and_neo4j_resource_lifecycle(monkeypatch):
    async with trace_run("test") as value:
        assert value["name"] == "test"
    class Driver:
        async def close(self): self.closed = True
    class Factory:
        def driver(self, *args, **kwargs): return Driver()
    monkeypatch.setitem(__import__("sys").modules, "neo4j", SimpleNamespace(AsyncGraphDatabase=Factory()))
    settings = SimpleNamespace(neo4j_uri="bolt://x", neo4j_username="u", neo4j_password="p", neo4j_database="neo4j", neo4j_max_connection_pool_size=1, neo4j_connection_timeout_seconds=1)
    resource = Neo4jResource(settings)
    await resource.start(); assert resource.adapter is not None; await resource.stop(); assert resource.adapter is None


@pytest.mark.asyncio
async def test_durable_memory_worker_success_and_failure():
    class Jobs:
        def __init__(self): self.values = [{"turn_id": "t"}]; self.calls=[]
        async def recover(self): self.calls.append("recover")
        async def claim(self): return self.values.pop(0) if self.values else None
        async def succeed(self, key): self.calls.append(("ok", key))
        async def fail(self, *args, **kwargs): self.calls.append(("fail", args))
    class Workflow:
        async def process(self, value): return None
    jobs = Jobs(); worker = DurableMemoryWorker(jobs, Workflow(), poll_interval=0.001)
    task = asyncio.create_task(worker.run()); await asyncio.sleep(0.005); await worker.stop(); await task
    assert "recover" in jobs.calls and ("ok", "t") in jobs.calls


@pytest.mark.asyncio
async def test_native_tool_list_shell_unknown_and_failed_command(tmp_path: Path):
    execution = NativeToolExecution(tmp_path)
    await execution.execute(ToolExecutionRequest("write_file", {"path": ".hidden", "content": "x"}, "s", "r", "c"))
    listed = await execution.execute(ToolExecutionRequest("list_directory", {"path": "."}, "s", "r", "c"))
    assert listed.status == "success" and ".hidden" not in (listed.output or "")
    shell = await execution.execute(ToolExecutionRequest("exec_shell", {"command": "printf ok"}, "s", "r", "c"))
    assert shell.status == "success" and "ok" in (shell.output or "")
    failed = await execution.execute(ToolExecutionRequest("exec_shell", {"command": "exit 2"}, "s", "r", "c"))
    assert failed.status == "failed"
    assert (await execution.execute(ToolExecutionRequest("missing", {}, "s", "r", "c"))).status == "unavailable"


@pytest.mark.asyncio
async def test_configured_provider_remote_selection(monkeypatch):
    class Chat:
        def __init__(self, **kwargs): self.kwargs = kwargs
        async def invoke(self, messages, **kwargs): return {"provider": self.kwargs.get("model")}
    class Embeds:
        def __init__(self, **kwargs): self.kwargs = kwargs
        def embed_documents(self, values): return [[1.0] for _ in values]
    monkeypatch.setitem(sys.modules, "langchain_openai", SimpleNamespace(ChatOpenAI=Chat, OpenAIEmbeddings=Embeds))
    monkeypatch.setitem(sys.modules, "langchain_anthropic", SimpleNamespace(ChatAnthropic=Chat))
    monkeypatch.setitem(sys.modules, "langchain_ollama", SimpleNamespace(ChatOllama=Chat, OllamaEmbeddings=Embeds))
    for provider in ("openai", "anthropic", "ollama"):
        settings = Settings(llm_providers=[LLMProviderConfig(provider=provider, api_key="key", model="m")], embedding_provider="openai", embedding_api_key="key")
        assert (await ConfiguredLLMProvider(settings).invoke(["x"]))["provider"] == "m"
    with pytest.raises(ValueError):
        ConfiguredLLMProvider(Settings(llm_providers=[LLMProviderConfig(provider="bad", api_key="key")]))._load_model()
    for provider in ("openai", "ollama"):
        settings = Settings(embedding_provider=provider, embedding_api_key="key")
        value = ConfiguredEmbeddingProvider(settings)
        assert await value.embed_query("x") == [1.0]
    with pytest.raises(ValueError):
        ConfiguredEmbeddingProvider(Settings(embedding_provider="bad"))._load()


@pytest.mark.asyncio
async def test_target_pgvector_indexers_cover_index_delete_query():
    class Result:
        def all(self): return [(SimpleNamespace(id="c", content="text"), 0.2)]
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return False
        def begin(self): return self
        async def execute(self, statement): return Result()
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def factory(): yield Session()
    class Embed:
        async def embed_documents(self, values): return [[1.0, 2.0] for _ in values]
        async def embed_query(self, value): return [1.0, 2.0]
    file_index = PostgresFileVectorIndexer(factory, Embed())
    chunk = SimpleNamespace(id="c", content="text")
    await file_index.index(SimpleNamespace(id="a"), [chunk]); await file_index.delete("a")
    assert (await file_index.query("q"))[0]["chunk_id"] == "c"
    memory_index = PostgresMemoryVectorIndexer(factory, Embed())
    await memory_index.index("m", "memory")
    assert (await memory_index.query("q"))[0]["memory_id"] == "c"
