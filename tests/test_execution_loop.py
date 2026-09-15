"""LLM 与工具执行循环的上下文传递测试。"""

from __future__ import annotations

import asyncio

import pytest

from athena.core.harness.turn_executor import ToolBatchOutcome
from athena.runtime.execution_loop import execute_tool_batch


@pytest.mark.asyncio
async def test_tool_batch_preserves_conversation_messages(monkeypatch):
    """工具执行后仍保留 user、assistant 工具调用和 tool 结果消息。"""

    class FakeHarness:
        async def execute_tool_batch(self, **_: object) -> ToolBatchOutcome:
            return ToolBatchOutcome(
                messages=[
                    {
                        "role": "tool",
                        "content": "目录内容",
                        "tool_call_id": "call-1",
                    }
                ],
                tool_results=[
                    {
                        "tool_call_id": "call-1",
                        "tool_name": "list_directory",
                        "status": "success",
                    }
                ],
            )

    monkeypatch.setattr(
        "athena.runtime.execution_loop.execute_tools._executor",
        lambda _: FakeHarness(),
    )

    state = {
        "messages": [
            {"role": "user", "content": "生成表格"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "list_directory",
                        "args": {"path": "."},
                    }
                ],
            },
        ],
        "pending_tool_calls": [
            {
                "id": "call-1",
                "name": "list_directory",
                "args": {"path": "."},
            }
        ],
        "tool_results": [],
        "session_id": "session-1",
        "run_id": "run-1",
        "turn_count": 1,
        "max_retries": 3,
    }

    result = await execute_tool_batch(
        state,
        {"configurable": {"stop_signal": asyncio.Event()}},
        graph_runtime=object(),
    )

    assert [message["role"] for message in result["messages"]] == [
        "user",
        "assistant",
        "tool",
    ]
    assert result["messages"][1]["tool_calls"][0]["id"] == "call-1"
    assert result["messages"][2]["tool_call_id"] == "call-1"
