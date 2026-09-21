"""仅处理高确定性的简单会话请求。"""

from __future__ import annotations

from athena.runtime.task_understanding.contracts import UserTaskSpec

_GREETING_MARKERS = ("你好", "您好", "谢谢", "感谢", "好的", "收到", "明白")


def _matches(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def build_fast_path_task(user_message: str) -> UserTaskSpec | None:
    """只识别空输入、问候和简短确认，复杂请求交给结构化 LLM。"""
    text = user_message.strip()
    if not text:
        return UserTaskSpec(
            goal="respond to the empty input",
            domain="general",
            mode="answer",
            confidence=1.0,
            context_requirements=["conversation"],
        )
    if _matches(text, _GREETING_MARKERS) and len(text) <= 16:
        return UserTaskSpec(
            goal=text[:1000],
            domain="general",
            mode="answer",
            confidence=1.0,
            context_requirements=["conversation"],
        )
    return None
