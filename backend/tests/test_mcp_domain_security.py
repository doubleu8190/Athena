"""MCP 配置领域校验和秘密封装测试。"""

from __future__ import annotations

import base64
from uuid import uuid4

import pytest

from domain.mcp import (
    McpSecretError,
    McpSecretPurpose,
    McpServerWrite,
    StdioConfig,
    StreamableHttpConfig,
    parse_server_config,
)
from domain.tool import MAX_TOOL_DESCRIPTION_LENGTH, ToolDefinition
from infrastructure.security.mcp_secrets import McpSecretCipher


def test_server_config_requires_exact_transport_fields() -> None:
    http = parse_server_config(
        {"transport": "streamable_http", "url": "https://mcp.example.com/rpc"}
    )
    stdio = parse_server_config(
        {"transport": "stdio", "command": "/usr/bin/mcp", "args": ["--safe"], "cwd": "/srv"}
    )
    assert isinstance(http, StreamableHttpConfig)
    assert isinstance(stdio, StdioConfig)
    with pytest.raises(ValueError, match="mcp_config_invalid"):
        parse_server_config(
            {
                "transport": "streamable_http",
                "url": "https://mcp.example.com/rpc",
                "command": "/usr/bin/mcp",
            }
        )
    with pytest.raises(ValueError, match="mcp_config_invalid"):
        parse_server_config({"transport": "stdio", "command": "mcp", "extra": "secret"})
    with pytest.raises(ValueError, match="mcp_config_invalid"):
        parse_server_config({"transport": "stdio", "command": "/usr/bin/mcp", "cwd": "relative"})
    for url in (
        "http://mcp.example.com/rpc",
        "https://user:password@mcp.example.com/rpc",
        "https://mcp.example.com/rpc?token=secret",
        "https://mcp.example.com/rpc#fragment",
    ):
        with pytest.raises(ValueError, match="mcp_config_invalid") as error:
            StreamableHttpConfig("streamable_http", url)
        assert "secret" not in str(error.value)


def test_server_write_limits_secret_bytes_and_keeps_values_out_of_errors() -> None:
    config = StdioConfig("stdio", "/usr/bin/mcp")
    write = McpServerWrite("mcp_1", "Server", config, {"TOKEN": "must-not-be-repr"})
    assert "must-not-be-repr" not in repr(write)
    with pytest.raises(ValueError, match="mcp_secret_invalid"):
        McpServerWrite("mcp_1", "Server", config, {"TOKEN": "x" * (16 * 1024)})
    with pytest.raises(ValueError, match="mcp_secret_name_invalid"):
        McpServerWrite("mcp_1", "Server", config, {"BAD-NAME": "not returned"})


@pytest.mark.parametrize(
    "header_name",
    [
        "Host",
        "Content-Length",
        "Connection",
        "Transfer-Encoding",
        "Accept",
        "Content-Type",
        "MCP-Protocol-Version",
        "Mcp-Method",
        "Mcp-Name",
        "Mcp-Session-Id",
        "Last-Event-ID",
        "lAsT-eVeNt-iD",
        "Mcp-Param-foo",
    ],
)
def test_http_secret_rejects_protocol_headers(header_name: str) -> None:
    with pytest.raises(ValueError, match="mcp_secret_name_invalid"):
        McpServerWrite(
            "mcp_1",
            "Server",
            StreamableHttpConfig("streamable_http", "https://mcp.example.com/rpc"),
            {header_name: "private-value"},
        )


def test_http_secret_header_names_are_case_insensitive_and_custom_names_are_allowed() -> None:
    config = StreamableHttpConfig("streamable_http", "https://mcp.example.com/rpc")
    with pytest.raises(ValueError, match="mcp_secret_name_invalid"):
        McpServerWrite("mcp_1", "Server", config, {"X-Test": "a", "x-test": "b"})
    assert McpServerWrite("mcp_1", "Server", config, {"X-Custom-Auth": "value"})


def test_tool_definition_enforces_description_and_schema_constraints() -> None:
    tool_id = f"tool_{uuid4().hex}"
    model_name = f"mcp__{tool_id.removeprefix('tool_')}"
    with pytest.raises(ValueError, match="mcp_tool_definition_invalid"):
        ToolDefinition(
            tool_id,
            "mcp_1",
            "remote",
            model_name,
            "x" * (MAX_TOOL_DESCRIPTION_LENGTH + 1),
            {},
        )
    with pytest.raises(ValueError, match="mcp_tool_definition_invalid"):
        ToolDefinition(tool_id, "mcp_1", "remote", model_name, "", {"type": "string"})
    with pytest.raises(ValueError, match="mcp_tool_definition_invalid"):
        ToolDefinition(tool_id, "mcp_1", "remote", model_name, "", {"invalid": object()})
    with pytest.raises(ValueError, match="mcp_tool_definition_invalid"):
        ToolDefinition(tool_id, "mcp_1", "remote", "mcp__different", "", {})
    with pytest.raises(ValueError, match="mcp_tool_definition_invalid"):
        ToolDefinition("tool_not-a-uuid", "mcp_1", "remote", "mcp__bad", "", {})


def test_tool_definition_cleans_description_and_rejects_unicode_controls_in_remote_name() -> None:
    tool_id = f"tool_{uuid4().hex}"
    model_name = f"mcp__{tool_id.removeprefix('tool_')}"
    tool = ToolDefinition(tool_id, "mcp_1", "safe-name", model_name, "a\x00b\x7fc", {})
    assert tool.description == "abc"
    for remote_name in ("bad\x7fname", "bad\x85name"):
        with pytest.raises(ValueError, match="mcp_tool_definition_invalid"):
            ToolDefinition(tool_id, "mcp_1", remote_name, model_name, "", {})


def test_mcp_secret_cipher_round_trip_nonce_and_aad_binding() -> None:
    cipher = McpSecretCipher(bytes(range(32)), key_version=3)
    values = {"Authorization": "Bearer very-secret-value"}
    sealed = cipher.seal("mcp_server_1", McpSecretPurpose.HTTP_HEADERS, values)
    another = cipher.seal("mcp_server_1", McpSecretPurpose.HTTP_HEADERS, values)

    assert sealed.nonce != another.nonce
    assert b"very-secret-value" not in sealed.ciphertext
    assert b"Authorization" not in sealed.ciphertext
    assert (
        cipher.open(
            "mcp_server_1",
            McpSecretPurpose.HTTP_HEADERS,
            sealed.ciphertext,
            sealed.nonce,
            sealed.key_version,
        )
        == values
    )
    with pytest.raises(McpSecretError, match="mcp_secret_decrypt_failed"):
        cipher.open(
            "different-server",
            McpSecretPurpose.HTTP_HEADERS,
            sealed.ciphertext,
            sealed.nonce,
            sealed.key_version,
        )
    damaged = bytes([sealed.ciphertext[0] ^ 1]) + sealed.ciphertext[1:]
    with pytest.raises(McpSecretError, match="mcp_secret_decrypt_failed"):
        cipher.open(
            "mcp_server_1",
            McpSecretPurpose.HTTP_HEADERS,
            damaged,
            sealed.nonce,
            sealed.key_version,
        )
    with pytest.raises(McpSecretError, match="mcp_secret_decrypt_failed"):
        cipher.open(
            "mcp_server_1",
            McpSecretPurpose.STDIO_ENVIRONMENT,
            sealed.ciphertext,
            sealed.nonce,
            sealed.key_version,
        )
    with pytest.raises(McpSecretError, match="mcp_secret_decrypt_failed"):
        cipher.open(
            "mcp_server_1",
            McpSecretPurpose.HTTP_HEADERS,
            sealed.ciphertext,
            sealed.nonce,
            key_version=2,
        )
    corrupted = sealed.ciphertext[:-1] + bytes([sealed.ciphertext[-1] ^ 1])
    with pytest.raises(McpSecretError, match="mcp_secret_decrypt_failed"):
        cipher.open(
            "mcp_server_1",
            McpSecretPurpose.HTTP_HEADERS,
            corrupted,
            sealed.nonce,
            sealed.key_version,
        )


def test_mcp_secret_cipher_environment_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATHENA_MCP_SECRET_KEY", raising=False)
    monkeypatch.delenv("ATHENA_MCP_SECRET_KEY_VERSION", raising=False)
    with pytest.raises(McpSecretError, match="mcp_secret_key_missing"):
        McpSecretCipher.from_environment()
    monkeypatch.setenv("ATHENA_MCP_SECRET_KEY", base64.b64encode(b"x" * 32).decode())
    monkeypatch.setenv("ATHENA_MCP_SECRET_KEY_VERSION", "0")
    with pytest.raises(McpSecretError, match="mcp_secret_key_invalid"):
        McpSecretCipher.from_environment()
