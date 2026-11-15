"""验证显式注入记忆服务不会被默认关闭的环境开关遮蔽。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.memory.extraction import ExtractionLimits
from app.memory.service import MemoryService
from app.project.service import ProjectService
from bootstrap.tool import build_query_services, build_run_tool_runtime
from domain.tool import AuthorizedRunContext


class _McpCatalog:
    """提供空 MCP 目录，突出内置记忆工具是否被装配。"""

    async def list_runtime_tools(self, *, limit: int) -> tuple[object, ...]:
        """返回空目录并确认调用方使用固定上限。"""
        assert limit == 201
        return ()


def test_injected_memory_service_enables_tools_when_environment_default_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """环境未开启总开关时，显式注入仍应生成记忆工具快照。"""

    async def scenario() -> None:
        monkeypatch.delenv("ATHENA_MEMORY_ENABLED", raising=False)
        memory = cast(
            MemoryService,
            SimpleNamespace(lifecycle_repository=None),
        )
        bundle = build_query_services(
            cast(Any, object()),
            projects=cast(ProjectService, object()),
            memory_service=memory,
        )

        assert bundle.memory is memory
        assert bundle.memory_enabled is True
        runtime = build_run_tool_runtime(
            auth=cast(Any, object()),
            sessions=cast(Any, object()),
            mcp_catalog=cast(Any, _McpCatalog()),
            mcp_executor=cast(Any, object()),
            query_services=bundle,
        )
        snapshot = await runtime.build_snapshot(
            "run-v2",
            AuthorizedRunContext(
                owner_id="owner_test",
                chat_session_id="session_test",
                project_id=None,
                auth_session_id="auth_test",
            ),
        )

        assert tuple(item.model_name for item in snapshot.tools) == (
            "request_user_input",
            "memory_search",
            "memory_remember",
            "memory_correct",
            "memory_forget",
        )

    asyncio.run(scenario())


def test_query_service_bootstrap_injects_control_repository_and_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """组合根必须注入共享生命周期仓储和可信控制来源。"""

    from bootstrap import tool as tool_bootstrap_module

    captured: dict[str, object] = {}
    class _Resource:
        async def start(self) -> None:
            return None

        async def aclose(self) -> None:
            return None

    class _Repository:
        pass

    class _SourceRepository:
        def __init__(self, _sessions: object) -> None:
            pass

    class _CapturingMemoryService:
        lifecycle_repository = None

        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["args"] = args
            captured.update(kwargs)

    monkeypatch.setenv("ATHENA_MEMORY_ENABLED", "true")
    monkeypatch.delenv("ATHENA_MEMORY_LIFECYCLE_ENABLED", raising=False)
    monkeypatch.setattr(
        tool_bootstrap_module,
        "LocalEmbedding",
        lambda *args: _Resource(),
    )
    monkeypatch.setattr(
        tool_bootstrap_module,
        "LocalReranker",
        lambda *args: _Resource(),
    )
    monkeypatch.setattr(
        tool_bootstrap_module,
        "LocalTokenCounter",
        lambda *args: _Resource(),
    )
    monkeypatch.setattr(tool_bootstrap_module, "MemoryRepository", lambda *args: _Repository())
    monkeypatch.setattr(
        tool_bootstrap_module,
        "MemoryControlSourceRepository",
        _SourceRepository,
    )
    monkeypatch.setattr(
        tool_bootstrap_module,
        "MemoryService",
        _CapturingMemoryService,
    )

    build_query_services(
        cast(Any, object()),
        projects=cast(ProjectService, object()),
    )

    assert captured["lifecycle"] is not None
    assert captured["control_source"] is not None


def test_live_profile_loader_prefers_process_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """真实 runtime loader 应让安全 source 后的显式进程环境覆盖旧文件。"""

    from integration.test_memory_decision_live import _live_config

    env_file = tmp_path / "stale.env"
    env_file.write_text(
        "\n".join(
            (
                "ATHENA_DATABASE_URL=stale-database",
                "ATHENA_MCP_SECRET_KEY=stale-key",
                "ATHENA_MCP_SECRET_KEY_VERSION=1",
                "ATHENA_LLM_LIVE_PROFILE_ID=stale-profile",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ATHENA_LLM_LIVE_ENV_FILE", str(env_file))
    monkeypatch.setenv("ATHENA_DATABASE_URL", "process-database")
    monkeypatch.setenv("ATHENA_MCP_SECRET_KEY", "process-key")
    monkeypatch.setenv("ATHENA_MCP_SECRET_KEY_VERSION", "9")
    monkeypatch.setenv("ATHENA_LLM_LIVE_PROFILE_ID", "process-profile")

    config = _live_config()

    assert config.database_url == "process-database"
    assert config.mcp_secret_key == "process-key"
    assert config.mcp_secret_key_version == 9
    assert config.profile_id == "process-profile"


def test_memory_worker_bootstrap_reads_only_current_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """组合根只读取轮询、批次、输出和归档配置，忽略废弃租约与并发项。"""

    from bootstrap import memory_worker as memory_bootstrap_module
    from bootstrap.memory_worker import MemoryWorkerBundle

    class _Database:
        """提供组合根所需会话工厂，不建立真实连接。"""

        session_factory = object()

    class _Profiles:
        """提供不参与构造的模型仓储替身。"""

    class _Provider:
        """提供不参与构造的文本模型替身。"""

    class _Repository:
        """提供不参与构造的记忆仓储替身。"""

    class _Embedding:
        """接收固定向量模型参数。"""

        def __init__(self, model: str, revision: str, device: str) -> None:
            self.model = model
            self.revision = revision
            self.device = device

    class _Counter:
        """接收固定 tokenizer 模型参数。"""

        def __init__(self, model: str, revision: str) -> None:
            self.model = model
            self.revision = revision

    class _Extraction:
        """接收组合根创建的依赖与输出上限。"""

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.args = args
            self.kwargs = kwargs

    class _Cipher:
        """提供固定密钥处理器替身。"""

        @staticmethod
        def from_environment() -> object:
            return object()

    monkeypatch.setenv("ATHENA_MEMORY_ENABLED", "true")
    monkeypatch.setenv("ATHENA_MEMORY_LIFECYCLE_ENABLED", "true")
    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+psycopg://test/db")
    monkeypatch.setenv("ATHENA_MEMORY_WORKER_POLL_SECONDS", "2.5")
    monkeypatch.setenv("ATHENA_MEMORY_EXTRACT_BATCH_RUNS", "12")
    monkeypatch.setenv("ATHENA_MEMORY_EXTRACT_BATCH_TOKENS", "15000")
    monkeypatch.setenv("ATHENA_MEMORY_EXTRACTION_OUTPUT_TOKENS", "3000")
    monkeypatch.delenv("ATHENA_MEMORY_DECISION_OUTPUT_TOKENS", raising=False)
    monkeypatch.setenv("ATHENA_MEMORY_DECISION_CONTEXT_TOKENS", "9000")
    monkeypatch.setenv("ATHENA_MEMORY_FACT_ARCHIVE_DAYS", "90")
    monkeypatch.setenv("ATHENA_MEMORY_OTHER_ARCHIVE_DAYS", "180")
    monkeypatch.setenv("ATHENA_MEMORY_WORKER_LEASE_SECONDS", "9")
    monkeypatch.setenv("ATHENA_MEMORY_WORKER_HEARTBEAT_SECONDS", "2")
    monkeypatch.setenv("ATHENA_MEMORY_WORKER_CONCURRENCY", "7")
    monkeypatch.setenv("ATHENA_MEMORY_SCAN_PAGE_LIMIT", "3")

    monkeypatch.setattr(memory_bootstrap_module, "build_database", lambda url: _Database())
    monkeypatch.setattr(
        memory_bootstrap_module,
        "McpSecretCipher",
        _Cipher,
    )
    monkeypatch.setattr(
        memory_bootstrap_module,
        "SqlAlchemyModelProfileRepository",
        lambda *args: _Profiles(),
    )
    monkeypatch.setattr(
        memory_bootstrap_module,
        "OpenAIChatCompletionsAdapter",
        _Provider,
    )
    monkeypatch.setattr(
        memory_bootstrap_module,
        "MemoryRepository",
        lambda *args: _Repository(),
    )
    monkeypatch.setattr(memory_bootstrap_module, "LocalEmbedding", _Embedding)
    monkeypatch.setattr(memory_bootstrap_module, "LocalTokenCounter", _Counter)
    monkeypatch.setattr(
        memory_bootstrap_module,
        "embedding_profile_id",
        lambda model, revision: "embedding-profile",
    )
    monkeypatch.setattr(
        memory_bootstrap_module,
        "MemoryExtractionService",
        _Extraction,
    )

    bundle = MemoryWorkerBundle()
    memory_bootstrap_module.build_memory_worker_dependencies(bundle)

    assert bundle.worker is not None
    extraction = cast(Any, bundle.worker)._extraction
    assert isinstance(extraction, _Extraction)
    limits = cast(ExtractionLimits, extraction.kwargs["limits"])
    assert limits.decision_max_output_tokens == 4_000
    assert ExtractionLimits().decision_max_output_tokens == 4_000
    env_example = (Path(__file__).resolve().parents[2] / ".env.example").read_text(
        encoding="utf-8"
    )
    assert "ATHENA_MEMORY_DECISION_OUTPUT_TOKENS=4000" in env_example
    compose_file = Path(__file__).resolve().parents[2] / "compose.yml"
    compose_lines = compose_file.read_text(encoding="utf-8").splitlines()
    assert any(
        line.strip()
        == (
            "ATHENA_MEMORY_DECISION_OUTPUT_TOKENS: "
            "${ATHENA_MEMORY_DECISION_OUTPUT_TOKENS:-4000}"
        )
        for line in compose_lines
    )

    config = bundle.worker._config
    assert config.idle_sleep_seconds == 2.5
    assert config.batch_run_limit == 12
    assert config.batch_token_limit == 15_000
    assert config.fact_max_age_days == 90
    assert config.other_max_age_days == 180
    assert not hasattr(config, "lease_seconds")
    assert not hasattr(config, "heartbeat_seconds")
    assert not hasattr(config, "concurrency")
    assert not hasattr(config, "scan_page_limit")


def test_memory_worker_entry_recovers_before_single_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """入口先恢复 running 和扫描边界，再进入唯一的消费循环并关闭资源。"""

    import memory_worker as memory_entry_module

    from app.memory.worker import MemoryWorkerConfig
    from bootstrap.memory_worker import MemoryWorkerBundle

    events: list[str] = []

    class _Database:
        """记录数据库释放。"""

        async def dispose(self) -> None:
            events.append("database.close")

    class _Provider:
        """记录文本模型客户端释放。"""

        async def aclose(self) -> None:
            events.append("provider.close")

    class _Embedding:
        """记录向量模型启动与释放。"""

        async def start(self) -> None:
            events.append("embedding.start")

        async def aclose(self) -> None:
            events.append("embedding.close")

    class _Counter:
        """记录 tokenizer 计数资源启动与关闭。"""

        async def start(self) -> None:
            events.append("counter.start")

        def close(self) -> None:
            events.append("counter.close")

    class _Worker:
        """记录启动顺序。"""

        async def validate_runtime(self) -> None:
            events.append("worker.validate")

        async def recover_running_jobs(self) -> int:
            events.append("worker.recover")
            return 1

        async def ensure_scan_job(self, enabled_boundary_at: object) -> None:
            del enabled_boundary_at
            events.append("worker.scan")

    def _build(bundle: MemoryWorkerBundle) -> None:
        bundle.database = cast(Any, _Database())
        bundle.provider = cast(Any, _Provider())
        bundle.embedding = cast(Any, _Embedding())
        bundle.counter = cast(Any, _Counter())
        bundle.worker = cast(Any, _Worker())
        bundle.config = MemoryWorkerConfig(idle_sleep_seconds=2.5)

    async def _consume(worker: object, *, idle_sleep_seconds: float) -> None:
        del worker
        assert idle_sleep_seconds == 2.5
        events.append("consume")

    monkeypatch.setattr(
        memory_entry_module,
        "build_memory_worker_dependencies",
        _build,
    )
    monkeypatch.setattr(memory_entry_module, "_consume", _consume)

    asyncio.run(memory_entry_module.run_worker())

    assert events == [
        "embedding.start",
        "counter.start",
        "worker.validate",
        "worker.recover",
        "worker.scan",
        "consume",
        "provider.close",
        "embedding.close",
        "counter.close",
        "database.close",
    ]
