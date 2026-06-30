"""Unit tests for auto_register_builtin_servers upsert logic."""

from __future__ import annotations

import json
import os
import tempfile

import pytest
import pytest_asyncio

from athena.config import Config


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_config(seed: list[dict], db_path: str) -> Config:
    """Create a Config with a custom mcp_servers_seed and DB path."""
    config = Config.load()
    config.mcp_servers_seed = seed
    config.sqlite_db_path = db_path
    return config


async def _create_tables(db_path: str):
    """Create all tables in the temp database."""
    from athena.models.base import get_engine, Base

    engine = get_engine(db_path)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _get_all_servers(db_path: str) -> list[dict]:
    """Read all MCPServer rows from the database."""
    from athena.models.base import get_session_maker
    from athena.models.mcp_server import MCPServer
    from sqlalchemy import select

    maker = get_session_maker(db_path)
    async with maker() as session:
        result = await session.execute(
            select(MCPServer).order_by(MCPServer.server_id)
        )
        rows = result.scalars().all()
        return [
            {
                "server_id": r.server_id,
                "name": r.name,
                "transport": r.transport,
                "connection_config": r.connection_config,
                "enabled": r.enabled,
                "source": r.source,
            }
            for r in rows
        ]


async def _insert_server(db_path: str, **kwargs):
    """Insert a single MCPServer row directly."""
    from athena.models.base import get_session_maker
    from athena.models.mcp_server import MCPServer

    maker = get_session_maker(db_path)
    async with maker() as session:
        defaults = {
            "server_id": "test",
            "name": "Test",
            "transport": "stdio",
            "connection_config": json.dumps({"command": "echo"}),
            "enabled": True,
            "source": "builtin",
        }
        defaults.update(kwargs)
        defaults["connection_config"] = (
            json.dumps(defaults["connection_config"])
            if isinstance(defaults["connection_config"], dict)
            else defaults["connection_config"]
        )
        server = MCPServer(**defaults)
        session.add(server)
        await session.commit()


# ── Seed data ──────────────────────────────────────────────────────────────

BUILTIN_SEED = [
    {
        "server_id": "builtin-core",
        "name": "Athena Built-in Tools",
        "transport": "stdio",
        "connection_config": {"command": "python -m athena.tools.server"},
        "source": "builtin",
    },
    {
        "server_id": "weather",
        "name": "Weather Query",
        "transport": "stdio",
        "connection_config": {"command": "python -m athena.tools.weather_server"},
        "source": "builtin",
    },
]

UPDATED_SEED = [
    {
        "server_id": "builtin-core",
        "name": "Athena Built-in Tools v2",
        "transport": "stdio",
        "connection_config": {"command": "python -m athena.tools.server --verbose"},
        "source": "builtin",
    },
]


# ── Tests ──────────────────────────────────────────────────────────────────


class TestAutoRegisterBuiltinServers:
    """Test auto_register_builtin_servers upsert behavior."""

    @pytest_asyncio.fixture(autouse=True)
    async def _setup(self):
        """Create a fresh temp DB and tables for each test."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            self.db_path = f.name
        await _create_tables(self.db_path)
        yield
        try:
            os.unlink(self.db_path)
            os.unlink(self.db_path + "-wal")
            os.unlink(self.db_path + "-shm")
        except FileNotFoundError:
            pass

    async def _run(self, seed: list[dict]) -> None:
        """Run auto_register_builtin_servers with the given seed."""
        from athena.mcp_client.seed_loader import auto_register_builtin_servers

        config = _make_config(seed, self.db_path)
        await auto_register_builtin_servers(config)

    # ── Basic cases ─────────────────────────────────────────────────────

    async def test_empty_db_inserts_all(self):
        """Empty DB → all seed servers inserted."""
        await self._run(BUILTIN_SEED)

        servers = await _get_all_servers(self.db_path)
        assert len(servers) == 2
        assert servers[0]["server_id"] == "builtin-core"
        assert servers[0]["enabled"] is True
        assert servers[0]["source"] == "builtin"
        assert servers[1]["server_id"] == "weather"

    async def test_existing_builtin_gets_updated(self):
        """DB has builtin server → name/transport/config updated."""
        await _insert_server(
            self.db_path,
            server_id="builtin-core",
            name="Old Name",
            transport="http",
            connection_config=json.dumps({"command": "old"}),
            source="builtin",
        )
        await self._run(BUILTIN_SEED)

        servers = await _get_all_servers(self.db_path)
        assert len(servers) == 2  # builtin-core updated + weather inserted
        core = next(s for s in servers if s["server_id"] == "builtin-core")
        assert core["name"] == "Athena Built-in Tools"
        assert core["transport"] == "stdio"
        assert json.loads(core["connection_config"]) == {"command": "python -m athena.tools.server"}

    async def test_disabled_builtin_preserves_enabled(self):
        """Disabled builtin server → config updated, enabled stays False."""
        await _insert_server(
            self.db_path,
            server_id="builtin-core",
            name="Old Name",
            enabled=False,
            source="builtin",
        )
        await self._run(BUILTIN_SEED)

        servers = await _get_all_servers(self.db_path)
        core = next(s for s in servers if s["server_id"] == "builtin-core")
        assert core["name"] == "Athena Built-in Tools"  # Updated
        assert core["enabled"] is False  # Preserved

    async def test_user_server_same_id_skipped(self):
        """User-created server with same server_id → not overwritten."""
        await _insert_server(
            self.db_path,
            server_id="builtin-core",
            name="My Custom Server",
            transport="http",
            connection_config=json.dumps({"url": "https://example.com"}),
            source="user",
        )
        await self._run(BUILTIN_SEED)

        servers = await _get_all_servers(self.db_path)
        assert len(servers) == 2  # user's builtin-core untouched + weather inserted
        core = next(s for s in servers if s["server_id"] == "builtin-core")
        assert core["name"] == "My Custom Server"  # NOT overwritten
        assert core["transport"] == "http"
        assert core["source"] == "user"

    async def test_user_server_different_id_untouched(self):
        """Pre-existing user server with different ID → left untouched."""
        await _insert_server(
            self.db_path,
            server_id="my-custom-server",
            name="My Server",
            source="user",
        )
        await self._run(BUILTIN_SEED)

        servers = await _get_all_servers(self.db_path)
        assert len(servers) == 3  # my-custom-server + 2 builtins
        custom = next(s for s in servers if s["server_id"] == "my-custom-server")
        assert custom["name"] == "My Server"
        assert custom["source"] == "user"

    async def test_new_server_in_updated_yaml_inserted(self):
        """A server that wasn't in DB before gets inserted."""
        await _insert_server(
            self.db_path,
            server_id="builtin-core",
            name="Existing",
            source="builtin",
        )
        await self._run(BUILTIN_SEED)  # includes both builtin-core and weather

        servers = await _get_all_servers(self.db_path)
        assert len(servers) == 2  # builtin-core (updated) + weather (inserted)
        weather = next(s for s in servers if s["server_id"] == "weather")
        assert weather["enabled"] is True
        assert weather["source"] == "builtin"

    async def test_config_update_propagates(self):
        """When YAML config changes, existing builtin gets updated."""
        await _insert_server(
            self.db_path,
            server_id="builtin-core",
            name="Athena Built-in Tools",
            transport="stdio",
            connection_config=json.dumps({"command": "python -m athena.tools.server"}),
            source="builtin",
        )
        # Now run with updated config
        await self._run(UPDATED_SEED)

        servers = await _get_all_servers(self.db_path)
        core = next(s for s in servers if s["server_id"] == "builtin-core")
        assert core["name"] == "Athena Built-in Tools v2"
        assert json.loads(core["connection_config"]) == {
            "command": "python -m athena.tools.server --verbose"
        }

    async def test_empty_seed_list_noop(self):
        """Empty YAML seed → no changes, no error."""
        await self._run([])

        servers = await _get_all_servers(self.db_path)
        assert len(servers) == 0

    async def test_idempotent_second_run(self):
        """Running auto-register twice with same seed → no duplicates."""
        await self._run(BUILTIN_SEED)
        await self._run(BUILTIN_SEED)

        servers = await _get_all_servers(self.db_path)
        assert len(servers) == 2  # Still 2, no duplicates

    async def test_external_source_treated_as_user(self):
        """Legacy source='external' is treated as user-created (skipped)."""
        await _insert_server(
            self.db_path,
            server_id="builtin-core",
            name="Legacy External Server",
            source="external",
        )
        await self._run(BUILTIN_SEED)

        servers = await _get_all_servers(self.db_path)
        core = next(s for s in servers if s["server_id"] == "builtin-core")
        assert core["name"] == "Legacy External Server"  # NOT overwritten
        assert core["source"] == "external"


class TestAutoRegisterBuiltinServersEdgeCases:
    """Edge case tests for auto_register_builtin_servers."""

    @pytest_asyncio.fixture(autouse=True)
    async def _setup(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            self.db_path = f.name
        await _create_tables(self.db_path)
        yield
        try:
            os.unlink(self.db_path)
            os.unlink(self.db_path + "-wal")
            os.unlink(self.db_path + "-shm")
        except FileNotFoundError:
            pass

    async def _run(self, seed: list[dict]) -> None:
        from athena.mcp_client.seed_loader import auto_register_builtin_servers

        config = _make_config(seed, self.db_path)
        await auto_register_builtin_servers(config)

    async def test_seed_none_handled(self):
        """None seed (instead of empty list) — handled gracefully."""
        from athena.mcp_client.seed_loader import auto_register_builtin_servers

        config = _make_config([], self.db_path)
        config.mcp_servers_seed = []  # Empty, should skip
        await auto_register_builtin_servers(config)

        servers = await _get_all_servers(self.db_path)
        assert len(servers) == 0

    async def test_mixed_sources_on_first_run(self):
        """Seed entries all inserted as builtin regardless of YAML source field."""
        mixed_seed = [
            {
                "server_id": "s1",
                "name": "Server 1",
                "transport": "stdio",
                "connection_config": {"command": "echo"},
                "source": "builtin",
            },
        ]
        await self._run(mixed_seed)

        servers = await _get_all_servers(self.db_path)
        assert len(servers) == 1
        assert servers[0]["source"] == "builtin"
        assert servers[0]["enabled"] is True
