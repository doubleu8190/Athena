"""消息配对器测试."""

from __future__ import annotations

from athena.core.compression.pairer import MessagePairer


def test_identify_turns_single_user_assistant():
    pairer = MessagePairer()
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    turns = pairer.identify_turns(msgs)
    assert len(turns) == 1
    assert len(turns[0]) == 2


def test_identify_turns_with_tool_calls():
    pairer = MessagePairer()
    msgs = [
        {"role": "user", "content": "read file"},
        {"role": "assistant", "content": "let me read the file", "tool_calls": [{"id": "tc1", "name": "read_file"}]},
        {"role": "tool", "content": "file content", "tool_call_id": "tc1"},
        {"role": "assistant", "content": "here is the file"},
    ]
    turns = pairer.identify_turns(msgs)
    assert len(turns) == 1
    assert len(turns[0]) == 4


def test_identify_turns_tool_calls_included_in_assistant():
    pairer = MessagePairer()
    msgs = [
        {"role": "user", "content": "read file"},
        {"role": "assistant", "content": "I will read the file", "tool_calls": [{"id": "tc1", "name": "read_file"}]},
        {"role": "tool", "content": "file content", "tool_call_id": "tc1"},
        {"role": "assistant", "content": "I have processed the file"},
    ]
    turns = pairer.identify_turns(msgs)
    assert len(turns) == 1
    assert len(turns[0]) == 4


def test_identify_turns_multiple_rounds():
    pairer = MessagePairer()
    msgs = [
        {"role": "system", "content": "sys prompt"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
    ]
    turns = pairer.identify_turns(msgs)
    # system 单独成段 + 2 个完整轮次
    assert len(turns) == 3
    assert turns[0][0]["role"] == "system"
    assert turns[1][0]["role"] == "user"
    assert turns[2][0]["role"] == "user"


def test_get_recent_turns_separation():
    pairer = MessagePairer()
    dialogue = [
        [{"role": "user", "content": f"q{i}"}, {"role": "assistant", "content": f"a{i}"}]
        for i in range(5)
    ]
    turns = [[{"role": "system", "content": "sys"}]] + dialogue
    old, recent = pairer.get_recent_turns(turns, keep_count=2)
    assert len(old) == 3
    # recent 包含 system 段 + 2 个最近轮次
    assert len(recent) == 3
    assert recent[0][0]["role"] == "system"


def test_get_recent_turns_insufficient():
    pairer = MessagePairer()
    turns = [
        [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}],
    ]
    old, recent = pairer.get_recent_turns(turns, keep_count=3)
    assert old == []
    assert recent == turns


def test_identify_turns_empty_assistant_not_complete():
    pairer = MessagePairer()
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": ""},
    ]
    turns = pairer.identify_turns(msgs)
    # 空 assistant 消息且无 tool_calls 不视为完整轮次
    assert len(turns) == 1
    assert turns[0][-1]["role"] == "assistant"
    assert turns[0][-1]["content"] == ""
