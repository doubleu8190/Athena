"""测试共享 fixture."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

# 确保使用临时数据目录，避免污染工作区
os.environ.setdefault("SQLITE_DB_PATH", "/tmp/athena_test.db")
os.environ.setdefault("CHROMADB_PATH", "/tmp/athena_test_chromadb")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ["DEBUG"] = "false"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
