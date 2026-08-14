#!/usr/bin/env python3
"""最小 MCP stdio server — 测试用，无第三方依赖.

按 MCP JSON-RPC 协议应答 initialize / tools/list / tools/call，
暴露一个 echo 工具。被 test_mcp_endpoints.py 以子进程方式驱动：
    [sys.executable, str(stub_path)]
"""

import json
import sys


def _handle(method: str, params: dict) -> dict:
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "athena-test-stub", "version": "0.1.0"},
        }
    if method == "tools/list":
        return {
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo back the input text",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                    },
                }
            ]
        }
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        text = f"{name}: {json.dumps(args, ensure_ascii=False)}"
        return {"content": [{"type": "text", "text": text}]}
    return {}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        # 通知（如 notifications/initialized）无 id，按 JSON-RPC 规范不得应答；
        # 对无 id 的请求回复会在客户端（官方 SDK 的 JSONRPCMessage 校验）中解析失败。
        if req.get("id") is None:
            continue
        result = _handle(method, req.get("params") or {})
        resp = {"jsonrpc": "2.0", "id": req.get("id"), "result": result}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
