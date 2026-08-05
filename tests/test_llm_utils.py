"""extract_message_text 工具函数测试."""

from langchain_core.messages import AIMessage

from athena.utils.llm import extract_message_text


def test_str_content() -> None:
    assert extract_message_text(AIMessage(content="扩展后的查询")) == "扩展后的查询"


def test_list_content_blocks() -> None:
    msg = AIMessage(
        content=[
            {"type": "text", "text": "第一部分"},
            {"type": "text", "text": "第二部分"},
        ]
    )
    assert extract_message_text(msg) == "第一部分\n第二部分"


def test_mixed_list_blocks() -> None:
    # 图片块无 text 字段，应被忽略而不是 repr 污染输出
    msg = AIMessage(
        content=[
            {"type": "image_url", "image_url": {"url": "http://x"}},
            {"type": "text", "text": "纯文本"},
        ]
    )
    assert extract_message_text(msg) == "纯文本"


def test_none_content_returns_empty() -> None:
    # AIMessage(content=None) 会被 pydantic 拒绝，无法真实构造；
    # 用无 content 属性的对象覆盖 getattr 缺省路径
    assert extract_message_text(object()) == ""


def test_empty_string_content() -> None:
    assert extract_message_text(AIMessage(content="")) == ""
