"""Bootstrap 组合根的环境解析与资源构造测试。"""

from __future__ import annotations

from typing import Any

import pytest

import bootstrap.database as bootstrap_database
import bootstrap.knowledge as bootstrap_knowledge
import bootstrap.redis as bootstrap_redis
import bootstrap.tool as bootstrap_tool
from infrastructure.database import (
    DEFAULT_DATABASE_MAX_OVERFLOW,
    DEFAULT_DATABASE_POOL_SIZE,
)
from infrastructure.embedding.profile import embedding_profile_id


def test_build_database_passes_pool_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, Any] = {}

    class FakeDatabase:
        def __init__(self, url: str, *, pool_size: int, max_overflow: int) -> None:
            recorded.update(url=url, pool_size=pool_size, max_overflow=max_overflow)

    monkeypatch.setattr(bootstrap_database, "Database", FakeDatabase)
    monkeypatch.setenv("ATHENA_DATABASE_POOL_SIZE", "7")
    monkeypatch.setenv("ATHENA_DATABASE_MAX_OVERFLOW", "3")

    bootstrap_database.build_database("postgresql+asyncpg://db.invalid/athena")

    assert recorded == {
        "url": "postgresql+asyncpg://db.invalid/athena",
        "pool_size": 7,
        "max_overflow": 3,
    }


def test_build_database_uses_default_pool_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, Any] = {}

    class FakeDatabase:
        def __init__(self, url: str, *, pool_size: int, max_overflow: int) -> None:
            recorded.update(url=url, pool_size=pool_size, max_overflow=max_overflow)

    monkeypatch.setattr(bootstrap_database, "Database", FakeDatabase)
    monkeypatch.delenv("ATHENA_DATABASE_POOL_SIZE", raising=False)
    monkeypatch.delenv("ATHENA_DATABASE_MAX_OVERFLOW", raising=False)

    bootstrap_database.build_database("postgresql+asyncpg://db.invalid/athena")

    assert recorded["pool_size"] == DEFAULT_DATABASE_POOL_SIZE
    assert recorded["max_overflow"] == DEFAULT_DATABASE_MAX_OVERFLOW


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("ATHENA_DATABASE_POOL_SIZE", "0"),
        ("ATHENA_DATABASE_POOL_SIZE", "not-a-number"),
        ("ATHENA_DATABASE_MAX_OVERFLOW", "-1"),
        ("ATHENA_DATABASE_MAX_OVERFLOW", "not-a-number"),
    ),
)
def test_build_database_rejects_invalid_pool_configuration(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    class FakeDatabase:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("invalid configuration must fail before construction")

    monkeypatch.setattr(bootstrap_database, "Database", FakeDatabase)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=f"{name}_invalid"):
        bootstrap_database.build_database("postgresql+asyncpg://db.invalid/athena")


def test_build_run_transport_returns_none_without_redis_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATHENA_REDIS_URL", raising=False)
    assert bootstrap_redis.build_run_transport() is None

    monkeypatch.setenv("ATHENA_REDIS_URL", "   ")
    assert bootstrap_redis.build_run_transport() is None


def test_build_run_transport_uses_binary_redis_client(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, Any] = {}
    client = object()

    def fake_from_url(url: str, **options: Any) -> object:
        recorded.update(url=url, **options)
        return client

    class FakeTransport:
        def __init__(self, redis: object) -> None:
            self.redis = redis

    monkeypatch.setenv("ATHENA_REDIS_URL", "redis://cache.invalid:6379/0")
    monkeypatch.setattr("bootstrap.redis.Redis.from_url", fake_from_url)
    monkeypatch.setattr(bootstrap_redis, "RedisRunTransport", FakeTransport)

    transport = bootstrap_redis.build_run_transport()

    assert recorded == {
        "url": "redis://cache.invalid:6379/0",
        "decode_responses": False,
        "socket_connect_timeout": 1,
    }
    assert isinstance(transport, FakeTransport)
    assert transport.redis is client


def test_build_knowledge_models_uses_shared_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[tuple[str, tuple[Any, ...]]] = []

    class FakeEmbedding:
        def __init__(self, *args: Any) -> None:
            created.append(("embedding", args))

    class FakeCounter:
        def __init__(self, *args: Any) -> None:
            created.append(("counter", args))

    class FakeTokenizer:
        def __init__(self) -> None:
            created.append(("tokenizer", ()))

    monkeypatch.setattr(bootstrap_knowledge, "LocalEmbedding", FakeEmbedding)
    monkeypatch.setattr(bootstrap_knowledge, "LocalKnowledgeTokenCounter", FakeCounter)
    monkeypatch.setattr(bootstrap_knowledge, "JiebaChineseTokenizer", FakeTokenizer)
    monkeypatch.setenv("ATHENA_KNOWLEDGE_EMBEDDING_MODEL", "custom/model")
    monkeypatch.setenv("ATHENA_KNOWLEDGE_EMBEDDING_REVISION", "revision-1")
    monkeypatch.setenv("ATHENA_KNOWLEDGE_EMBEDDING_DEVICE", "mps")

    resources = bootstrap_knowledge.build_knowledge_models()

    assert created == [
        ("embedding", ("custom/model", "revision-1", "mps")),
        ("counter", ("custom/model", "revision-1")),
        ("tokenizer", ()),
    ]
    assert isinstance(resources.embedding, FakeEmbedding)
    assert isinstance(resources.counter, FakeCounter)
    assert isinstance(resources.tokenizer, FakeTokenizer)
    assert resources.profile_id == embedding_profile_id("custom/model", "revision-1")


def test_build_mcp_executor_rejects_invalid_private_host_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATHENA_MCP_ALLOWED_PRIVATE_HOSTS", "bad host")
    monkeypatch.delenv("ATHENA_MCP_STDIO_BASE_ENV_NAMES", raising=False)

    with pytest.raises(ValueError, match="ATHENA_MCP_ALLOWED_PRIVATE_HOSTS_invalid"):
        bootstrap_tool.build_mcp_executor()


def test_build_mcp_executor_accepts_valid_allowlist_and_environment_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATHENA_MCP_ALLOWED_PRIVATE_HOSTS", "mcp.internal,10.0.0.8")
    monkeypatch.setenv("ATHENA_MCP_STDIO_BASE_ENV_NAMES", "GOOD_NAME")
    monkeypatch.setenv("GOOD_NAME", "value")

    executor = bootstrap_tool.build_mcp_executor(cleanup_timeout_seconds=3.0)

    assert executor is not None
