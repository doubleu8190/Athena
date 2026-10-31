"""验证 Worker v1 的健康监视器和派发器进入应用资源生命周期。"""

from __future__ import annotations

import asyncio

from bootstrap.postgres import build_postgres_dependencies
from configuration import LLMProviderConfig, Settings
from workers import WorkerSupervisor


def test_enabled_worker_protocol_starts_monitor_and_dispatcher(monkeypatch) -> None:
    """协议与后台 Worker 启用时，supervisor 会启动并停止监控器和派发器。"""
    events: list[str] = []

    class Monitor:
        async def start(self) -> None:
            events.append("start")

        async def stop(self) -> None:
            events.append("stop")

    class Dispatcher:
        async def start(self) -> None:
            events.append("dispatcher-start")

        async def stop(self) -> None:
            events.append("dispatcher-stop")

    class BackgroundWorker:
        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    monkeypatch.setattr("bootstrap.postgres.WorkerHealthMonitor", lambda *args, **kwargs: Monitor())
    monkeypatch.setattr("bootstrap.postgres.WorkerTaskDispatcher", lambda *args, **kwargs: Dispatcher())
    for worker_name in (
        "RunConsumer",
        "KnowledgeDocumentWorker",
        "MemoryWorker",
        "OrchestrationOutboxDispatcher",
        "ApprovalExpiryWorker",
    ):
        monkeypatch.setattr(
            f"bootstrap.postgres.{worker_name}",
            lambda *args, **kwargs: BackgroundWorker(),
        )
    settings = Settings(
        database_backend="postgres",
        workers_enabled=True,
        llm_providers=[LLMProviderConfig(provider="openai", api_key="test-key")],
        embedding_provider="echo",
        embedding_dimension=2,
        worker_protocol_service_tokens={"worker-a": "worker-token"},
        worker_orchestrator_token="orchestrator-token",
        worker_allowed_origins=("https://worker.example",),
        worker_execution_grant_secret="g" * 32,
    )
    graph = build_postgres_dependencies(settings)
    supervisors = [
        resource for resource in graph.resources if isinstance(resource, WorkerSupervisor)
    ]

    async def exercise_supervisors() -> None:
        for supervisor in supervisors:
            await supervisor.start()
        for supervisor in reversed(supervisors):
            await supervisor.stop()

    asyncio.run(exercise_supervisors())

    assert events == ["start", "dispatcher-start", "dispatcher-stop", "stop"]


def test_disabled_worker_protocol_does_not_construct_dispatcher(monkeypatch) -> None:
    """协议关闭时不会组装远程任务派发器。"""
    constructions: list[str] = []

    def create_dispatcher(*args, **kwargs):
        constructions.append("dispatcher")
        raise AssertionError("disabled_worker_protocol_constructed_dispatcher")

    monkeypatch.setattr("bootstrap.postgres.WorkerTaskDispatcher", create_dispatcher)
    settings = Settings(
        database_backend="postgres",
        llm_providers=[LLMProviderConfig(provider="openai", api_key="test-key")],
        embedding_provider="echo",
        embedding_dimension=2,
    )

    graph = build_postgres_dependencies(settings)

    assert constructions == []
    assert all(
        not isinstance(resource, WorkerSupervisor) for resource in graph.resources
    )
