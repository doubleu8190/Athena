"""测试共享 fixture."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

# 确保使用临时数据目录，避免污染工作区
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ["DEBUG"] = "false"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "postgres: integration test requiring a PostgreSQL database with pgvector",
    )


def pytest_collection_modifyitems(config, items):
    """Keep PostgreSQL integration tests explicit when the service is unavailable.

    The production persistence layer is PostgreSQL-only. A temporary ``.db`` path
    is not a valid substitute, so these tests require an explicit
    ``ATHENA_TEST_DATABASE_URL`` and are skipped otherwise.
    """
    import os

    database_url = os.environ.get("ATHENA_TEST_DATABASE_URL", "").strip()
    if database_url:
        os.environ["DATABASE_URL"] = database_url
        return
    skip = pytest.mark.skip(
        reason="requires ATHENA_TEST_DATABASE_URL pointing to PostgreSQL + pgvector"
    )
    for item in items:
        import inspect

        try:
            text = inspect.getsource(item.obj)
        except (OSError, TypeError):
            text = ""
        if (
            "Database(" in text
            or "initialize_postgres_engine(" in text
            or "agent_store" in item.fixturenames
            or "database" in item.fixturenames
        ):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
