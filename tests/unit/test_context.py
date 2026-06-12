"""Tests for Context Manager — session lifecycle and context building."""

import pytest

from athena.core.context import ContextManager, SessionContext


class TestSessionId:
    """Test deterministic session ID generation."""

    def test_same_input_same_id(self):
        """Same (user_id, channel, chat_id) should produce the same session_id."""
        id1 = ContextManager.make_session_id("user1", "telegram", "12345")
        id2 = ContextManager.make_session_id("user1", "telegram", "12345")
        assert id1 == id2
        assert len(id1) == 32  # SHA-256 hex truncated

    def test_different_channel_different_id(self):
        """Different channels should produce different session IDs."""
        id1 = ContextManager.make_session_id("user1", "telegram", "12345")
        id2 = ContextManager.make_session_id("user1", "web", "12345")
        assert id1 != id2

    def test_different_chat_different_id(self):
        """Different chat IDs should produce different session IDs."""
        id1 = ContextManager.make_session_id("user1", "telegram", "111")
        id2 = ContextManager.make_session_id("user1", "telegram", "222")
        assert id1 != id2


class TestTokenEstimation:
    """Test token counting and compression threshold."""

    def test_empty_context_estimate(self):
        """Empty context should have near-zero token estimate."""
        ctx = SessionContext(
            session_id="test",
            user_id="user1",
            channel="web",
            chat_id="chat1",
        )
        from athena.core.context import ContextManager
        mgr = object.__new__(ContextManager)  # Skip init for unit test
        estimate = mgr._estimate_total_tokens(ctx)
        assert estimate == 0

    def test_history_adds_tokens(self):
        """Adding messages should increase token estimate."""
        ctx = SessionContext(
            session_id="test",
            user_id="user1",
            channel="web",
            chat_id="chat1",
        )
        ctx.message_history = [
            {"role": "user", "content": "Hello, this is a test message with some length." * 10},
        ]
        from athena.core.context import ContextManager
        mgr = object.__new__(ContextManager)
        estimate = mgr._estimate_total_tokens(ctx)
        assert estimate > 0


class TestSnapshot:
    """Test context snapshot serialization."""

    def test_build_snapshot(self):
        """Snapshot should contain all required fields."""
        ctx = SessionContext(
            session_id="test-sess",
            user_id="user1",
            channel="telegram",
            chat_id="chat1",
            conversation_summary="Previous discussion about AI",
            message_history=[{"role": "user", "content": "Hello"}],
            current_task={"task_id": "task-1", "status": "running", "completed_steps": [1]},
        )
        from athena.core.context import ContextManager
        mgr = object.__new__(ContextManager)
        snapshot = mgr._build_snapshot(ctx)

        assert snapshot["version"] == 1
        assert "snapshot_at" in snapshot
        assert snapshot["conversation_summary"] == "Previous discussion about AI"
        assert len(snapshot["message_history"]) == 1
        assert snapshot["current_task"]["task_id"] == "task-1"
        assert 1 in snapshot["current_task"]["completed_steps"]
        assert "token_count_estimate" in snapshot

    def test_restore_from_snapshot(self):
        """Restoring from snapshot should create equivalent context."""
        snapshot = {
            "version": 1,
            "snapshot_at": "2026-06-10T12:00:00Z",
            "conversation_summary": "Test summary",
            "message_history": [{"role": "user", "content": "Hi"}],
            "current_task": {"task_id": "t1", "status": "running", "completed_steps": [1, 2]},
            "injected_memories": ["mem1"],
            "token_count_estimate": 500,
        }
        from athena.core.context import ContextManager
        mgr = object.__new__(ContextManager)
        import asyncio
        ctx = asyncio.run(mgr.restore_from_snapshot("sess-1", snapshot))

        assert ctx.session_id == "sess-1"
        assert ctx.conversation_summary == "Test summary"
        assert len(ctx.message_history) == 1
        assert ctx.current_task["task_id"] == "t1"
