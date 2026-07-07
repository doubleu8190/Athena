"""Tests for Context Manager — session ID generation."""


from athena.core.context import ContextManager


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
