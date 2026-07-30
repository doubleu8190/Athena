"""Shared test fixtures — in-memory SQLite, test Config."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def clean_config_singleton():
    """Reset the config singleton between tests."""
    from athena.config import set_config
    yield
    set_config(None)  # type: ignore[arg-type]


@pytest.fixture
def temp_db_path():
    """Create a temporary SQLite database path."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    yield path
    try:
        os.unlink(path)
        os.unlink(path + "-wal")
        os.unlink(path + "-shm")
    except FileNotFoundError:
        pass


@pytest.fixture
def test_config():
    """Create a minimal test configuration."""
    from athena.config import Config
    return Config.load()


@pytest_asyncio.fixture
async def db_session(temp_db_path):
    """Create an async SQLite session for testing."""
    from athena.models.base import get_engine, get_session_maker

    engine = get_engine(temp_db_path)
    maker = get_session_maker(temp_db_path)

    # Create all tables
    from athena.models.base import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with maker() as session:
        yield session

    await engine.dispose()
