"""消息配对器测试."""

from __future__ import annotations

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from athena.core.compression.pairer import MessagePairer


def _human(content: str) -> HumanMessage:
    return HumanMessage(content=content)


def _assistant(content: str = "", tool_calls: list | None = None) -> AIMessage:
    return AIMessage(content=content, tool_calls=tool_calls or [])


def _system(content: str) -> SystemMessage:
    return SystemMessage(content=content)


def _tool(content: str, tool_call_id: str) -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tool_call_id)


def test_identify_turns_single_user_assistant():
    pairer = MessagePairer()
    msgs = [_human("hi"), _assistant("hello")]
    turns = pairer.identify_turns(msgs)
    assert len(turns) == 1
    assert len(turns[0]) == 2


def test_identify_turns_with_tool_calls():
    pairer = MessagePairer()
    msgs = [
        _human("read file"),
        _assistant("let me read the file", [{"id": "tc1", "name": "read_file", "args": {}}]),
        _tool("file content", "tc1"),
        _assistant("here is the file"),
    ]
    turns = pairer.identify_turns(msgs)
    assert len(turns) == 1
    assert len(turns[0]) == 4


def test_identify_turns_tool_calls_included_in_assistant():
    pairer = MessagePairer()
    msgs = [
        _human("read file"),
        _assistant("I will read the file", [{"id": "tc1", "name": "read_file", "args": {}}]),
        _tool("file content", "tc1"),
        _assistant("I have processed the file"),
    ]
    turns = pairer.identify_turns(msgs)
    assert len(turns) == 1
    assert len(turns[0]) == 4


def test_identify_turns_multiple_rounds():
    pairer = MessagePairer()
    msgs = [
        _system("sys prompt"),
        _human("q1"),
        _assistant("a1"),
        _human("q2"),
        _assistant("a2"),
    ]
    turns = pairer.identify_turns(msgs)
    # system 单独成段 + 2 个完整轮次
    assert len(turns) == 3
    assert isinstance(turns[0][0], SystemMessage)
    assert isinstance(turns[1][0], HumanMessage)
    assert isinstance(turns[2][0], HumanMessage)


def test_get_recent_turns_separation():
    pairer = MessagePairer()
    dialogue = [
        [_human(f"q{i}"), _assistant(f"a{i}")]
        for i in range(5)
    ]
    turns = [[_system("sys")]] + dialogue
    old, recent = pairer.get_recent_turns(turns, keep_count=2)
    assert len(old) == 3
    # recent 包含 system 段 + 2 个最近轮次
    assert len(recent) == 3
    assert isinstance(recent[0][0], SystemMessage)


def test_get_recent_turns_insufficient():
    pairer = MessagePairer()
    turns = [
        [_human("q1"), _assistant("a1")],
    ]
    old, recent = pairer.get_recent_turns(turns, keep_count=3)
    assert old == []
    assert recent == turns


def test_identify_turns_empty_assistant_not_complete():
    pairer = MessagePairer()
    msgs = [
        _human("q"),
        _assistant(""),
    ]
    turns = pairer.identify_turns(msgs)
    # 空 assistant 消息且无 tool_calls 不视为完整轮次
    assert len(turns) == 1
    assert turns[0][-1].content == ""
