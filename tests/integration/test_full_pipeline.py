"""Integration tests for the full Core pipeline.

Tests the Context → Planner → Executor flow with mocked LLM and MCP.
"""

import pytest


class TestFullPipeline:
    """End-to-end pipeline tests."""

    @pytest.mark.asyncio
    async def test_message_to_unified_message(self):
        """UnifiedMessage should be created correctly."""
        from athena.core.message import UnifiedMessage
        msg = UnifiedMessage(
            message_id="msg-1",
            channel="web",
            user_id="admin",
            content="Hello",
        )
        assert msg.message_id == "msg-1"
        assert msg.channel == "web"
        assert msg.user_id == "admin"
        assert msg.content == "Hello"
