"""Unit tests for MCP transport layer — StdioTransport and TransportFactory."""

import pytest

from athena.mcp_client.transports import StdioTransport, TransportFactory


class TestStdioTransport:
    """Test StdioTransport command parsing and env handling."""

    def test_legacy_command_string_split(self):
        """Legacy format: command as space-separated string."""
        t = StdioTransport("python -m athena.tools.server")
        assert t.command == ["python", "-m", "athena.tools.server"]
        assert t._extra_env == {}

    def test_command_as_list_preserved(self):
        """Command already a list, no args."""
        t = StdioTransport(["python", "-m", "foo"])
        assert t.command == ["python", "-m", "foo"]

    def test_env_default_empty(self):
        """No env passed → _extra_env is empty dict."""
        t = StdioTransport("echo hello")
        assert t._extra_env == {}

    def test_env_passed_through(self):
        """env dict is stored in _extra_env."""
        t = StdioTransport("npx", env={"API_KEY": "secret"})
        assert t._extra_env == {"API_KEY": "secret"}

    def test_env_none_is_empty(self):
        """Explicit None env → empty dict."""
        t = StdioTransport("cmd", env=None)
        assert t._extra_env == {}

    def test_command_with_spaces_in_path(self):
        """Command string with spaces is split on whitespace."""
        t = StdioTransport("python -m athena.tools.weather_server")
        assert t.command == ["python", "-m", "athena.tools.weather_server"]


class TestTransportFactory:
    """Test TransportFactory.create() for stdio transport."""

    def test_legacy_command_string(self):
        """Legacy: command as space-separated string."""
        t = TransportFactory.create("stdio", {"command": "python -m foo"})
        assert isinstance(t, StdioTransport)
        assert t.command == ["python", "-m", "foo"]
        assert t._extra_env == {}

    def test_claude_desktop_command_with_args(self):
        """Claude Desktop format: separate command + args list."""
        t = TransportFactory.create("stdio", {
            "command": "npx",
            "args": ["-y", "@amap/amap-maps-mcp-server"],
        })
        assert isinstance(t, StdioTransport)
        assert t.command == ["npx", "-y", "@amap/amap-maps-mcp-server"]

    def test_command_with_args_and_env(self):
        """Claude Desktop format: command + args + env."""
        t = TransportFactory.create("stdio", {
            "command": "npx",
            "args": ["-y", "@scope/pkg"],
            "env": {"API_KEY": "secret", "DEBUG": "1"},
        })
        assert isinstance(t, StdioTransport)
        assert t.command == ["npx", "-y", "@scope/pkg"]
        assert t._extra_env == {"API_KEY": "secret", "DEBUG": "1"}

    def test_command_as_list_no_args(self):
        """command already a list, no args key."""
        t = TransportFactory.create("stdio", {
            "command": ["python", "-m", "foo"],
        })
        assert isinstance(t, StdioTransport)
        assert t.command == ["python", "-m", "foo"]

    def test_empty_connection_config(self):
        """Edge case: no command at all."""
        t = TransportFactory.create("stdio", {})
        assert isinstance(t, StdioTransport)
        assert t.command == []

    def test_env_only_no_command(self):
        """env without command — command list is empty."""
        t = TransportFactory.create("stdio", {"env": {"KEY": "val"}})
        assert isinstance(t, StdioTransport)
        assert t.command == []
        assert t._extra_env == {"KEY": "val"}

    def test_command_with_single_arg(self):
        """Single arg in args list."""
        t = TransportFactory.create("stdio", {
            "command": "uvx",
            "args": ["mcp-server-git"],
        })
        assert t.command == ["uvx", "mcp-server-git"]
