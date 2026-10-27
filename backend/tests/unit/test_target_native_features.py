from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain.retrieval.fusion import RetrievalRankFusion
from domain.memory import ClaimedMemoryJob, CompletedTurn
from application.runs.context import ContextAcquisitionService, ContextItem, ContextPlan, ProviderResult
from domain.retrieval import RetrievalCandidate
from domain.retrieval.evaluation import RetrievalEvaluationCase, evaluate_rankings
from infrastructure.integrations.file_parsers.native import NativeFileAdapterRegistry
from infrastructure.integrations.sandbox.docker_runner import DockerSandbox
from infrastructure.tools.native import NativeToolExecution
from domain.tools import ToolExecutionRequest
from configuration.settings import Settings, LLMProviderConfig
from infrastructure.integrations.llm.embedding_provider import ConfiguredEmbeddingProvider, EmbeddingProvider
from infrastructure.integrations.llm.llm_provider import ConfiguredLLMProvider
from infrastructure.integrations.neo4j.resource import Neo4jResource
from shared import dumps, loads, new_id, now_utc
from workers import MemoryWorker as DurableMemoryWorker
from infrastructure.integrations.pgvector import PostgresFileVectorIndex, PostgresMemoryVectorIndex
from tests.fakes.embedding import _EchoEmbedder
from tests.fakes.leases import LeaseCenter, lease_grant


class _ContextEvents:
    async def publish(self, _event):
        return None


class _ContextTrace:
    async def record_context(self, **_kwargs):
        return "retrieval-test"


class _ContextProvider:
    def __init__(self, handler):
        self.handler = handler

    async def retrieve_context(self, **kwargs):
        return await self.handler(**kwargs)


def _context_service(providers):
    return ContextAcquisitionService(
        {name: _ContextProvider(provider) for name, provider in providers.items()},
        trace_repository=_ContextTrace(),
        event_publisher=_ContextEvents(),
    )


@pytest.mark.asyncio
async def test_target_context_acquisition_is_bounded():
    async def provider(**_kwargs):
        return ProviderResult("memory", "succeeded", (ContextItem("memory", "one two", "m1"), ContextItem("memory", "three", "m2")))
    result = await _context_service({"memory": provider}).retrieve_context(
        session_id="s",
        run_id="r",
        invocation_id="call-1",
        stage="directed",
        plan=ContextPlan(("memory",), max_items=1, max_tokens=2),
    )
    assert len(result.items) == 1 and result.truncated


def test_target_retrieval_evaluation_and_entrypoint(monkeypatch):
    case = RetrievalEvaluationCase.from_ids("q", ["a", "b"])
    metrics = evaluate_rankings([case], {"q": ["x", "a", "a", "b"]}, ks=(1, 3))
    assert metrics.query_count == 1 and metrics.recall_at_k[3] == 1.0
    assert evaluate_rankings([], {}).query_count == 0
    with pytest.raises(ValueError):
        evaluate_rankings([case], {}, ks=(0,))

    import  main as entrypoint
    observed = {}
    class Uvicorn:
        @staticmethod
        def run(*args, **kwargs): observed.update(args=args, kwargs=kwargs)
    monkeypatch.setitem(sys.modules, "uvicorn", Uvicorn)
    import configuration.settings as target_config
    monkeypatch.setattr(target_config, "get_settings", lambda: SimpleNamespace(host="127.0.0.1", port=8123))
    entrypoint.run()
    assert observed["args"] == (entrypoint.app,)
    assert observed["kwargs"]["port"] == 8123


def test_target_retrieval_fuses_rankings_and_registry_supports_formats():
    service = RetrievalRankFusion()
    values = service.fuse_ranked_candidates([RetrievalCandidate("a", "a")], [RetrievalCandidate("b", "b"), RetrievalCandidate("a", "a")], limit=2)
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
async def test_exec_shell_uses_configured_docker_sandbox(tmp_path: Path):
    class Sandbox:
        def __init__(self, result):
            self.result = result
            self.request = None

        async def execute(self, request):
            self.request = request
            return self.result

    sandbox = Sandbox(SimpleNamespace(status="success", exit_code=0, stdout="DOCKER_STDOUT", stderr=""))
    execution = NativeToolExecution(
        tmp_path,
        sandbox=sandbox,
        sandbox_enabled=True,
        sandbox_image="python:test",
        sandbox_max_timeout=12,
    )
    result = await execution.execute(
        ToolExecutionRequest("exec_shell", {"command": "printf DOCKER_STDOUT", "timeout": 30}, "s", "r", "c")
    )
    assert result.status == "success" and result.output == "DOCKER_STDOUT"
    assert sandbox.request.workspace.parent == (tmp_path / "sandboxes").resolve()
    assert sandbox.request.workspace.name != ""
    assert sandbox.request.workspace.is_dir()
    first_workspace = sandbox.request.workspace
    assert sandbox.request.image == "python:test"
    assert sandbox.request.argv == ("/bin/sh", "-lc", "printf DOCKER_STDOUT")
    assert sandbox.request.timeout_seconds == 12

    second = await execution.execute(
        ToolExecutionRequest("exec_shell", {"command": "printf OTHER_RUN"}, "s", "other-run", "c2")
    )
    assert second.status == "success"
    assert sandbox.request.workspace != first_workspace


@pytest.mark.asyncio
async def test_exec_shell_does_not_fall_back_to_host_when_docker_is_unavailable(tmp_path: Path):
    class Sandbox:
        async def execute(self, _request):
            return SimpleNamespace(status="runner_unavailable", exit_code=None, stdout="", stderr="docker socket unavailable")

    result = await NativeToolExecution(tmp_path, sandbox=Sandbox(), sandbox_enabled=True).execute(
        ToolExecutionRequest("exec_shell", {"command": "printf SHOULD_NOT_RUN_ON_HOST"}, "s", "r", "c")
    )
    assert result.status == "sandbox_unavailable"
    assert result.error == "docker socket unavailable"


@pytest.mark.asyncio
async def test_exec_shell_uses_host_only_when_sandbox_is_explicitly_disabled(tmp_path: Path):
    class Sandbox:
        async def execute(self, _request):
            raise AssertionError("disabled sandbox must not be called")

    result = await NativeToolExecution(tmp_path, sandbox=Sandbox(), sandbox_enabled=False).execute(
        ToolExecutionRequest("exec_shell", {"command": "printf LOCAL_ONLY"}, "s", "r", "c")
    )
    assert result.status == "success" and result.output == "LOCAL_ONLY"


@pytest.mark.asyncio
async def test_required_sandbox_cannot_be_disabled(tmp_path: Path):
    class Sandbox:
        async def execute(self, _request):
            return SimpleNamespace(status="runner_unavailable", exit_code=None, stdout="", stderr="docker unavailable")

    result = await NativeToolExecution(
        tmp_path, sandbox=Sandbox(), sandbox_enabled=False, sandbox_required=True
    ).execute(ToolExecutionRequest("exec_shell", {"command": "printf NO_HOST_FALLBACK"}, "s", "r", "c"))
    assert result.status == "sandbox_unavailable"


@pytest.mark.asyncio
async def test_docker_sandbox_has_target_lifecycle(monkeypatch, tmp_path: Path):
    settings = SimpleNamespace(sandbox_docker_binary="docker", sandbox_default_network="none", sandbox_memory_mb=64, sandbox_cpu_limit=1, sandbox_max_output_bytes=10)
    settings.sandbox_pids_limit = 48
    sandbox = DockerSandbox(settings)
    await sandbox.start()
    await sandbox.stop()
    assert sandbox.build_mcp_process(command="python").command == "python"

    class Process:
        def __init__(self, code=0, timeout=False):
            self.returncode = None if timeout else code
            self.timeout = timeout
            self.killed = False
            self.waited = False
            self.terminated = asyncio.Event()
        async def communicate(self):
            if self.timeout: await self.terminated.wait()
            return b"out", b"err"
        def kill(self): self.killed = True
        async def wait(self):
            self.waited = True
            self.returncode = -15 if self.timeout else self.returncode
            self.terminated.set()
            return self.returncode
        pid = 99999999
    calls = []
    async def create(*args, **kwargs):
        calls.append(args)
        if args and args[1] == "version": return Process(0)
        return Process(0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    request = SimpleNamespace(workspace=tmp_path, image="python", argv=("-V",), timeout_seconds=1)
    assert (await sandbox.execute(request)).status == "success"
    docker_args = calls[0]
    assert "--pids-limit" in docker_args and "48" in docker_args
    assert await sandbox.health()
    hanging = Process(timeout=True)
    async def hanging_create(*args, **kwargs): return hanging
    monkeypatch.setattr(asyncio, "create_subprocess_exec", hanging_create)
    assert (await sandbox.execute(request)).status == "timeout" and hanging.waited
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
async def test_configured_providers_reject_unsupported_provider_names():
    settings = Settings(llm_providers=[LLMProviderConfig(provider="echo", api_key="")], embedding_provider="echo", embedding_dimension=2)
    with pytest.raises(ValueError, match="Unsupported"):
        ConfiguredLLMProvider(settings)
    with pytest.raises(ValueError, match="Unsupported"):
        await ConfiguredEmbeddingProvider(settings).embed_query("x")
    embedding = EmbeddingProvider(_EchoEmbedder(2), dimension=2)
    assert len(await embedding.embed_query("x")) == 2


def test_configured_openai_llm_counts_tokens_with_model_tokenizer():
    settings = Settings(llm_providers=[LLMProviderConfig(provider="openai", api_key="key", model="gpt-4o")])
    provider = ConfiguredLLMProvider(settings)
    provider._model = SimpleNamespace()
    assert provider.count_text_tokens("你好世界") == 2


@pytest.mark.asyncio
async def test_configured_llm_retries_after_lazy_model_failure():
    settings = Settings(
        llm_providers=[LLMProviderConfig(provider="ollama", api_key="")],
        llm_retry={"max_attempts": 2, "timeout_ms": 1000},
    )
    provider = ConfiguredLLMProvider(settings)

    class Flaky:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary")
            return {"content": "ok"}

    flaky = Flaky()
    provider._model = flaky
    assert (await provider.invoke(["hello"]))["content"] == "ok"
    assert flaky.calls == 2


@pytest.mark.asyncio
async def test_context_unknown_provider_is_skipped():
    result = await _context_service({}).retrieve_context(
        session_id="s",
        run_id="r",
        invocation_id="call-2",
        stage="directed",
        plan=ContextPlan(("missing",), max_items=2, max_tokens=2),
    )
    assert result.providers_failed == () and result.provider_results[0].status == "skipped"


@pytest.mark.asyncio
async def test_neo4j_resource_lifecycle(monkeypatch):
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
        def __init__(self):
            self._leases = LeaseCenter()
            self.values = [ClaimedMemoryJob(
                CompletedTurn("t", "", ""), 1,
                lease_grant("memory_processing", "t"),
            )]
            self.calls=[]
        async def claim_next_job(self, *, lease_seconds): return self.values.pop(0) if self.values else None
        async def mark_succeeded(self, key, *, lease): self.calls.append(("ok", key))
        async def mark_failed(self, *args, **kwargs): self.calls.append(("fail", args))
    class Workflow:
        async def process_completed_turn(self, value, *, lease): return None
    jobs = Jobs(); worker = DurableMemoryWorker(jobs, Workflow(), poll_interval=0.001, lease_center=jobs._leases)
    task = asyncio.create_task(worker.run()); await asyncio.sleep(0.005); worker.stop(); await task
    assert ("ok", "t") in jobs.calls


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
        async def ainvoke(self, messages, **kwargs): return {"provider": self.kwargs.get("model")}
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
        async def scalar(self, statement):
            return SimpleNamespace(deleted_time=None, source_turn_id="t")
        async def scalars(self, statement):
            return SimpleNamespace(all=lambda: [])
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def factory(): yield Session()
    class Embed:
        async def embed_documents(self, values): return [[1.0, 2.0] for _ in values]
        async def embed_query(self, value): return [1.0, 2.0]
    file_index = PostgresFileVectorIndex(factory, Embed(), lease_center=LeaseCenter())
    chunk = SimpleNamespace(id="c", attachment_id="a", content="text", index_revision_id="revision-1")
    await file_index.index_attachment(
        SimpleNamespace(id="a"), [chunk],
        lease=lease_grant("document_ingestion", "a"),
    ); await file_index.delete_attachment("a")
    assert (await file_index.query("q"))[0]["chunk_id"] == "c"
    memory_index = PostgresMemoryVectorIndex(factory, Embed(), lease_center=LeaseCenter())
    await memory_index.index("m", "memory", lease=lease_grant("memory_processing", "t"))
    assert (await memory_index.query("q"))[0]["memory_id"] == "c"
