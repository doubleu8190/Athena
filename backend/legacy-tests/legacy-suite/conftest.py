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


@pytest.fixture
def postgres_url() -> str:
    """Return the explicit PostgreSQL URL used by integration tests."""
    url = os.environ.get("ATHENA_TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("requires ATHENA_TEST_DATABASE_URL pointing to PostgreSQL + pgvector")
    os.environ["DATABASE_URL"] = url
    return url


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
