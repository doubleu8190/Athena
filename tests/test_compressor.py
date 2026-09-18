"""上下文压缩器测试。"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from athena.core.compression.compressor import ContextCompressor
from athena.models import Message, MessageRole


class _TokenCounter:
    """按文本长度近似 token 数的测试计数器。"""

    def count_text_tokens(self, text: str) -> int:
        return len(text)


class _LLM:
    """返回固定摘要的测试 LLM。"""

    def count_text_tokens(self, text: str) -> int:
        return len(text)

    async def ainvoke(self, messages):
        return SimpleNamespace(content="压缩后的摘要")


def _message(role: MessageRole, content: str, run_id: str) -> Message:
    return Message(
        id=f"{run_id}:{role.value}",
        session_id="session-1",
        role=role,
        content=content,
        run_id=run_id,
        timestamp=datetime.now(),
    )


def _compressor(*, keep_recent_turns: int = 3) -> ContextCompressor:
    settings = SimpleNamespace(
        max_context_tokens=20,
        compression_threshold=0.5,
        keep_recent_turns=keep_recent_turns,
        max_summary_tokens=2000,
    )
    return ContextCompressor(
        llm=_LLM(),
        token_counter=_TokenCounter(),
        db=None,
        settings=settings,
    )


@pytest.mark.asyncio
async def test_compresses_single_oversized_turn() -> None:
    compressor = _compressor()
    messages = [
        _message(MessageRole.USER, "x" * 100, "run-1"),
        _message(MessageRole.ASSISTANT, "answer", "run-1"),
    ]

    compressed = await compressor.compress(messages)

    assert len(compressed) == 1
    assert compressed[0].role == MessageRole.SYSTEM
    assert compressed[0].type == "conversation_summary"
    assert "压缩后的摘要" in compressed[0].content


@pytest.mark.asyncio
async def test_compression_keeps_configured_recent_turns_when_available() -> None:
    compressor = _compressor(keep_recent_turns=1)
    messages = [
        _message(MessageRole.USER, "old question", "run-1"),
        _message(MessageRole.ASSISTANT, "old answer", "run-1"),
        _message(MessageRole.USER, "x" * 100, "run-2"),
        _message(MessageRole.ASSISTANT, "recent answer", "run-2"),
    ]

    compressed = await compressor.compress(messages)

    assert compressed[0].type == "conversation_summary"
    assert [message.run_id for message in compressed[1:]] == ["run-2", "run-2"]
