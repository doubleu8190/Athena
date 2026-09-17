"""命令消费者失败事件的错误信息测试。"""

from __future__ import annotations

from athena.runtime.command_consumer import CommandConsumer


def test_error_message_uses_exception_type_when_message_is_empty() -> None:
    """无文本异常仍要产生可诊断的错误内容。"""

    assert CommandConsumer._error_message(AssertionError()) == "AssertionError"


def test_error_message_preserves_exception_message() -> None:
    """有文本异常应保留原始错误内容。"""

    assert CommandConsumer._error_message(ValueError("invalid plan")) == "invalid plan"
